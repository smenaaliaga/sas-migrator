"""Tarjeta de autorización de ejecución (Fase 7) — la pausa sagrada.

Los notebooks pueden escribir en la BD del proyecto: la ejecución NUNCA
ocurre sin autorización explícita del usuario. El default recomendado es NO
ejecutar (el camino fácil jamás dispara efectos irreversibles).
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.interview._io import load_json, load_yaml
from sas_migrator.core.models.interview import InterviewCard, Question, QuestionType

AUTHORIZE = "Autorizar ejecución"
DECLINE = "No ejecutar ahora"
EXECUTION_OPTIONS = [DECLINE, AUTHORIZE]


def build_execution_card(state_dir: Path) -> InterviewCard:
    state_dir = Path(state_dir)
    mapping = load_json(state_dir / "sas_python_mapping.json") or {}
    notebooks = sorted({
        str(m.get("notebook_path", ""))
        for m in mapping.get("mappings", [])
        if m.get("notebook_path")
    })
    evidence = [f"notebook: {nb}" for nb in notebooks[:10]]

    conns = load_yaml(state_dir / "db_connections.yaml") or {}
    aliases = [str(c.get("alias", "?")) for c in conns.get("connections", [])]
    if aliases:
        evidence.append(
            "db_connections.yaml: escribe en la(s) conexión(es) " + ", ".join(aliases)
        )
    else:
        evidence.append("sin conexiones de BD declaradas (ejecución solo local)")

    verification = load_json(state_dir / "table_verification.json") or {}
    if verification.get("status"):
        evidence.append(f"table_verification.json: {verification['status']}")

    return InterviewCard(
        card_id="execution_approval",
        interview_type="execution_approval",
        phase=7,
        block_id="execution",
        title="Autorización de ejecución de notebooks",
        transition="Los notebooks están listos. Ejecutarlos puede escribir en la BD:",
        questions=[
            Question(
                id="Q-EXEC-1",
                text=(
                    "¿Autorizas ejecutar los notebooks ahora (nbclient, en orden "
                    "topológico)? La escritura es idempotente por periodo, pero corre "
                    "contra la BD configurada."
                ),
                question_type=QuestionType.APPROVAL,
                options=list(EXECUTION_OPTIONS),
                recommended_default=DECLINE,
                evidence=evidence,
            )
        ],
    )
