"""EGP Extractor — opens a SAS Enterprise Guide .egp file (ZIP with XML) and builds a FlowGraph.

The .egp format (SAS EG 7.1+) is a ZIP archive containing:
- project.xml (UTF-16): project metadata, elements, PFD (Process Flow Diagrams), dependencies
- CodeTask-<id>/code.sas: SAS source code for each code task
- Query-<id>/...: generated SQL for query builder tasks
- ImportTask-<id>/...: import task metadata
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sas_migrator.core.models.graph import (
    DroppedEdge,
    Edge,
    ExtractionResidue,
    FlowGraph,
    FlowInfo,
    FlowSummary,
    NodeType,
    ResidueElement,
    SASNode,
)
from sas_migrator.core.parser.statements import parse_sas_code

# ── Helpers ─────────────────────────────────────────────────────────────────

def _classify_code(code: str) -> NodeType:
    """Classify SAS code into a NodeType based on leading keywords."""
    stripped = code.replace('\ufeff', '')  # remove BOM
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)  # remove block comments
    stripped = re.sub(r'\*[^;]*;', '', stripped)  # remove line comments
    stripped = stripped.strip().upper()

    # Match leading keyword first
    if re.match(r'PROC\s+SQL\b', stripped):
        return NodeType.PROC_SQL
    if re.match(r'PROC\s+SORT\b', stripped):
        return NodeType.PROC_SORT
    if re.match(r'PROC\s+FREQ\b', stripped):
        return NodeType.PROC_FREQ
    if re.match(r'PROC\s+MEANS\b', stripped):
        return NodeType.PROC_MEANS
    if re.match(r'PROC\s+TRANSPOSE\b', stripped):
        return NodeType.PROC_TRANSPOSE
    if re.match(r'PROC\s+EXPORT\b', stripped):
        return NodeType.EXPORT
    if re.match(r'PROC\s+IMPORT\b', stripped):
        return NodeType.IMPORT
    if re.match(r'PROC\s+', stripped):
        return NodeType.PROC_OTHER
    if re.match(r'DATA\s+', stripped):
        return NodeType.DATA_STEP
    if re.match(r'%MACRO\b', stripped):
        return NodeType.MACRO
    # Fallback: search for dominant pattern anywhere in code
    if re.search(r'\bPROC\s+SQL\b', stripped):
        return NodeType.PROC_SQL
    if re.search(r'\bPROC\s+IMPORT\b', stripped):
        return NodeType.IMPORT
    if re.search(r'\bDATA\s+\w', stripped):
        return NodeType.DATA_STEP
    if re.search(r'\bPROC\s+', stripped):
        return NodeType.PROC_OTHER
    return NodeType.OTHER


_CONSTRUCT_PATTERN = re.compile(
    r"^\s*(?:(PROC)\s+(\w+)|(DATA)\s+\w|(%MACRO)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _detect_constructs(code: str) -> list[str]:
    """Lista ordenada y sin duplicados de TODOS los constructs del código.

    Complementa a ``_classify_code`` (que solo mira el construct líder): un
    CodeTask con DATA step + PROC SQL reporta ambos. Se guarda en
    ``node.metadata["sas_constructs"]``.
    """
    cleaned = re.sub(r'/\*.*?\*/', '', code.replace('﻿', ''), flags=re.DOTALL)
    constructs: list[str] = []
    for m in _CONSTRUCT_PATTERN.finditer(cleaned):
        if m.group(1):
            name = f"PROC {m.group(2).upper()}"
        elif m.group(3):
            name = "DATA"
        else:
            name = "%MACRO"
        if name not in constructs:
            constructs.append(name)
    return constructs


_DS_PATTERN = re.compile(
    r"""(?:
        (?:SET|MERGE|UPDATE)\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)  # DATA step inputs
      | FROM\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)                  # SQL FROM
      | DATA\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)                  # DATA step output
      | OUT\s*=\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)               # PROC OUT=
      | LIBNAME\s+([A-Za-z_]\w+)                                  # LIBNAME reference
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _extract_datasets_legacy(code: str) -> tuple[list[str], list[str], list[str]]:
    """Extractor v1 por regex. Ya no alimenta los nodos: sobrevive solo como
    chequeo cruzado contra el parser v2 (desacuerdos → parser_upgrade_report)."""
    inputs: list[str] = []
    outputs: list[str] = []
    libraries: list[str] = []

    for m in _DS_PATTERN.finditer(code):
        if m.group(1):
            inputs.append(m.group(1))
        elif m.group(2):
            inputs.append(m.group(2))
        elif m.group(3):
            ds = m.group(3)
            if ds.upper() not in ("_NULL_", "_WEBOUT"):
                outputs.append(ds)
        elif m.group(4):
            outputs.append(m.group(4))
        elif m.group(5):
            libraries.append(m.group(5))

    return (
        sorted(set(inputs)),
        sorted(set(outputs)),
        sorted(set(libraries)),
    )


