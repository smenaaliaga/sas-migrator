"""Nodos del grafo — cada fase invoca funciones del core (determinista) o
stubs LLM (Etapa 1). Ningún nodo escribe migration_state a mano: el estado
del grafo lo persiste el checkpointer.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from sas_migrator.core import intake as core_intake
from sas_migrator.core import planning as core_planning
from sas_migrator.core.analysis import analyze as core_analyze
from sas_migrator.core.analysis import indexes as core_indexes
from sas_migrator.core.analysis import ledger as core_ledger
from sas_migrator.core.extractors.egp import extract_egp
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


def _dump_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Fase 0: intake ───────────────────────────────────────────────────────────

def phase0_intake(state: MigrationGraphState) -> dict:
    ws, st, _ = _paths(state)
    st.mkdir(parents=True, exist_ok=True)
    catalog = core_intake.scan(ws / "input")
    _dump_json(st / "intake.json", catalog)
    return {"current_phase": 0, "notes": ["fase 0: intake.json generado"]}


# ── Fase 1: entrevista inicial (stub → interrupt en Etapa 3) ────────────────

def phase1_initial_interview(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    stubs.stub_initial_interview(st)
    return {"current_phase": 1, "notes": ["fase 1: entrevista inicial (stub)"]}


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

    # Criterio del code-analyst (stub): reviews, descripciones, fichas M-xxx.
    stubs.stub_analysis_reviews(st)
    core_ledger.cmd_sync(st)

    return {"current_phase": 2, "notes": ["fase 2: extracción + análisis + reviews (stub)"]}


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

    stubs.stub_file_mapping(st)
    return {"current_phase": 3, "notes": [f"fase 3: {len(profiles)} perfil(es) + matching (stub)"]}


# ── Fase 4: entrevista post-análisis (stub → interrupts en Etapa 3) ─────────

def phase4_post_interview(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    stubs.stub_post_analysis_interview(st)
    return {"current_phase": 4, "notes": ["fase 4: entrevista post-análisis (stub)"]}


# ── Fase 5: plan de traducción (determinista) + aprobación (stub) ───────────

def phase5_plan(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    plan = core_planning.build(st)
    _dump_json(st / "translation_plan.json", plan)
    stubs.stub_approve_plan(st)
    return {"current_phase": 5, "notes": ["fase 5: plan construido y aprobado (stub)"]}


# ── Fase 6: generación (stub = embrión del ensamblador) ─────────────────────

def phase6_generation(state: MigrationGraphState) -> dict:
    _, st, out = _paths(state)
    stubs.stub_generate_notebooks(st, out)
    return {"current_phase": 6, "notes": ["fase 6: notebooks generados (stub/ensamblador)"]}


# ── Fase 7: validación (modo sin insumos en stub) ────────────────────────────

def phase7_validation(state: MigrationGraphState) -> dict:
    _, st, _ = _paths(state)
    stubs.stub_validation_report(st)
    return {"current_phase": 7, "notes": ["fase 7: validación not_applicable (stub)"]}


# ── Fase 8: documentación (stub) ─────────────────────────────────────────────

def phase8_docs(state: MigrationGraphState) -> dict:
    _, st, out = _paths(state)
    stubs.stub_docs(st, out)
    return {"current_phase": 8, "notes": ["fase 8: docs generados (stub)"]}


# ── Cierre ───────────────────────────────────────────────────────────────────

def finish(state: MigrationGraphState) -> dict:
    return {"current_phase": 9, "done": True, "notes": ["fase 9: migración base completa"]}
