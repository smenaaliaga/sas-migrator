"""Sub-grafo de iteración post-migración (Fase 9) — con su propio gate.

La Fase 9 dejó de ser honor system: cada iteración registra una
``IterationEntry`` obligatoria en ``state/iteration_log.json``, re-traduce
los nodos afectados (con la instrucción como contexto), re-ensambla,
re-audita y RE-CORRE la validación. El gate del sub-grafo exige la entry
completada con ``validation_result`` poblado, auditoría sin high y validación
sin FAIL — cerrar el ciclo sin validar es imposible por topología.
"""

from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from sas_migrator.core.utils.fsio import dump_json, load_json

LOG_FILE = "iteration_log.json"


class IterationState(TypedDict, total=False):
    workspace: str
    cycle: int
    request_type: str
    description: str
    affected_nodes: list[str]
    done: bool
    entry_id: str
    errors: list[str]
    notes: Annotated[list[str], operator.add]


def _load_log(state_dir: Path) -> dict:
    return load_json(state_dir / LOG_FILE) or {"iterations": []}


def _save_log(state_dir: Path, log: dict) -> None:
    dump_json(state_dir / LOG_FILE, log)


def next_cycle(state_dir: Path) -> int:
    """max+1, no len+1: borrar una entry a mano no puede hacer colisionar ids."""
    cycles = [
        int(e.get("cycle", 0))
        for e in _load_log(Path(state_dir)).get("iterations", [])
    ]
    return max(cycles, default=0) + 1


def last_in_progress_cycle(state_dir: Path) -> int | None:
    """Ciclo de la última entry que quedó a medias (corte durante iterate)."""
    stale = [
        int(e.get("cycle", 0))
        for e in _load_log(Path(state_dir)).get("iterations", [])
        if e.get("status") == "in_progress"
    ]
    return max(stale) if stale else None


def _defer_stale(state_dir: Path) -> list[int]:
    """Entries in_progress huérfanas → deferred, visibles. Nada queda a medias
    en silencio: arrancar un ciclo nuevo declara qué pasó con el anterior."""
    log = _load_log(state_dir)
    deferred: list[int] = []
    for e in log.get("iterations", []):
        if e.get("status") == "in_progress":
            e["status"] = "deferred"
            e["validation_result"] = e.get("validation_result") or None
            deferred.append(int(e.get("cycle", 0)))
    if deferred:
        _save_log(state_dir, log)
    return deferred


def iterate_apply(state: IterationState) -> dict:
    from sas_migrator.core.audit import run_audit
    from sas_migrator.core.config import load_project_config
    from sas_migrator.core.models.state import IterationEntry
    from sas_migrator.core.validation.cascade import run_cascade

    ws = Path(state["workspace"])
    st, out = ws / "state", ws / "output"
    cycle = state["cycle"]
    affected = list(state.get("affected_nodes") or [])

    entry = IterationEntry(
        id=f"IT-{cycle:03d}",
        cycle=cycle,
        request_type=state.get("request_type", "enhancement"),
        description=state.get("description", ""),
        affected_nodes=affected,
        status="in_progress",
    )
    # Upsert por cycle: el nodo se re-ejecuta al reanudar (mismo contrato que
    # ADR-0002) y no puede duplicar la entry que dejó a medias.
    log = _load_log(st)
    existing = next(
        (e for e in log["iterations"] if e.get("cycle") == cycle), None
    )
    if existing is not None:
        existing.update(json.loads(entry.model_dump_json()))
    else:
        log["iterations"].append(json.loads(entry.model_dump_json()))
    _save_log(st, log)

    notes: list[str] = []
    changes: list[str] = []
    if affected:
        from sas_migrator.llm.phases import retranslate_nodes

        counts = retranslate_nodes(st, out, ws, affected, entry.description)
        changes = counts["notebooks"]
        notes.append(
            f"re-traducidos {counts['retranslated']}/{len(affected)} nodo(s), "
            f"{counts['assembly_failures']} fallo(s) estáticos"
        )

    # Re-auditar y RE-CORRER la validación — condición de cierre del ciclo.
    run_audit(st, out)
    cfg = load_project_config(ws)
    ref_dir = st / "reference_outputs"
    if ref_dir.exists() and any(ref_dir.iterdir()) and (st / "db_connections.yaml").exists():
        _report, code = run_cascade(st, config=cfg)
        validation_result = "PASS" if code == 0 else "FAIL"
    else:
        # Sin insumos de comparación: la validación corrió en modo honesto —
        # WARN comunica "sin comparar contra referencia".
        validation_result = "WARN"
    notes.append(f"validación re-corrida: {validation_result}")

    log = _load_log(st)
    for e in log["iterations"]:
        if e.get("cycle") == cycle:
            e["changes_made"] = changes
            e["validation_result"] = validation_result
            e["status"] = "completed"
    _save_log(st, log)

    return {"entry_id": entry.id, "notes": notes}