def _extract_datasets(code: str) -> tuple[list[str], list[str], list[str]]:
    """Inputs/outputs/librerías de un nodo, vía el parser v2 (statement-dirigido).

    Formato: ``LIB.TABLA`` en mayúsculas, con WORK explícito para los no
    calificados. Las referencias macro-dependientes (``&lib..tabla``) no entran
    aquí — quedan en ``NodeParse.macro_refs`` y las resuelve la entrevista B4b.
    """
    parse = parse_sas_code(code)
    return (
        sorted({f"{r.libref}.{r.table}" for r in parse.inputs}),
        sorted({f"{r.libref}.{r.table}" for r in parse.outputs}),
        sorted(parse.librefs_declared),
    )


# ── Process Flow inventory ──────────────────────────────────────────────────

def _slugify(label: str) -> str:
    """Convierte una etiqueta de Process Flow en un slug seguro para nombres de notebook."""
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    return slug or "flujo"


def _container_type(pfd_id: str) -> str:
    """Deriva el tipo de contenedor a partir del prefijo del ID del PFD."""
    if pfd_id.startswith("PFD-"):
        return "PFD"
    if pfd_id.startswith("ProcessFlowContainer-"):
        return "ProcessFlowContainer"
    if pfd_id.startswith("OrderedListContainer-"):
        return "OrderedListContainer"
    return "other"


def build_flow_summary(graph: FlowGraph) -> FlowSummary:
    """Construye el inventario determinista de Process Flows desde un FlowGraph.

    Agrupa los nodos por ``pfd_id`` y resume, por flujo: conteo de nodos, tipos,
    datasets de entrada/salida, librerías y un slug sugerido para el notebook. El
    campo ``description`` queda vacío — lo completa el agente code-analyst con
    lenguaje de negocio. Esta función NO razona sobre el código SAS: solo agrega
    metadatos ya extraídos.
    """
    nodes_by_pfd: dict[str, list[SASNode]] = {}
    unassigned: list[str] = []
    for node in graph.nodes:
        if node.pfd_id:
            nodes_by_pfd.setdefault(node.pfd_id, []).append(node)
        else:
            unassigned.append(node.id)

    def _make_flow(pfd_id: str, label: str) -> FlowInfo:
        pfd_nodes = nodes_by_pfd.get(pfd_id, [])
        node_types: dict[str, int] = {}
        inputs: set[str] = set()
        outputs: set[str] = set()
        libs: set[str] = set()
        for node in pfd_nodes:
            type_name = node.node_type.value
            node_types[type_name] = node_types.get(type_name, 0) + 1
            inputs.update(node.inputs)
            outputs.update(node.outputs)
            libs.update(node.libraries)
        ctype = _container_type(pfd_id)
        migratable = len(pfd_nodes) > 0 and ctype != "OrderedListContainer"
        return FlowInfo(
            pfd_id=pfd_id,
            pfd_label=label,
            container_type=ctype,
            migratable_candidate=migratable,
            node_count=len(pfd_nodes),
            node_ids=sorted(n.id for n in pfd_nodes),
            node_types=dict(sorted(node_types.items())),
            input_datasets=sorted(inputs),
            output_datasets=sorted(outputs),
            libraries=sorted(libs),
            notebook_slug=_slugify(label),
        )

    flows: list[FlowInfo] = []
    # Flujos declarados en project.xml (orden de declaración).
    for pfd_id, label in graph.pfds.items():
        flows.append(_make_flow(pfd_id, label))
    # Flujos referenciados por nodos pero ausentes del índice de contenedores (raro).
    for pfd_id in sorted(set(nodes_by_pfd) - set(graph.pfds)):
        label = next((n.pfd_label or "" for n in nodes_by_pfd[pfd_id]), "")
        flows.append(_make_flow(pfd_id, label))

    return FlowSummary(
        project_name=graph.project_name,
        egp_path=graph.egp_path,
        generated_at=datetime.utcnow(),
        total_flows=len(flows),
        migratable_flows=sum(1 for f in flows if f.migratable_candidate),
        total_nodes=len(graph.nodes),
        flows=flows,
        unassigned_nodes=sorted(unassigned),
    )


