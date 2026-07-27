"""Runners LLM por fase — la rama no-stub de los nodos del grafo.

Cada runner es función de (state_dir, workspace): lee artefactos de state/,
llama al caller estructurado y escribe los MISMOS artefactos que el stub, con
los mismos contratos que exigen los gates. Un NeedsHuman se registra en
state/needs_human.yaml (el gate de la fase bloquea) y el runner sigue con el
resto — nunca silencio, nunca un crash por contenido.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from sas_migrator.core.models.translation import NodeTranslation
from sas_migrator.core.utils.needs_human import record as record_needs_human
from sas_migrator.llm import prompt_builder, runtime
from sas_migrator.llm.contracts import (
    DiagnosesOut,
    DocsOut,
    FileMappingBatch,
    ImprovementsOut,
    PfdAnalysisOut,
)
from sas_migrator.llm.errors import NeedsHuman

MAX_CODE_CHARS = 20_000  # techo defensivo por nodo en los prompts


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _seeded_improvements(state_dir: Path) -> list[dict]:
    """Fichas M-xxx escritas a mano en ``state/improvements_seed.yaml``.

    La fase 2 sobrescribe improvements_proposed.yaml, así que una ficha que el
    LLM no puede derivar de la evidencia (una decisión de arquitectura, p. ej.
    usar un SDK oficial en vez de la llamada HTTP cruda) necesita esta puerta.
    Se validan contra Improvement como cualquier otra y entran siempre como
    ``proposed``: sembrar una ficha NO la aprueba, solo garantiza que la
    entrevista B5 te la pregunte.
    """
    path = state_dir / "improvements_seed.yaml"
    if not path.exists():
        return []
    from sas_migrator.core.models.analysis import Improvement

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    seeded = []
    for raw in doc.get("improvements", []):
        if not isinstance(raw, dict):
            continue
        data = json.loads(Improvement.model_validate({**raw, "status": "proposed"}).model_dump_json())
        seeded.append(data)
    return seeded


def _node_code(state_dir: Path, node_id: str) -> str:
    path = state_dir / "nodes" / f"{node_id}.json"
    if not path.exists():
        return ""
    return str(_load_json(path).get("code") or "")[:MAX_CODE_CHARS]


# ── Fase 2: análisis (map por PFD + reduce de mejoras) ──────────────────────

def run_analysis(state_dir: Path, workspace: Path) -> dict:
    state_dir = Path(state_dir)
    caller = runtime.get_caller(workspace)
    index = _load_json(state_dir / "nodes_index.json")
    nodes = index.get("nodes", [])

    by_pfd: dict[str, list[dict]] = {}
    for n in nodes:
        by_pfd.setdefault(str(n.get("pfd_id") or "sin_pfd"), []).append(n)

    reviews_dir = state_dir / "analysis_reviews"
    reviews_dir.mkdir(exist_ok=True)
    summary_path = state_dir / "flow_summary.json"
    summary = _load_json(summary_path)
    flows_by_pfd = {f.get("pfd_id"): f for f in summary.get("flows", [])}
    pfds_ok = 0

    for pfd_id in sorted(by_pfd):
        pfd_nodes = by_pfd[pfd_id]
        expected_ids = [str(n["id"]) for n in pfd_nodes]
        head = prompt_builder.header_line({"pfd_id": pfd_id, "node_ids": expected_ids})
        body = "\n\n".join(
            f"### {n['id']} — {n.get('label', '')} ({n.get('node_type', '?')})\n"
            f"```sas\n{_node_code(state_dir, str(n['id']))}\n```"
            for n in pfd_nodes
        )
        try:
            out = caller.call(
                task="analysis_reviews",
                system_blocks=prompt_builder.build_analysis_system(),
                user_content=f"{head}\n\n{body}",
                output_model=PfdAnalysisOut,
            )
        except NeedsHuman as exc:
            record_needs_human(
                state_dir, phase=2, task="analysis_reviews", node_id=None,
                reason=exc.reason, detail=f"PFD {pfd_id}: {exc.detail}",
                attempts=exc.attempts,
            )
            continue

        known = set(expected_ids)
        reviews = [
            {"node_id": r.node_id, "note": r.note}
            for r in out.reviews
            if r.node_id in known
        ]
        _dump_json(reviews_dir / f"{pfd_id}.json", {"pfd_id": pfd_id, "reviews": reviews})
        flow = flows_by_pfd.get(pfd_id)
        if flow is not None and not str(flow.get("description") or "").strip():
            flow["description"] = out.flow_description
        pfds_ok += 1

    _dump_json(summary_path, summary)

    # Reduce: fichas M-xxx desde la evidencia (más las sembradas a mano).
    evidence = _load_json(state_dir / "analysis_evidence.json")
    smells = _load_json(state_dir / "code_smells.json")
    smell_cats = sorted(
        {str(s.get("category", "")).strip() for s in smells.get("smells", []) if s.get("category")}
    )
    head = prompt_builder.header_line({"smell_categories": smell_cats})
    user = (
        f"{head}\n\n## analysis_evidence.json\n```json\n"
        + json.dumps(evidence, ensure_ascii=False)[:40_000]
        + "\n```\n\n## code_smells.json (resumen)\n```json\n"
        + json.dumps(smells.get("summary", {}), ensure_ascii=False)
        + "\n```\n"
    )
    try:
        out = caller.call(
            task="improvements",
            system_blocks=prompt_builder.build_improvements_system(),
            user_content=user,
            output_model=ImprovementsOut,
        )
        improvements = list(_seeded_improvements(state_dir))
        seen = {str(i.get("id")) for i in improvements}
        for imp in out.improvements:
            data = json.loads(imp.model_dump_json())
            data["status"] = "proposed"  # la decisión es del usuario, siempre
            if str(data.get("id")) in seen:  # una semilla con el mismo id manda
                continue
            improvements.append(data)
        doc = {
            "improvements": improvements,
            "category_scan_summary": {v.category: v.verdict for v in out.category_scan},
        }
        (state_dir / "improvements_proposed.yaml").write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    except NeedsHuman as exc:
        record_needs_human(
            state_dir, phase=2, task="improvements", reason=exc.reason,
            detail=exc.detail, attempts=exc.attempts,
        )

    return {"pfds_ok": pfds_ok, "pfds_total": len(by_pfd)}


# ── Fase 3: matching archivo↔nodo ───────────────────────────────────────────

def run_matching(state_dir: Path, workspace: Path) -> dict:
    state_dir = Path(state_dir)
    profiles_path = state_dir / "profile_report.json"
    profiles = []
    if profiles_path.exists():
        doc = _load_json(profiles_path)
        profiles = doc if isinstance(doc, list) else doc.get("profiles", [])

    if not profiles:
        _dump_json(state_dir / "file_mapping.json", {"mappings": []})
        return {"mappings": 0, "llm": False}

    index = _load_json(state_dir / "nodes_index.json")
    node_summaries = [
        {
            "id": n["id"],
            "label": n.get("label", ""),
            "node_type": n.get("node_type", ""),
            "placement": n.get("placement"),
            "flags": n.get("flags", {}),
        }
        for n in index.get("nodes", [])
    ]
    head = prompt_builder.header_line(
        {"files": [str(p.get("file_path", "")) for p in profiles]}
    )
    user = (
        f"{head}\n\n## Perfiles\n```json\n"
        + json.dumps(profiles, ensure_ascii=False)[:40_000]
        + "\n```\n\n## Nodos\n```json\n"
        + json.dumps(node_summaries, ensure_ascii=False)[:40_000]
        + "\n```\n"
    )
    caller = runtime.get_caller(workspace)
    try:
        out = caller.call(
            task="matching",
            system_blocks=prompt_builder.build_matching_system(),
            user_content=user,
            output_model=FileMappingBatch,
        )
        mappings = [json.loads(m.model_dump_json()) for m in out.mappings]
    except NeedsHuman as exc:
        record_needs_human(
            state_dir, phase=3, task="matching", reason=exc.reason,
            detail=exc.detail, attempts=exc.attempts,
        )
        # Fallback honesto: todo pendiente de confirmación humana.
        mappings = [
            {
                "file_path": str(p.get("file_path", "")),
                "node_id": None,
                "role": "unknown",
                "confidence": 0.0,
                "reasons": ["matching LLM falló — confirmar a mano (needs_human)"],
                "needs_confirmation": True,
            }
            for p in profiles
        ]
    _dump_json(state_dir / "file_mapping.json", {"mappings": mappings})
    if profiles and not (state_dir / "column_mapping.yaml").exists():
        (state_dir / "column_mapping.yaml").write_text(
            yaml.safe_dump({"mappings": []}), encoding="utf-8"
        )
    return {"mappings": len(mappings), "llm": True}


# ── Fase 6: traducción por nodo + ensamblado ────────────────────────────────

def run_translation(state_dir: Path, output_dir: Path, workspace: Path) -> dict:
    from sas_migrator.core.assembly.notebook import assemble_notebooks

    state_dir = Path(state_dir)
    plan = _load_json(state_dir / "translation_plan.json")
    caller = runtime.get_caller(workspace)
    system = prompt_builder.build_translation_system()

    db_aliases: list[str] = []
    conns_path = state_dir / "db_connections.yaml"
    if conns_path.exists():
        conns = yaml.safe_load(conns_path.read_text(encoding="utf-8")) or {}
        db_aliases = [str(c.get("alias", "")) for c in conns.get("connections", [])]

    # Persistir cada traducción apenas se obtiene (no al final del loop): si el
    # proceso muere a mitad de camino, retomar la corrida salta los nodos que
    # ya tengan .json en disco en vez de perder todo el trabajo en memoria.
    trans_dir = state_dir / "translations"
    trans_dir.mkdir(exist_ok=True)
    translations: dict[str, NodeTranslation] = {}
    for path in sorted(trans_dir.glob("*.json")):
        nt = NodeTranslation.model_validate(_load_json(path))
        translations[nt.node_id] = nt

    for target in plan.get("targets", []):
        nid = str(target.get("node_id"))
        if nid in translations:
            continue
        user = prompt_builder.build_translation_user(
            target, _node_code(state_dir, nid), db_aliases
        )
        try:
            nt = caller.call(
                task="translation",
                system_blocks=system,
                user_content=user,
                output_model=NodeTranslation,
            )
        except NeedsHuman as exc:
            record_needs_human(
                state_dir, phase=6, task="translation", node_id=nid,
                reason=exc.reason, detail=exc.detail, attempts=exc.attempts,
            )
            continue
        # Identidad defensiva: el mapping se construye con los ids del PLAN.
        nt = nt.model_copy(
            update={"node_id": nid, "node_label": target.get("node_label") or nid}
        )
        translations[nid] = nt
        _dump_json(trans_dir / f"{nid}.json", json.loads(nt.model_dump_json()))

    mapping, failures = assemble_notebooks(
        plan, translations, Path(output_dir), db_bootstrap=bool(db_aliases)
    )
    for failure in failures:
        record_needs_human(
            state_dir, phase=6, task="assembly", node_id=failure.node_id,
            reason="static_check_failed", detail=f"{failure.reason}: {failure.detail}",
        )
    _dump_json(
        state_dir / "sas_python_mapping.json", json.loads(mapping.model_dump_json())
    )
    return {
        "translated": len(translations),
        "assembly_failures": len(failures),
        "targets": len(plan.get("targets", [])),
    }


# ── Fase 9: re-traducción de nodos afectados por una iteración ──────────────

def retranslate_nodes(
    state_dir: Path,
    output_dir: Path,
    workspace: Path,
    node_ids: list[str],
    iteration_note: str,
) -> dict:
    """Re-traduce SOLO los nodos afectados (con la instrucción de la iteración
    como contexto) y re-ensambla los notebooks con el resto de las
    traducciones persistidas intactas."""
    from sas_migrator.core.assembly.notebook import assemble_notebooks

    state_dir = Path(state_dir)
    plan = _load_json(state_dir / "translation_plan.json")
    targets_by_id = {str(t.get("node_id")): t for t in plan.get("targets", [])}
    trans_dir = state_dir / "translations"

    translations: dict[str, NodeTranslation] = {}
    for path in sorted(trans_dir.glob("*.json")) if trans_dir.exists() else []:
        nt = NodeTranslation.model_validate(_load_json(path))
        translations[nt.node_id] = nt

    caller = runtime.get_caller(workspace)
    system = prompt_builder.build_translation_system()
    db_aliases: list[str] = []
    conns_path = state_dir / "db_connections.yaml"
    if conns_path.exists():
        conns = yaml.safe_load(conns_path.read_text(encoding="utf-8")) or {}
        db_aliases = [str(c.get("alias", "")) for c in conns.get("connections", [])]

    retranslated = 0
    for nid in node_ids:
        target = targets_by_id.get(nid)
        if target is None:
            continue
        user = prompt_builder.build_translation_user(
            target, _node_code(state_dir, nid), db_aliases,
            iteration_note=iteration_note,
        )
        try:
            nt = caller.call(
                task="translation", system_blocks=system, user_content=user,
                output_model=NodeTranslation,
            )
        except NeedsHuman as exc:
            record_needs_human(
                state_dir, phase=9, task="translation", node_id=nid,
                reason=exc.reason, detail=exc.detail, attempts=exc.attempts,
            )
            continue
        nt = nt.model_copy(
            update={"node_id": nid, "node_label": target.get("node_label") or nid}
        )
        translations[nid] = nt
        trans_dir.mkdir(exist_ok=True)
        _dump_json(trans_dir / f"{nid}.json", json.loads(nt.model_dump_json()))
        retranslated += 1

    mapping, failures = assemble_notebooks(
        plan, translations, Path(output_dir), db_bootstrap=bool(db_aliases)
    )
    for failure in failures:
        record_needs_human(
            state_dir, phase=9, task="assembly", node_id=failure.node_id,
            reason="static_check_failed", detail=f"{failure.reason}: {failure.detail}",
        )
    _dump_json(
        state_dir / "sas_python_mapping.json", json.loads(mapping.model_dump_json())
    )
    notebooks = sorted({m.notebook_path for m in mapping.mappings})
    return {
        "retranslated": retranslated,
        "assembly_failures": len(failures),
        "notebooks": notebooks,
    }


# ── Fase 7: diagnóstico de mismatches de validación ─────────────────────────

def run_mismatch_diagnosis(
    state_dir: Path, workspace: Path, validation_report: dict
) -> list[dict]:
    """Diagnostica los resultados FAIL de la cascada con los 8 patrones de
    causa. Devuelve diagnósticos (dicts MismatchDiagnosis); NeedsHuman queda
    registrado en fase 7 y devuelve []."""
    state_dir = Path(state_dir)
    failed = [
        r for r in validation_report.get("results", [])
        if r.get("overall_status") in ("FAIL", "ERROR")
    ]
    if not failed:
        return []
    caller = runtime.get_caller(workspace)
    head = prompt_builder.header_line(
        {"tables": [str(r.get("target_table", "")) for r in failed]}
    )
    user = (
        f"{head}\n\n## Resultados fallidos de la cascada\n```json\n"
        + json.dumps(failed, ensure_ascii=False, default=str)[:40_000]
        + "\n```\n"
    )
    try:
        out = caller.call(
            task="mismatch_diagnosis",
            system_blocks=prompt_builder.build_diagnosis_system(),
            user_content=user,
            output_model=DiagnosesOut,
        )
        return [json.loads(d.model_dump_json()) for d in out.diagnoses]
    except NeedsHuman as exc:
        record_needs_human(
            state_dir, phase=7, task="mismatch_diagnosis", reason=exc.reason,
            detail=exc.detail, attempts=exc.attempts,
        )
        return []


# ── Fase 8: doc-writer ──────────────────────────────────────────────────────

def run_docs(state_dir: Path, output_dir: Path, workspace: Path) -> bool:
    """Escribe los 5 documentos con el doc-writer LLM. Ante NeedsHuman cae al
    template stub (gate 8 sigue verde en forma) y registra el item (gate 8
    bloquea por needs_human) — nunca silencio, nunca docs vacíos."""
    state_dir = Path(state_dir)
    docs_dir = Path(output_dir) / "docs"

    def _maybe(path: str, loader=_load_json):
        p = state_dir / path
        try:
            return loader(p) if p.exists() else None
        except Exception:
            return None

    context = {
        "flow_summary": _maybe("flow_summary.json"),
        "translation_plan": _maybe("translation_plan.json"),
        "sas_python_mapping": _maybe("sas_python_mapping.json"),
        "approved_improvements": _maybe(
            "approved_improvements.yaml",
            lambda p: yaml.safe_load(p.read_text(encoding="utf-8")),
        ),
        "validation_report": _maybe("validation_report.json"),
        "db_connections_aliases": [
            c.get("alias")
            for c in (_maybe(
                "db_connections.yaml",
                lambda p: yaml.safe_load(p.read_text(encoding="utf-8")),
            ) or {}).get("connections", [])
        ],
    }
    head = prompt_builder.header_line(
        {"project": (context.get("translation_plan") or {}).get("project_name", "")}
    )
    user = (
        f"{head}\n\n## Contexto de la migración\n```json\n"
        + json.dumps(context, ensure_ascii=False, default=str)[:60_000]
        + "\n```\n"
    )
    caller = runtime.get_caller(workspace)
    try:
        out = caller.call(
            task="docs",
            system_blocks=prompt_builder.build_docs_system(),
            user_content=user,
            output_model=DocsOut,
        )
    except NeedsHuman as exc:
        record_needs_human(
            state_dir, phase=8, task="docs", reason=exc.reason,
            detail=exc.detail, attempts=exc.attempts,
        )
        return False

    docs_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("README.md", out.readme),
        ("LINEAGE.md", out.lineage),
        ("DECISIONS.md", out.decisions),
        ("IMPROVEMENTS.md", out.improvements),
        ("RUNBOOK.md", out.runbook),
    ):
        (docs_dir / name).write_text(text.strip() + "\n", encoding="utf-8")
    return True
