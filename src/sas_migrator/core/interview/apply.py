"""Escritores deterministas de decisiones de entrevista.

Los YAML de decisión los escribe SIEMPRE código (temp+rename), nunca un LLM ni
texto libre del usuario directamente. Se construyen instanciando los modelos
Pydantic del core: válidos contra schema por construcción. Los gates 1 y 4
reales son el contrato de salida (los tests lo verifican con ``check_gate``).
"""

from __future__ import annotations

import re
from pathlib import Path

from sas_migrator.core.interview import post_analysis
from sas_migrator.core.interview._io import (
    atomic_write_json,
    atomic_write_yaml,
    load_json,
    load_yaml,
)
from sas_migrator.core.models.analysis import PlacementDecisions, PlacementResolution
from sas_migrator.core.models.data import (
    ApiConnection,
    ApiConnectionMode,
    ApiConnections,
    ConnectionRole,
    DatabaseConnection,
    DatabaseConnections,
)
from sas_migrator.core.models.interview import (
    Answer,
    CardAnswers,
    InterviewCard,
    InterviewQA,
    QuestionBlock,
)
from sas_migrator.core.utils.node_io import dump_node, load_node

Collected = list[tuple[InterviewCard, CardAnswers]]

_POSTPONE = re.compile(r"\bposterg|\bpostpone", re.IGNORECASE)
_RENAME = re.compile(r"([A-Za-z_][\w]*)\s*(?:→|->)\s*([A-Za-z_][\w]*)")

_ROLE_BY_ANSWER = {
    "Solo lectura (fuente)": ConnectionRole.SOURCE,
    "Solo escritura (destino)": ConnectionRole.TARGET,
    "Lectura y escritura": ConnectionRole.BOTH,
}


def _model_dump(model) -> dict:
    import json as _json

    return _json.loads(model.model_dump_json())


def _value_of(ans: CardAnswers, question_id: str) -> str:
    for a in ans.answers:
        if a.question_id == question_id:
            return str(a.value)
    return ""


def _interview_yaml(interview_type: str, phase: int, collected: Collected) -> dict:
    """Reconstruye una InterviewQA desde las tarjetas + respuestas."""
    blocks: dict[str, QuestionBlock] = {}
    for card, ans in collected:
        block = blocks.get(card.block_id)
        if block is None:
            block = QuestionBlock(block_id=card.block_id, title=card.title)
            blocks[card.block_id] = block
        block.questions.extend(card.questions)
        block.answers.extend(ans.answers)
        if ans.free_text.strip():
            block.answers.append(
                Answer(question_id=f"{card.card_id}#texto-libre", value=ans.free_text.strip())
            )
    qa = InterviewQA(
        interview_type=interview_type, phase=phase, blocks=list(blocks.values())
    )
    return _model_dump(qa)


# ── Fase 1 ──────────────────────────────────────────────────────────────────

def apply_initial(state_dir: Path, collected: Collected) -> None:
    state_dir = Path(state_dir)
    atomic_write_yaml(
        state_dir / "initial_interview.yaml", _interview_yaml("initial", 1, collected)
    )


# ── Fase 4 ──────────────────────────────────────────────────────────────────

def decide_improvement(option: str, free_text: str) -> tuple[str, str]:
    """Decisión final de una ficha M-xxx.

    Regla léxica documentada: ``postponed`` SOLO existe si el usuario lo pidió
    en texto libre; jamás es una opción de la tarjeta.
    """
    text = free_text.strip()
    if option == "Aprobar":
        return "approved", text
    if option == "Rechazar":
        return "rejected", text
    if not option and text and _POSTPONE.search(text):
        return "postponed", text
    raise ValueError(
        f"ficha sin decisión resoluble (opción='{option}', texto='{text[:60]}') — "
        "una contrapropuesta debe re-presentarse como tarjeta modificada antes de aplicar"
    )


def parse_renames(free_text: str) -> dict[str, str]:
    """Renombres 'LIBREF → NUEVA_BASE' del paso 3 de B4b."""
    return {m.group(1).upper(): m.group(2).upper() for m in _RENAME.finditer(free_text)}