# ── Preservación de enriquecimiento entre re-extracciones ───────────────────

# Campos que NO produce el extractor: los escribe generate_analysis.py
# (clasificación determinista) o el code-analyst (criterio LLM). Una
# re-extracción los conserva; todo lo demás se sobreescribe desde el .egp.
PRESERVED_NODE_METADATA = ("classification", "complexity", "migration_priority")
PRESERVED_NODE_FIELDS = ("translation_notes",)


def _read_json_if_exists(path: Path) -> dict:
    """Lee un JSON de disco; dict vacío si no existe o está corrupto."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_node_enrichment(node_dict: dict, node_path: Path) -> dict:
    """Re-injerta en un nodo recién extraído el análisis previo del mismo nodo.

    Sin esto, correr el extractor después de la Fase 2 borra silenciosamente
    `classification`/`complexity`/`migration_priority` y las `translation_notes`
    del code-analyst, mientras el ledger sigue afirmando que el nodo fue revisado.
    """
    previous = _read_json_if_exists(node_path)
    if not previous:
        return node_dict

    prev_meta = previous.get("metadata") or {}
    if isinstance(prev_meta, dict):
        meta = node_dict.setdefault("metadata", {})
        for key in PRESERVED_NODE_METADATA:
            if key in prev_meta and key not in meta:
                meta[key] = prev_meta[key]

    for key in PRESERVED_NODE_FIELDS:
        if previous.get(key) and not node_dict.get(key):
            node_dict[key] = previous[key]

    return node_dict


def _merge_flow_descriptions(summary_dict: dict, summary_path: Path) -> dict:
    """Conserva las `description` de negocio ya redactadas, emparejadas por pfd_id."""
    previous = _read_json_if_exists(summary_path)
    prior_desc = {
        flow.get("pfd_id"): flow.get("description")
        for flow in previous.get("flows", [])
        if isinstance(flow, dict) and flow.get("description")
    }
    if not prior_desc:
        return summary_dict

    for flow in summary_dict.get("flows", []):
        if not flow.get("description") and prior_desc.get(flow.get("pfd_id")):
            flow["description"] = prior_desc[flow["pfd_id"]]
    return summary_dict


# ── Main extractor ──────────────────────────────────────────────────────────

def extract_egp(egp_path: str | Path, output_dir: str | Path | None = None) -> FlowGraph:
    """Extract a FlowGraph from a SAS Enterprise Guide .egp file.

    Args:
        egp_path: Path to the .egp file.
        output_dir: If provided, persists flow_graph.json, flow_summary.json and
            nodes/<id>.json there.

    Returns:
        FlowGraph with nodes and edges.
    """
    egp_path = Path(egp_path)
    if not egp_path.exists():
        raise FileNotFoundError(f"EGP file not found: {egp_path}")

    with zipfile.ZipFile(egp_path, "r") as zf:
        # ── Parse project.xml ───────────────────────────────────────────
        raw_xml = zf.read("project.xml")
        # EGP project.xml is UTF-16 encoded
        xml_text = raw_xml.decode("utf-16")
        root = ET.fromstring(xml_text)

        project_name = ""
        project_elem = root.find("Element")
        if project_elem is not None:
            project_name = project_elem.findtext("Label") or ""

        # ── Build element index from <Elements> section ─────────────────
        elements_section = root.find("Elements")
        element_index: dict[str, dict[str, Any]] = {}
        code_task_ids: set[str] = set()
        query_task_ids: set[str] = set()
        other_task_ids: set[str] = set()
        warnings: list[str] = []

        if elements_section is not None:
            for el_wrapper in elements_section:
                inner = el_wrapper.find("Element")
                if inner is None:
                    continue
                el_id = (inner.findtext("ID") or "").strip()
                el_type = (inner.findtext("Type") or "").strip()
                el_label = (inner.findtext("Label") or "").strip()
                el_container = (inner.findtext("Container") or "").strip()
                el_inputs = (inner.findtext("InputIDs") or "").strip()

                element_index[el_id] = {
                    "type": el_type,
                    "label": el_label,
                    "container": el_container,
                    "inputs": el_inputs,
                    "wrapper": el_wrapper,
                }

                if el_type == "TASK" and el_id.startswith("CodeTask-"):
                    code_task_ids.add(el_id)
                elif el_type == "TASK" and el_id.startswith("Query-"):
                    query_task_ids.add(el_id)
                elif el_type == "TASK" and el_id:
                    # Tareas nativas de EG (ImportTask, asistentes): se materializan
                    # igual — nunca desaparecen en silencio.
                    other_task_ids.add(el_id)

        # ── Build PFD index (container ID → label) ─────────────────────
        pfds: dict[str, str] = {}
        for el_id, info in element_index.items():
            if info["type"] == "CONTAINER":
                pfds[el_id] = info["label"]

        # ── Extract code.sas for each CodeTask ─────────────────────────
        # The ZIP contains versioned snapshots: <prefix>/PFD-<id>/CodeTask-<id>/code.sas
        # Also top-level: CodeTask-<id>/code.sas (latest version)
        # We prefer top-level entries (latest).
        code_sas_map: dict[str, str] = {}  # task id → SAS code
        sas_entries = [
            n for n in zf.namelist()
            if n.endswith("/code.sas") or n.endswith(".sql")
        ]

        def _read_code_entry(entry: str) -> str | None:
            try:
                raw_code = zf.read(entry)
                try:
                    return raw_code.decode("utf-8")
                except UnicodeDecodeError:
                    return raw_code.decode("latin-1")
            except Exception as exc:
                warnings.append(f"No se pudo leer {entry}: {exc}")
                return None

        # Index by owning task ID (CodeTask-* y Query-*). Prefer shorter paths
        # (top-level = latest version; deeper = versioned snapshots).
        # Solo se admiten owners que correspondan a tareas materializadas en
        # project.xml, para evitar contaminar el residuo con snapshots de rutas
        # no vinculadas al grafo actual.
        known_task_ids = set(code_task_ids) | set(query_task_ids)
        chosen_path: dict[str, str] = {}
        # Código SAS en el ZIP cuyo owner no existe en project.xml. Se separa en
        # dos casos: en la raíz del ZIP (`CodeTask-<id>/code.sas`) es código que
        # el .egp trae y el grafo no explica — el gate de Fase 2 promete no
        # perderlo en silencio. Anidado bajo un prefijo de snapshot
        # (`<snap>/PFD-<id>/CodeTask-<id>/code.sas`) es una versión histórica ya
        # superada: se informa, pero no bloquea.
        unowned_code_entries: dict[str, str] = {}
        snapshot_code_entries: dict[str, str] = {}
        for entry in sas_entries:
            parts = entry.split("/")
            owner = next(
                (p for p in parts if p.startswith("CodeTask-") or p.startswith("Query-")),
                None,
            )
            if owner is None:
                continue
            if owner not in known_task_ids:
                bucket = unowned_code_entries if len(parts) <= 2 else snapshot_code_entries
                prior = bucket.get(owner)
                if prior is None or len(parts) < len(prior.split("/")):
                    bucket[owner] = entry
                continue
            if owner not in chosen_path or len(parts) < len(chosen_path[owner].split("/")):
                code_text = _read_code_entry(entry)
                if code_text is not None:
                    code_sas_map[owner] = code_text
                    chosen_path[owner] = entry

        # ── Build SASNode objects ───────────────────────────────────────
        nodes: list[SASNode] = []
        node_ids_seen: set[str] = set()

        tasks_without_code: list[str] = []
        queries_without_payload: list[str] = []

        # Process CodeTasks
        for ct_id in sorted(code_task_ids):
            info = element_index.get(ct_id, {})
            code = code_sas_map.get(ct_id, "")
            node_type = _classify_code(code) if code else NodeType.OTHER
            inputs, outputs, libraries = _extract_datasets(code) if code else ([], [], [])

            pfd_id = info.get("container", "")
            pfd_label = pfds.get(pfd_id, "")

            metadata: dict[str, Any] = {}
            if code:
                metadata["sas_constructs"] = _detect_constructs(code)
            else:
                tasks_without_code.append(ct_id)
                metadata["requires_manual_review"] = True

            node = SASNode(
                id=ct_id,
                label=info.get("label", ""),
                node_type=node_type,
                code=code,
                pfd_id=pfd_id or None,
                pfd_label=pfd_label or None,
                inputs=inputs,
                outputs=outputs,
                libraries=libraries,
                metadata=metadata,
            )
            nodes.append(node)
            node_ids_seen.add(ct_id)

        # Process Query tasks — usar el payload de código si existe en el ZIP
        # (Query-<id>/code.sas o *.sql); si no, queda sin código y se reporta.
        for q_id in sorted(query_task_ids):
            info = element_index.get(q_id, {})
            pfd_id = info.get("container", "")
            pfd_label = pfds.get(pfd_id, "")

            code = code_sas_map.get(q_id, "")
            if code:
                inputs, outputs, libraries = _extract_datasets(code)
                metadata = {"sas_constructs": _detect_constructs(code)}
            else:
                inputs, outputs, libraries = [], [], []
                queries_without_payload.append(q_id)
                metadata = {"requires_manual_review": True}

            node = SASNode(
                id=q_id,
                label=info.get("label", ""),
                node_type=NodeType.QUERY,
                code=code,
                pfd_id=pfd_id or None,
                pfd_label=pfd_label or None,
                inputs=inputs,
                outputs=outputs,
                libraries=libraries,
                metadata=metadata,
            )
            nodes.append(node)
            node_ids_seen.add(q_id)

        # Process native EG tasks (ImportTask, asistentes point-and-click):
        # se materializan siempre — la decisión de ignorarlos o traducirlos a
        # mano se toma explícitamente en la entrevista, nunca por omisión.
        for t_id in sorted(other_task_ids):
            info = element_index.get(t_id, {})
            pfd_id = info.get("container", "")
            pfd_label = pfds.get(pfd_id, "")
            task_kind = t_id.split("-", 1)[0]
            node_type = NodeType.IMPORT if task_kind == "ImportTask" else NodeType.OTHER

            node = SASNode(
                id=t_id,
                label=info.get("label", ""),
                node_type=node_type,
                pfd_id=pfd_id or None,
                pfd_label=pfd_label or None,
                metadata={
                    "native_task": True,
                    "task_kind": task_kind,
                    "requires_manual_review": True,
                },
            )
            nodes.append(node)
            node_ids_seen.add(t_id)

        # ── Build edges from PFD Dependencies ──────────────────────────
        # Toda arista descartada queda registrada en el residuo con su razón:
        # endpoint_unknown (ID que no existe en project.xml → inexplicado) o
        # endpoint_not_node (elemento conocido pero no-TASK, ej. dato/metadata).
        edges: list[Edge] = []
        dropped_edges: list[DroppedEdge] = []
        if elements_section is not None:
            for el_wrapper in elements_section:
                pfd_elem = el_wrapper.find("PFD")
                if pfd_elem is None:
                    continue
                for process in pfd_elem:
                    p_inner = process.find("Element")
                    if p_inner is None:
                        continue
                    p_id = (p_inner.findtext("ID") or "").strip()
                    deps = process.find("Dependencies")
                    if deps is not None:
                        for dep in deps:
                            dep_id = (dep.text or "").strip()
                            if not dep_id or not p_id:
                                continue
                            if dep_id in node_ids_seen and p_id in node_ids_seen:
                                edges.append(Edge(source=dep_id, target=p_id))
                            else:
                                missing = [x for x in (dep_id, p_id) if x not in node_ids_seen]
                                reason = (
                                    "endpoint_unknown"
                                    if any(x not in element_index for x in missing)
                                    else "endpoint_not_node"
                                )
                                dropped_edges.append(
                                    DroppedEdge(source=dep_id, target=p_id, reason=reason)
                                )

        # Deduplicate edges
        seen_edges: set[tuple[str, str]] = set()
        unique_edges: list[Edge] = []
        for e in edges:
            key = (e.source, e.target)
            if key not in seen_edges:
                seen_edges.add(key)
                unique_edges.append(e)

        # ── Cuadratura .egp ↔ extracción (residuo) ─────────────────────
        expected_non_nodes: list[ResidueElement] = []
        unmaterialized_tasks: list[ResidueElement] = []
        element_type_counts: dict[str, int] = {}
        for el_id, info in element_index.items():
            el_type = info["type"] or "(sin tipo)"
            element_type_counts[el_type] = element_type_counts.get(el_type, 0) + 1
            if el_id in node_ids_seen:
                continue
            residue_el = ResidueElement(
                id=el_id,
                type=info["type"],
                label=info["label"],
                container=info["container"],
            )
            if info["type"] == "TASK":
                unmaterialized_tasks.append(residue_el)
            else:
                expected_non_nodes.append(residue_el)

        orphan_code_entries: list[str] = []
        for owner, entry in chosen_path.items():
            if owner not in node_ids_seen:
                orphan_code_entries.append(entry)
        # Owners que ni siquiera aparecen en project.xml (solo los de la raíz).
        orphan_code_entries.extend(unowned_code_entries.values())

        unexplained_dropped = [e for e in dropped_edges if e.reason == "endpoint_unknown"]
        residue = ExtractionResidue(
            egp_path=str(egp_path),
            total_elements=len(element_index),
            materialized_nodes=len(nodes),
            element_type_counts=dict(sorted(element_type_counts.items())),
            expected_non_nodes=expected_non_nodes,
            unmaterialized_tasks=unmaterialized_tasks,
            orphan_code_entries=sorted(orphan_code_entries),
            snapshot_code_entries=sorted(snapshot_code_entries.values()),
            tasks_without_code=sorted(tasks_without_code),
            queries_without_payload=sorted(queries_without_payload),
            dropped_edges=dropped_edges,
            warnings=warnings,
            unexplained_count=(
                len(unmaterialized_tasks)
                + len(orphan_code_entries)
                + len(unexplained_dropped)
                + len(warnings)
            ),
        )

        graph = FlowGraph(
            project_name=project_name,
            egp_path=str(egp_path),
            nodes=nodes,
            edges=unique_edges,
            pfds=pfds,
            metadata={
                "eg_version": root.attrib.get("EGVersion", ""),
                "total_elements": len(element_index),
                "code_tasks": len(code_task_ids),
                "query_tasks": len(query_task_ids),
                "native_tasks": len(other_task_ids),
                "containers": len(pfds),
                "extraction_residue_summary": {
                    "unexplained_count": residue.unexplained_count,
                    "unmaterialized_tasks": len(unmaterialized_tasks),
                    "orphan_code_entries": len(orphan_code_entries),
                    "tasks_without_code": len(tasks_without_code),
                    "queries_without_payload": len(queries_without_payload),
                    "dropped_edges": len(dropped_edges),
                    "warnings": len(warnings),
                },
            },
        )

    # ── Persist if output_dir given ─────────────────────────────────────
    if output_dir is not None:
        out = Path(output_dir)
        os.makedirs(out, exist_ok=True)
        os.makedirs(out / "nodes", exist_ok=True)

        # flow_graph.json (without per-node code to keep it manageable)
        graph_dict = graph.model_dump(mode="json")
        for n in graph_dict["nodes"]:
            n.pop("code", None)
        with open(out / "flow_graph.json", "w", encoding="utf-8") as f:
            json.dump(graph_dict, f, indent=2, ensure_ascii=False)

        # flow_summary.json — inventario de Process Flows (un flujo = un notebook candidato).
        # Una re-extracción NO debe borrar las descripciones de negocio que el
        # code-analyst ya redactó: se re-injertan por pfd_id.
        flow_summary = build_flow_summary(graph)
        summary_dict = _merge_flow_descriptions(
            flow_summary.model_dump(mode="json"), out / "flow_summary.json"
        )
        with open(out / "flow_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary_dict, f, indent=2, ensure_ascii=False)

        # Individual node files (with code). Igual que arriba: la extracción es
        # la fuente de verdad de la topología y el código, pero NO puede pisar
        # el análisis (clasificación determinista + criterio del code-analyst).
        for node in graph.nodes:
            node_path = out / "nodes" / f"{node.id}.json"
            node_dict = _merge_node_enrichment(node.model_dump(mode="json"), node_path)
            with open(node_path, "w", encoding="utf-8") as f:
                json.dump(node_dict, f, indent=2, ensure_ascii=False)

        # extraction_residue.json — cuadratura .egp ↔ nodos (gate de Fase 2)
        with open(out / "extraction_residue.json", "w", encoding="utf-8") as f:
            json.dump(residue.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

    return graph
