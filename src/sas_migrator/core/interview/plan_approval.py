"""Tarjeta de aprobación del plan de traducción (Fase 5).

Rechazar el plan no necesita lógica extra: ``user_approved`` queda en false y
el gate 5 bloquea solo (schema_validation exige user_approved == true).
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.interview._io import load_json
from sas_migrator.core.models.interview import InterviewCard, Question, QuestionType

PLAN_OPTIONS = ["Aprobar el plan", "Rechazar el plan"]


def build_plan_card(state_dir: Path) -> InterviewCard:
    plan = load_json(Path(state_dir) / "translation_plan.json") or {}
    targets = plan.get("targets", [])
    notebooks = sorted({t.get("notebook_path") for t in targets if t.get("notebook_path")})
    evidence = [
        f"translation_plan.json: {len(targets)} nodo(s) en {len(notebooks)} notebook(s)",
        f"nodos ignorados: {len(plan.get('ignored_nodes', []))}",
        f"mejoras globales: {len(plan.get('global_improvements', []))}",
        f"supuestos: {len(plan.get('assumptions', []))}",
    ]
    evidence.extend(f"notebook: {nb}" for nb in notebooks[:10])
    return InterviewCard(
        card_id="plan_approval",
        interview_type="plan_approval",
        phase=5,
        block_id="plan-approval",
        title="Aprobación del plan de traducción",
        transition="El plan está listo. Una sola decisión:",
        questions=[
            Question(
                id="Q-PLAN-1",
                text="¿Apruebas el plan de traducción?",
                question_type=QuestionType.APPROVAL,
                options=list(PLAN_OPTIONS),
                recommended_default="Aprobar el plan",
                evidence=evidence,
            )
        ],
    )