def _scope_decisions(state_dir: Path, collected: Collected) -> tuple[set[str], dict[str, str]]:
    """(nodos ignorados, translation_notes por nodo) desde las tarjetas B2."""
    ignored: set[str] = set()
    notes: dict[str, str] = {}
    summary = load_json(state_dir / "flow_summary.json") or {}
    flows = [f for f in summary.get("flows", []) if f.get("migratable_candidate")]

    for card, ans in collected:
        if card.card_id == "B2-scope:flows":
            selection = _value_of(ans, "Q-B2-1")
            if selection and selection.strip().lower() != "todos":
                chosen = {p.strip() for p in selection.split(";") if p.strip()}
                for flow in flows:
                    if post_analysis.flow_option(flow) not in chosen:
                        ignored.update(flow.get("node_ids", []))
            if _value_of(ans, "Q-B2-2") == "Excluirlos":
                ignored.update(post_analysis._orphan_nodes(state_dir))
        elif card.card_id.startswith("B2-scope:native:") or card.card_id == "B2-scope:queries":
            # El node_id viaja en el id de la pregunta, no en el de la tarjeta:
            # la tarjeta de consultas agrupa N nodos y la de tarea nativa, uno.
            for question in card.questions:
                nid = question.id.removeprefix("Q-B2-4-")
                choice = _value_of(ans, question.id)
                if choice == "Excluir de la migración":
                    ignored.add(nid)
                elif choice == "Traducir a mano":
                    notes[nid] = (
                        ans.free_text.strip() or "(a traducir a mano; sin descripción)"
                    )
    return ignored, notes


def _improvement_decisions(collected: Collected) -> dict[str, tuple[str, str]]:
    decisions: dict[str, tuple[str, str]] = {}
    for card, ans in collected:
        if not card.card_id.startswith("B5:"):
            continue
        imp_id = card.card_id.split(":")[1]
        option = _value_of(ans, card.questions[0].id)
        if option == "Explicar más":
            continue  # la decisión llega en la tarjeta de detalle
        decisions[imp_id] = decide_improvement(option, ans.free_text)
    return decisions


def _db_decisions(state_dir: Path, collected: Collected) -> tuple[bool, list[str], list[str]]:
    """(conectar a BD, prefijos confirmados BD, prefijos ruta/no-BD)."""
    connect = False
    confirmed: list[str] = []
    noise: list[str] = []
    for card, ans in collected:
        if card.card_id == "B4b:step1":
            connect = _value_of(ans, "Q-B4b-1") == "Sí, conectar a la base de datos"
        elif card.card_id.startswith("B4b:resolve:"):
            prefix = card.card_id.split("B4b:resolve:", 1)[1]
            choice = _value_of(ans, card.questions[0].id)
            if choice == "Es una base de datos":
                confirmed.append(prefix)
            elif choice == "Es una ruta de archivos (no BD)":
                noise.append(prefix)
            # "No sé" → el nodo queda ambiguous y visible; no se adivina.
    return connect, sorted(confirmed), sorted(noise)


def _cell_logging_decision(collected: Collected) -> bool:
    """B5b → ¿logging por celda en los notebooks? Tarjeta ausente = False."""
    for card, ans in collected:
        if card.card_id == "B5b-cell-logging":
            return _value_of(ans, "Q-B5b-1") == post_analysis.CELL_LOGGING_OPTIONS[0]
    return False


def _write_db_connections(
    state_dir: Path, collected: Collected, confirmed: list[str]
) -> None:
    ev = load_json(state_dir / "db_evidence.json") or {}
    role = ConnectionRole.SOURCE
    renames: dict[str, str] = {}
    for card, ans in collected:
        if card.card_id == "B4b:step2":
            role = _ROLE_BY_ANSWER.get(_value_of(ans, "Q-B4b-4"), ConnectionRole.SOURCE)
        elif card.card_id == "B4b:step3":
            renames = parse_renames(ans.free_text)

    tables_by_libref: dict[str, list[str]] = {}
    for lr in ev.get("librefs", []):
        if lr.get("source") == "libname_statement":
            tables_by_libref[str(lr.get("libref"))] = sorted(
                str(t.get("table")) for t in lr.get("tables", [])
            )
    for cause in post_analysis.unconfirmed_prefixes(state_dir):
        if cause["prefix"] in confirmed:
            tables_by_libref[str(cause["prefix"])] = sorted(map(str, cause["tables"]))

    connections = [
        DatabaseConnection(
            alias=libref,
            database=renames.get(libref, libref),
            role=role,
            tables=tables,
        )
        for libref, tables in sorted(tables_by_libref.items())
    ]
    atomic_write_yaml(
        state_dir / "db_connections.yaml",
        _model_dump(DatabaseConnections(connections=connections)),
    )


