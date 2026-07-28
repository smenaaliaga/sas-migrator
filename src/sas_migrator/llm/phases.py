"""Runners LLM por fase — la rama no-stub de los nodos del grafo.

Cada runner es función de (state_dir, workspace): lee artefactos de state/,
llama al caller estructurado y escribe los MISMOS artefactos que el stub, con
los mismos contratos que exigen los gates. Un NeedsHuman se registra en
state/needs_human.yaml (el gate de la fase bloquea) y el runner sigue con el
resto — nunca silencio, nunca un crash por contenido.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from sas_migrator.core.models.translation import Confidence, NodeTranslation
from sas_migrator.core.utils import fsio
from sas_migrator.core.utils.fsio import dump_json as _dump_json
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

# Techo de código SAS por llamada de traducción. Los modelos actuales tienen
# ventana de 200k tokens: 120k chars ≈ 30k tokens dejan lugar de sobra para el
# system, el plan y la respuesta. Un nodo más grande NO se corta — se parte por
# frontera de PROC/DATA (ver `split_sas_blocks`) y, si ni así entra, va a
# needs_human. Truncar en silencio produce traducciones que parecen completas
# y cubren una fracción del nodo.
MAX_CODE_CHARS = 120_000

# Reintentos de traducción por fallo de chequeo estático. El modelo recibe el
# motivo exacto del rechazo, así que un segundo intento tiene información nueva
# —no es la misma tirada otra vez—. Pasado el techo va a needs_human: insistir
# más gasta tokens en un nodo que el modelo no está resolviendo.
MAX_TRANSLATION_RETRIES = 2

# En el prompt de ANÁLISIS entran todos los nodos de un PFD a la vez, y el
# objetivo es revisar (clasificar, detectar mejoras), no reproducir el código.
# Ahí sí se recorta, pero el recorte se anuncia dentro del prompt.
ANALYSIS_EXCERPT_CHARS = 20_000


def _load_json(path: Path):
    # required=True: acá los artefactos los garantizan los gates previos — un
    # faltante es un bug de secuencia, no un estado tolerable.
    return fsio.load_json(path, required=True)


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
    """Código SAS COMPLETO del nodo. Nunca recorta: la decisión de qué hacer
    con un nodo que no entra en un prompt es de quien arma ese prompt."""
    path = state_dir / "nodes" / f"{node_id}.json"
    if not path.exists():
        return ""
    return str(_load_json(path).get("code") or "")


def _node_excerpt(state_dir: Path, node_id: str, limit: int) -> str:
    """Extracto para prompts de revisión, con el recorte declarado en el texto.

    Un modelo que recibe código cortado sin avisar cree estar viendo el nodo
    entero; si lo sabe, puede decirlo en su review.
    """
    code = _node_code(state_dir, node_id)
    if len(code) <= limit:
        return code
    faltan = len(code) - limit
    return (
        f"{code[:limit]}\n"
        f"/* ⚠ EXTRACTO: se omitieron {faltan} caracteres ({len(code)} en total). "
        f"No concluyas que el nodo termina acá. */"
    )


# Frontera de bloque SAS: el inicio de un PROC/DATA de primer nivel. Cortar en
# cualquier otro lado parte una sentencia al medio.
_SAS_BLOCK_START = re.compile(r"^[ \t]*(?:PROC|DATA)\b", re.IGNORECASE | re.MULTILINE)


def split_sas_blocks(code: str, limit: int) -> list[str]:
    """Parte el código en trozos de ``<= limit`` cortando SOLO entre bloques.

    Devuelve ``[]`` si un bloque individual ya excede el techo: en ese caso no
    hay corte honesto posible y el nodo debe ir a needs_human, no traducirse a
    medias.
    """
    if len(code) <= limit:
        return [code] if code else []

    cortes = [m.start() for m in _SAS_BLOCK_START.finditer(code)]
    if not cortes or cortes[0] > limit:
        return []  # ni el preámbulo entra: sin frontera utilizable
    bloques = [
        code[ini:fin]
        for ini, fin in zip([0, *cortes], [*cortes, len(code)], strict=True)
        if ini < fin
    ]
    if any(len(b) > limit for b in bloques):
        return []  # un solo PROC/DATA más grande que el techo

    trozos: list[str] = []
    actual = ""
    for bloque in bloques:
        if actual and len(actual) + len(bloque) > limit:
            trozos.append(actual)
            actual = bloque
        else:
            actual += bloque
    if actual:
        trozos.append(actual)
    return trozos


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
            "```sas\n"
            f"{_node_excerpt(state_dir, str(n['id']), ANALYSIS_EXCERPT_CHARS)}\n```"
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
        fsio.dump_yaml(state_dir / "improvements_proposed.yaml", doc)
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
        fsio.dump_yaml(state_dir / "column_mapping.yaml", {"mappings": []})
    return {"mappings": len(mappings), "llm": True}


# ── Fase 6: traducción por nodo + ensamblado ────────────────────────────────

def merge_translations(partes: list[NodeTranslation]) -> NodeTranslation:
    """Fusiona las traducciones de las partes de un nodo en una sola.

    Las celdas se concatenan en orden (el orden de ejecución del SAS original);
    los imports se deduplican conservando la primera aparición. La confianza es
    la MÍNIMA de las partes: un nodo partido no es más confiable que su tramo
    más dudoso.
    """
    if len(partes) == 1:
        return partes[0]

    base = partes[0]
    imports: list[str] = []
    for parte in partes:
        for linea in parte.imports:
            if linea not in imports:
                imports.append(linea)

    orden = [Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH]
    peor = min(partes, key=lambda p: orden.index(p.confidence)).confidence

    warnings = [
        f"NODO PARTIDO: se tradujo en {len(partes)} partes por tamaño; el corte "
        "se hizo entre bloques PROC/DATA. Revisar que las variables compartidas "
        "entre partes se sigan llamando igual."
    ]
    for i, parte in enumerate(partes, start=1):
        warnings.extend(f"[parte {i}/{len(partes)}] {w}" for w in parte.warnings)

    return base.model_copy(update={
        "imports": imports,
        "cells": [c for parte in partes for c in parte.cells],
        "confidence": peor,
        "warnings": warnings,
    })


def _translate_node(
    caller, *, system: list[str], target: dict, code: str, db_aliases: list[str],
    iteration_note: str | None = None,
) -> NodeTranslation:
    """Traduce un nodo, partiéndolo por bloques si no entra en un prompt.

    Lanza ``NeedsHuman`` si el código no admite un corte honesto: mejor bloquear
    el gate que entregar un notebook que cubre parte del nodo sin decirlo.
    """
    trozos = [code] if len(code) <= MAX_CODE_CHARS else split_sas_blocks(code, MAX_CODE_CHARS)
    if not trozos:
        raise NeedsHuman(
            task="translation",
            reason="node_code_too_large",
            attempts=0,
            detail=(
                f"{len(code)} caracteres de SAS y ningún corte posible en frontera "
                f"PROC/DATA bajo {MAX_CODE_CHARS}: hay un bloque indivisible más "
                "grande que el techo. Partir el nodo a mano en el .egp o subir "
                "MAX_CODE_CHARS."
            ),
        )

    partes: list[NodeTranslation] = []
    for i, trozo in enumerate(trozos, start=1):
        nota = iteration_note
        if len(trozos) > 1:
            # El modelo debe saber que ve un tramo: si no, "completa" el código
            # que no ve, o declara terminado un nodo que sigue.
            aviso = (
                f"Este es el tramo {i} de {len(trozos)} del nodo, cortado entre "
                "bloques PROC/DATA. Traduce SOLO este tramo; los DataFrames que "
                "vengan de tramos anteriores ya existen en el notebook y los "
                "posteriores continúan después. No agregues imports repetidos ni "
                "recrees tablas de otros tramos."
            )
            nota = f"{aviso}\n\n{nota}" if nota else aviso
        partes.append(caller.call(
            task="translation",
            system_blocks=system,
            user_content=prompt_builder.build_translation_user(
                target, trozo, db_aliases, iteration_note=nota
            ),
            output_model=NodeTranslation,
        ))
    return merge_translations(partes)


def _load_valid_translations(trans_dir: Path) -> dict[str, NodeTranslation]:
    """Traducciones persistidas que HOY pasan el chequeo estático.

    Retomar una corrida no debe heredar la basura de corridas viejas: un .json
    que no pasa el gate no se registra, así el nodo se vuelve a traducir en vez
    de quedar cacheado como hecho. No se borra el archivo — sirve para mirar qué
    salió mal, y la próxima traducción lo sobrescribe.
    """
    from sas_migrator.core.assembly.notebook import check_node_translation

    translations: dict[str, NodeTranslation] = {}
    if not trans_dir.exists():
        return translations
    for path in sorted(trans_dir.glob("*.json")):
        nt = NodeTranslation.model_validate(_load_json(path))
        if check_node_translation(nt) is None:
            translations[nt.node_id] = nt
    return translations


def _translate_checked(
    caller, *, system: list[str], target: dict, code: str, db_aliases: list[str],
    iteration_note: str | None = None,
) -> NodeTranslation:
    """Traduce un nodo y NO devuelve nada que no pase el chequeo estático.

    El gate del ensamblador es local al nodo, así que se puede correr acá — el
    momento en que todavía sirve de algo. Corrido solo al ensamblar, un nodo
    malo ya está persistido en ``state/translations/``, y el retomar-desde-disco
    lo da por hecho para siempre: se traduce mal una vez y no se reintenta nunca.

    El motivo del rechazo vuelve al modelo como instrucción del reintento.
    Agotados los intentos, ``NeedsHuman`` — que el caller registra en la cola.
    """
    from sas_migrator.core.assembly.notebook import check_node_translation

    nota = iteration_note
    plan_strategy = str(target.get("strategy") or "")
    for intento in range(1, MAX_TRANSLATION_RETRIES + 2):
        nt = _translate_node(
            caller, system=system, target=target, code=code,
            db_aliases=db_aliases, iteration_note=nota,
        )
        if plan_strategy and nt.strategy and nt.strategy != plan_strategy:
            motivo = (
                f"strategy_mismatch: devolviste strategy '{nt.strategy}' y el plan "
                f"aprobado para este nodo dice '{plan_strategy}'"
            )
        else:
            failure = check_node_translation(nt)
            if failure is None:
                return nt
            motivo = f"{failure.reason}: {failure.detail}"

        if intento > MAX_TRANSLATION_RETRIES:
            raise NeedsHuman(
                task="translation", reason="static_check_failed",
                attempts=intento, detail=motivo,
            )
        aviso = (
            "Tu traducción anterior de este nodo fue RECHAZADA por el chequeo "
            f"estático:\n\n    {motivo}\n\n"
            "Volvé a traducir el nodo completo corrigiendo eso. No dejes huecos: "
            "si algo no se puede resolver con la información disponible, la celda "
            "va con raise NotImplementedError('<qué falta>') y el motivo en "
            "warnings — nunca un valor vacío que el resto del código consuma."
        )
        nota = f"{aviso}\n\n{iteration_note}" if iteration_note else aviso
    raise AssertionError("inalcanzable")  # pragma: no cover


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
    translations = _load_valid_translations(trans_dir)

    for target in plan.get("targets", []):
        nid = str(target.get("node_id"))
        if nid in translations:
            continue
        try:
            nt = _translate_checked(
                caller, system=system, target=target,
                code=_node_code(state_dir, nid), db_aliases=db_aliases,
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

    translations = _load_valid_translations(trans_dir)

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
        try:
            nt = _translate_checked(
                caller, system=system, target=target,
                code=_node_code(state_dir, nid), db_aliases=db_aliases,
                iteration_note=iteration_note,
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
        fsio.atomic_write_text(docs_dir / name, text.strip() + "\n")
    return True