def iteration_gate(state: IterationState) -> dict:
    from sas_migrator.core.utils.schema_validation import check_iteration_gate

    ws = Path(state["workspace"])
    passed, errors = check_iteration_gate(ws / "state", state["cycle"])
    note = (
        f"ciclo {state['cycle']} cerrado (gate de iteración en verde)"
        if passed
        else f"ciclo {state['cycle']} BLOQUEADO: {'; '.join(errors[:3])}"
    )
    return {"done": passed, "errors": errors, "notes": [note]}


def build_iteration_graph(checkpointer=None):
    g = StateGraph(IterationState)
    g.add_node("iterate_apply", iterate_apply)
    g.add_node("iteration_gate", iteration_gate)
    g.set_entry_point("iterate_apply")
    g.add_edge("iterate_apply", "iteration_gate")
    g.add_edge("iteration_gate", END)
    return g.compile(checkpointer=checkpointer)


def run_iteration(
    workspace: Path,
    description: str,
    request_type: str = "enhancement",
    affected_nodes: list[str] | None = None,
    *,
    checkpointer=None,
    resume: bool = False,
) -> dict:
    """Corre (o retoma) un ciclo de iteración y devuelve su resultado.

    Con ``checkpointer``, cada ciclo vive en su thread
    (``iteration-{cycle:03d}``) del MISMO sqlite que la migración base: un
    corte a mitad de la re-traducción se retoma con ``resume=True`` — la
    fase 9 era la única no reanudable del sistema. Arrancar un ciclo NUEVO
    con una entry in_progress colgada la pasa a ``deferred`` (visible, nunca
    silencio).
    """
    ws = Path(workspace).resolve()
    state_dir = ws / "state"
    graph = build_iteration_graph(checkpointer=checkpointer)

    if resume:
        cycle = last_in_progress_cycle(state_dir)
        if cycle is None:
            raise LookupError("no hay iteración in_progress para retomar")
        if checkpointer is None:
            raise LookupError(
                "retomar exige el checkpointer de la sesión (usar "
                "MigrationSession.iterate(resume=True))"
            )
        config = {"configurable": {"thread_id": f"iteration-{cycle:03d}"}}
        result = graph.invoke(None, config)
    else:
        deferred = _defer_stale(state_dir)
        cycle = next_cycle(state_dir)
        payload: IterationState = {
            "workspace": str(ws),
            "cycle": cycle,
            "request_type": request_type,
            "description": description,
            "affected_nodes": affected_nodes or [],
        }
        if checkpointer is not None:
            config = {"configurable": {"thread_id": f"iteration-{cycle:03d}"}}
            result = graph.invoke(payload, config)
        else:
            result = graph.invoke(payload)
        if deferred:
            result.setdefault("notes", [])
            result["notes"] = list(result["notes"]) + [
                f"ciclo(s) {deferred} quedaron deferred (estaban in_progress); "
                "retomalos con `iterate --resume` antes de que se pisen"
            ]
    return {
        "cycle": cycle,
        "entry_id": result.get("entry_id", f"IT-{cycle:03d}"),
        "done": bool(result.get("done")),
        "errors": list(result.get("errors") or []),
        "notes": list(result.get("notes") or []),
    }