def _write_placement_decisions(
    state_dir: Path, confirmed: list[str], noise: list[str]
) -> int:
    """Re-clasifica los nodos ambiguos con los prefijos resueltos por B4b."""
    from sas_migrator.core.parser.placement import classify_placement, project_db_librefs
    from sas_migrator.core.parser.statements import parse_sas_code, resolve_db_engines

    index = load_json(state_dir / "nodes_index.json") or {}
    ambiguous = [n for n in index.get("nodes", []) if n.get("placement") == "ambiguous"]
    resolutions: list[PlacementResolution] = []

    if ambiguous and (confirmed or noise):
        engines = resolve_db_engines(Path(state_dir).parent)
        parses = {}
        for n in ambiguous:
            node = load_node(state_dir / "nodes" / f"{n['id']}.json") or {}
            parses[n["id"]] = parse_sas_code(node.get("code") or "")
        db_libs = project_db_librefs(parses, engines) | set(confirmed)

        for nid in sorted(parses):
            parse = parses[nid]
            decision = classify_placement(parse, db_libs, engines)
            if decision.placement != "ambiguous":
                resolutions.append(
                    PlacementResolution(
                        node_id=nid,
                        placement=decision.placement,
                        reason=f"prefijos confirmados como BD: {', '.join(confirmed)}",
                    )
                )
                continue
            unknown = {
                r.libref
                for r in [*parse.inputs, *parse.outputs]
                if r.libref != "WORK" and r.libref not in db_libs
            }
            if unknown and unknown <= set(noise):
                touches_db = any(
                    r.libref in db_libs for r in [*parse.inputs, *parse.outputs]
                )
                resolutions.append(
                    PlacementResolution(
                        node_id=nid,
                        placement="hybrid" if touches_db else "pandas",
                        reason=(
                            "prefijos confirmados como ruta de archivos: "
                            + ", ".join(sorted(unknown))
                        ),
                    )
                )
            # sino: sigue ambiguous ("No sé") — visible para la Etapa 4.

    doc = PlacementDecisions(
        decisions=resolutions,
        confirmed_prefixes=confirmed,
        noise_prefixes=noise,
    )
    atomic_write_yaml(state_dir / "placement_decisions.yaml", _model_dump(doc))
    return len(resolutions)


_IMPORT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_sdk_package(free_text: str) -> str:
    """Primer token con forma de nombre de import en el texto libre; '' si no hay."""
    match = _IMPORT_NAME.search(free_text or "")
    return match.group(0) if match else ""


def _api_decisions(collected: Collected) -> list[tuple[str, str, str]]:
    """[(host, mode, package)] desde las tarjetas B4c, en orden de tarjeta."""
    decisions: list[tuple[str, str, str]] = []
    for card, ans in collected:
        if not card.card_id.startswith("B4c:api:"):
            continue
        host = card.card_id.split("B4c:api:", 1)[1]
        choice = _value_of(ans, card.questions[0].id)
        if choice == "Usar una librería/SDK oficial (indico el paquete en texto libre)":
            decisions.append((host, "sdk", parse_sdk_package(ans.free_text)))
        else:
            decisions.append((host, "http", ""))
    return decisions


def _write_api_connections(state_dir: Path, collected: Collected) -> None:
    """B4c → api_connections.yaml. mode=http también se registra: la auditoría
    y el prompt necesitan saber que la decisión fue replicar, no que faltó."""
    decisions = _api_decisions(collected)
    if not decisions:
        return
    ev = load_json(state_dir / "http_evidence.json") or {}
    nodes_by_host = {
        str(h.get("host")): [str(n) for n in h.get("node_ids", [])]
        for h in ev.get("hosts", [])
    }
    connections = [
        ApiConnection(
            host=host,
            mode=ApiConnectionMode(mode),
            package=package,
            node_ids=nodes_by_host.get(host, []),
        )
        for host, mode, package in sorted(decisions)
    ]
    atomic_write_yaml(
        state_dir / "api_connections.yaml",
        _model_dump(ApiConnections(connections=connections)),
    )


