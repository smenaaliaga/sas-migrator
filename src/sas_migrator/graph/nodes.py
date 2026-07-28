"""Nodos del grafo — cada fase invoca funciones del core (determinista) o
stubs LLM (Etapa 1). Ningún nodo escribe migration_state a mano: el estado
del grafo lo persiste el checkpointer.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from sas_migrator.core import intake as core_intake
from sas_migrator.core import planning as core_planning
from sas_migrator.core.analysis import analyze as core_analyze
from sas_migrator.core.analysis import indexes as core_indexes
from sas_migrator.core.analysis import ledger as core_ledger
from sas_migrator.core.extractors.egp import extract_egp
from sas_migrator.core.utils.fsio import dump_json as _dump_json
from sas_migrator.graph import stubs
from sas_migrator.graph.state import MigrationGraphState


def _paths(state: MigrationGraphState) -> tuple[Path, Path, Path]:
    ws = Path(state["workspace"])
    return ws, ws / "state", ws / "output"


@contextlib.contextmanager
def _argv(args: list[str]):
    """Invoca mains argparse-based del core sin subprocess.

    Deuda anotada: los módulos con main(argv) implícito (indexes) deberían
    exponer una función build(state_dir); limpiar en Etapa 2.
    """
    old = sys.argv
    sys.argv = ["sas-migrator", *args]
    try:
        yield
    finally:
        sys.argv = old


# ── Fase 0: intake ───────────────────────────────────────────────────────────

def phase0_intake(state: MigrationGraphState) -> dict:
    ws, st, _ = _paths(state)
    st.mkdir(parents=True, exist_ok=True)
    catalog = core_intake.scan(ws / "input")
    _dump_json(st / "intake.json", catalog)
    return {"current_phase": 0, "notes": ["fase 0: intake.json generado"]}


# ── Fase 1: entrevista inicial (stub | interrupt) ───────────────────────────

def phase1_initial_interview(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    if state.get("stub_mode", True):
        stubs.stub_initial_interview(st)
        return {"current_phase": 1, "notes": ["fase 1: entrevista inicial (stub)"]}
    from sas_migrator.graph.interviews import run_initial_interview

    run_initial_interview(st)
    return {"current_phase": 1, "notes": ["fase 1: entrevista inicial (usuario)"]}


# ── Fase 2: extracción + análisis determinista + criterio (stub) ────────────

def phase2_analysis(state: MigrationGraphState) -> dict:
    ws, st, _ = _paths(state)
    egp = Path(state["egp_file"])

    extract_egp(egp, st)
    core_analyze.main(st)
    with _argv(["--state-dir", str(st)]):
        core_indexes.main()

    # Parser v2: placement + inputs/outputs recuperados + reporte comparativo.
    from sas_migrator.core.parser.enrich import enrich_state

    enrich_state(st)
    core_ledger.cmd_init(st)

    # Criterio del code-analyst: reviews, descripciones, fichas M-xxx.
    if state.get("stub_mode", True):
        stubs.stub_analysis_reviews(st)
        note = "fase 2: extracción + análisis + reviews (stub)"
    else:
        from sas_migrator.llm.phases import run_analysis

        counts = run_analysis(st, ws)
        note = (
            "fase 2: extracción + análisis + reviews (LLM) — "
            f"{counts['pfds_ok']}/{counts['pfds_total']} PFDs"
        )
    core_ledger.cmd_sync(st)

    return {"current_phase": 2, "notes": [note]}


# ── Fase 3: profiling + matching (stub) ──────────────────────────────────────

def phase3_profiling(state: MigrationGraphState) -> dict:
    ws, st, _ = _paths(state)
    from sas_migrator.core import profiling as core_profiling

    data_dir = ws / "input" / "data"
    profiles = []
    if data_dir.exists():
        for f in sorted(data_dir.iterdir()):
            if f.is_file():
                try:
                    profiles.append(core_profiling.profile_file(f))
                except Exception as exc:  # perfil fallido se reporta, no revienta
                    profiles.append({"file_path": str(f), "file_type": f.suffix.lstrip("."), "error": str(exc)})
    _dump_json(st / "profile_report.json", profiles)

    if state.get("stub_mode", True):
        stubs.stub_file_mapping(st)
        note = f"fase 3: {len(profiles)} perfil(es) + matching (stub)"
    else:
        from sas_migrator.llm.phases import run_matching

        counts = run_matching(st, ws)
        note = f"fase 3: {len(profiles)} perfil(es) + matching (LLM: {counts['mappings']})"
    return {"current_phase": 3, "notes": [note]}


# ── Fase 4: entrevista post-análisis (stub | interrupts) ────────────────────

def phase4_post_interview(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    if state.get("stub_mode", True):
        stubs.stub_post_analysis_interview(st)
        return {"current_phase": 4, "notes": ["fase 4: entrevista post-análisis (stub)"]}
    from sas_migrator.graph.interviews import run_post_analysis_interview

    counts = run_post_analysis_interview(st)
    return {
        "current_phase": 4,
        "notes": [
            "fase 4: entrevista post-análisis (usuario) — "
            f"{counts.get('excluded', 0)} exclusiones, "
            f"{counts.get('improvements', 0)} mejoras aprobadas, "
            f"{counts.get('placements_resolved', 0)} placements resueltos"
        ],
    }


# ── Fase 5: plan de traducción (determinista) + aprobación (stub | interrupt) ─

def phase5_plan(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    if state.get("stub_mode", True):
        plan = core_planning.build(st)
        _dump_json(st / "translation_plan.json", plan)
        stubs.stub_approve_plan(st)
        return {"current_phase": 5, "notes": ["fase 5: plan construido y aprobado (stub)"]}

    # Escritura previa al interrupt: solo si el plan aún no existe — el nodo se
    # re-ejecuta al reanudar y no debe pisar el archivo en cada replay.
    if not (st / "translation_plan.json").exists():
        _dump_json(st / "translation_plan.json", core_planning.build(st))
    from sas_migrator.graph.interviews import run_plan_approval

    approved = run_plan_approval(st)
    note = "aprobado por el usuario" if approved else "RECHAZADO por el usuario (gate 5 bloquea)"
    return {"current_phase": 5, "notes": [f"fase 5: plan construido; {note}"]}


# ── Fase 6: generación (traducción stub | LLM + ensamblador determinista) ───

def phase6_generation(state: MigrationGraphState) -> dict:
    ws, st, out = _paths(state)
    from sas_migrator.core.gen_run_all import write_run_all

    if state.get("stub_mode", True):
        stubs.stub_generate_notebooks(st, out)
        note = "fase 6: notebooks generados (stub/ensamblador)"
    else:
        from sas_migrator.llm.phases import run_translation

        counts = run_translation(st, out, ws)
        note = (
            "fase 6: notebooks generados (LLM/ensamblador) — "
            f"{counts['translated']}/{counts['targets']} nodos, "
            f"{counts['assembly_failures']} fallos estáticos"
        )
    write_run_all(out)
    return {"current_phase": 6, "notes": [note]}


# ── Fase 7: verificación BD → autorización (pausa sagrada) → ejecución →
#    cascada de validación ─────────────────────────────────────────────────

def phase7_validation(state: MigrationGraphState) -> dict:
    ws, st, out = _paths(state)
    if state.get("stub_mode", True):
        stubs.stub_validation_report(st)
        return {"current_phase": 7, "notes": ["fase 7: validación not_applicable (stub)"]}

    from sas_migrator.core.config import load_project_config
    from sas_migrator.core.db.verify_tables import verify
    from sas_migrator.core.gen_run_all import discover_notebooks
    from sas_migrator.core.validation.references import stage_references

    cfg = load_project_config(ws)
    notes: list[str] = []

    # 1. Paso 4 de B4b: verificación de tablas contra la BD (idempotente).
    vreport, vcode = verify(st, cfg)
    if vreport.get("status") not in ("not_applicable",):
        notes.append(f"verificación de tablas: {vreport.get('status')}")

    # 2. Referencias SAS staged (byte-idempotente; puede correr antes del interrupt).
    staged = stage_references(st, ws / "input" / "data", st / "reference_outputs")

    # 3. Autorización de ejecución — esa pausa es sagrada.
    notebooks = discover_notebooks(out)
    authorized = False
    if notebooks:
        from sas_migrator.core.interview.execution import AUTHORIZE, build_execution_card
        from sas_migrator.graph.interviews import ask

        answer = ask(build_execution_card(st))
        authorized = any(a.value == AUTHORIZE for a in answer.answers)

    exec_summary: dict | None = None
    if authorized:
        if vreport.get("status") == "blocked_missing_targets":
            notes.append(
                "ejecución NO corrida: faltan tablas DESTINO (ver table_verification.json)"
            )
        else:
            from sas_migrator.core.execution import execute_notebooks, resolve_db_url

            exec_report = execute_notebooks(
                out, notebooks, db_url=resolve_db_url(st, cfg)
            )
            _dump_json(st / "execution_report.json", exec_report)
            exec_summary = {
                "passed": exec_report["passed"],
                "failed": exec_report["failed"],
            }
            notes.append(
                f"ejecución: {exec_report['passed']}/{exec_report['total']} notebooks OK"
            )
    elif notebooks:
        notes.append("ejecución no autorizada por el usuario — notebooks sin correr")

    # 4. Cascada de validación (referencias + conexiones) o reporte honesto.
    if staged and (st / "db_connections.yaml").exists():
        from sas_migrator.core.validation.cascade import run_cascade

        report, code = run_cascade(st, config=cfg)
        report["mode"] = report.get("validation_mode", "full")
        report.setdefault("results", [])
        if exec_summary is not None:
            report["execution"] = exec_summary
        if report.get("failed") or report.get("errors"):
            from sas_migrator.llm.phases import run_mismatch_diagnosis

            report["diagnoses"] = run_mismatch_diagnosis(st, ws, report)
            notes.append(
                f"validación: {report.get('failed', 0)} FAIL / "
                f"{report.get('errors', 0)} ERROR — diagnóstico adjunto"
            )
        elif report["mode"] == "full":
            notes.append(f"validación: cascada PASS ({report.get('passed', 0)} tabla(s))")
        else:
            notes.append(f"validación: {report['mode']}")
        report["overall_status"] = (
            "PASS" if code == 0 else ("BLOCKED" if code == 3 else "FAIL")
        )
        report["notes"] = list(notes)
        _dump_json(st / "validation_report.json", report)
    else:
        report = _build_validation_report(st, staged, exec_summary, notes)
        _dump_json(st / "validation_report.json", report)
    return {"current_phase": 7, "notes": [f"fase 7: {'; '.join(notes) or 'sin insumos'}"]}


def _build_validation_report(
    st: Path, staged: list[str], exec_summary: dict | None, notes: list[str]
) -> dict:
    if staged:
        report = {
            "mode": "references_staged",
            "overall_status": "PASS",
            "results": [],
            "references": staged,
            "notes": list(notes),
        }
    else:
        report = {
            "mode": "not_applicable",
            "overall_status": "PASS",
            "results": [],
            "notes": list(notes) or ["sin referencias SAS ni tablas destino"],
        }
    if exec_summary is not None:
        report["execution"] = exec_summary
        if exec_summary.get("failed"):
            report["overall_status"] = "FAIL"
    return report


# ── Fase 8: documentación (stub | doc-writer LLM) ───────────────────────────

def phase8_docs(state: MigrationGraphState) -> dict:
    ws, st, out = _paths(state)
    if state.get("stub_mode", True):
        stubs.stub_docs(st, out)
        return {"current_phase": 8, "notes": ["fase 8: docs generados (stub)"]}

    from sas_migrator.llm.phases import run_docs

    ok = run_docs(st, out, ws)
    if not ok:
        # Fallback al template para que los archivos existan; el item
        # needs_human ya quedó registrado y el gate 8 bloquea igual.
        stubs.stub_docs(st, out)
    note = "fase 8: docs redactados (doc-writer)" if ok else \
        "fase 8: docs template (doc-writer needs_human)"
    return {"current_phase": 8, "notes": [note]}


# ── Cierre ───────────────────────────────────────────────────────────────────

def finish(state: MigrationGraphState) -> dict:
    return {"current_phase": 9, "done": True, "notes": ["fase 9: migración base completa"]}