def summarize_counts(state_dir: Path, collected: Collected) -> dict:
    """Conteos para la tarjeta B6 — misma fuente que los artefactos finales."""
    state_dir = Path(state_dir)
    ignored, _ = _scope_decisions(state_dir, collected)
    improvements = _improvement_decisions(collected)
    approved = sum(1 for status, _ in improvements.values() if status == "approved")
    index = load_json(state_dir / "nodes_index.json") or {}
    total = len(index.get("nodes", []))
    _, confirmed, noise = _db_decisions(state_dir, collected)
    unresolved = [
        c["prefix"]
        for c in post_analysis.unconfirmed_prefixes(state_dir)
        if c["prefix"] not in confirmed and c["prefix"] not in noise
    ]
    ev = load_json(state_dir / "analysis_evidence.json") or {}
    assumptions = len(unresolved) + (1 if ev.get("macro_variables") else 0)
    return {
        "migrate": max(total - len(ignored), 0),
        "excluded": len(ignored),
        "improvements": approved,
        "assumptions": assumptions,
    }


def apply_post_analysis(state_dir: Path, collected: Collected) -> dict:
    """Escribe TODOS los artefactos de decisión de la Fase 4.

    Se llama una sola vez, después del último interrupt del nodo (regla de
    re-ejecución: nada de escrituras de decisión a mitad de entrevista).
    """
    state_dir = Path(state_dir)

    # 1. La entrevista en sí (contrato del gate 4).
    atomic_write_yaml(
        state_dir / "post_analysis_interview.yaml",
        _interview_yaml("post_analysis", 4, collected),
    )

    # 2. Alcance → ignored_nodes.yaml + translation_notes.
    ignored, notes = _scope_decisions(state_dir, collected)
    atomic_write_yaml(state_dir / "ignored_nodes.yaml", {"ignored_nodes": sorted(ignored)})
    for nid, note in sorted(notes.items()):
        node_path = state_dir / "nodes" / f"{nid}.json"
        node = load_node(node_path)
        if node is not None:
            node["translation_notes"] = note
            dump_node(node_path, node)

    # 3. Mejoras → approved_improvements.yaml (toda M-xxx con decisión).
    decisions = _improvement_decisions(collected)
    proposed = load_yaml(state_dir / "improvements_proposed.yaml") or {}
    decided = []
    for item in proposed.get("improvements", []):
        item = dict(item)
        imp_id = str(item.get("id"))
        if imp_id not in decisions:
            raise ValueError(f"la ficha {imp_id} quedó sin decisión — la entrevista no cerró")
        status, comment = decisions[imp_id]
        item["status"] = status
        item["user_comment"] = comment
        decided.append(item)
    atomic_write_yaml(state_dir / "approved_improvements.yaml", {"improvements": decided})

    # 4. B4b → db_connections.yaml + placement_decisions.yaml.
    connect, confirmed, noise = _db_decisions(state_dir, collected)
    if connect:
        _write_db_connections(state_dir, collected, confirmed)
    resolved = _write_placement_decisions(state_dir, confirmed, noise)

    # 5. B4c → api_connections.yaml (solo si hubo tarjetas de conexión externa).
    _write_api_connections(state_dir, collected)

    # 6. B5b → cell_logging.yaml (preferencia de generación; la lee la Fase 5).
    atomic_write_yaml(
        state_dir / "cell_logging.yaml",
        {"cell_logging": {"enabled": _cell_logging_decision(collected)}},
    )

    counts = summarize_counts(state_dir, collected)
    counts["placements_resolved"] = resolved
    return counts


# ── Fase 5 ──────────────────────────────────────────────────────────────────

def apply_plan_approval(state_dir: Path, ans: CardAnswers) -> bool:
    """Registra la decisión sobre el plan. Rechazo → user_approved=False y el
    gate 5 bloquea solo (exige user_approved == true)."""
    state_dir = Path(state_dir)
    plan_path = state_dir / "translation_plan.json"
    plan = load_json(plan_path) or {}
    approved = _value_of(ans, "Q-PLAN-1") == "Aprobar el plan"
    plan["user_approved"] = approved
    note = "plan aprobado por el usuario" if approved else "plan RECHAZADO por el usuario"
    if ans.free_text.strip():
        note += f" — {ans.free_text.strip()}"
    plan.setdefault("notes", []).append(note)
    atomic_write_json(plan_path, plan)
    return approved
