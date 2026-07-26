"""Servidor MCP — tools delgadas sobre MigrationSession (ADR-0005).

Un servidor por workspace (``sas-migrator serve --workspace ...``), transporte
stdio. Las tools NO contienen lógica: delegan en la misma `MigrationSession`
que usa la CLI, así ambos frentes ejecutan exactamente el mismo camino.

`authorize_execution` e `iterate` existen desde ya (contrato estable para
clientes) pero responden `not_available` honesto: la ejecución de notebooks
llega en la Etapa 5 y la iteración post-migración en la Etapa 7. Nunca fingen
éxito ni escriben nada.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sas_migrator.service import MigrationSession

SERVER_NAME = "sas-migrator"


def _dump(result) -> dict:
    return result.model_dump(mode="json")


def build_tools(session: MigrationSession) -> dict[str, Callable[..., Any]]:
    """Las 7 tools del contrato MCP, como funciones puras sobre la sesión.

    Separadas de FastMCP para poder testearlas sin transporte.
    """

    def start_migration(egp: str | None = None, stub_mode: bool = False) -> dict:
        """Inicia la migración del workspace (fase 0 en adelante). Se detiene
        en la primera pregunta de entrevista, gate bloqueado o el final."""
        try:
            return _dump(session.start(Path(egp) if egp else None, stub_mode=stub_mode))
        except FileNotFoundError as exc:
            return {"status": "error", "message": str(exc)}

    def status() -> dict:
        """Estado actual: fase, gates completados y entrevista pendiente."""
        return _dump(session.status())

    def get_pending_question() -> dict | None:
        """La tarjeta de entrevista pendiente (InterviewCard), o null."""
        card = session.pending()
        return card.model_dump(mode="json") if card is not None else None

    def answer(card_id: str, answers: list[dict], free_text: str = "") -> dict:
        """Responde la tarjeta pendiente y avanza hasta el próximo interrupt,
        gate o final. `answers`: [{question_id, value}]."""
        payload = {"card_id": card_id, "answers": answers, "free_text": free_text}
        return _dump(session.answer(payload))

    def approve_plan(approved: bool, comment: str = "") -> dict:
        """Aprueba o rechaza el plan de traducción (tarjeta plan_approval)."""
        card = session.pending()
        if card is None or card.card_id != "plan_approval":
            return {
                "status": "error",
                "message": (
                    "no hay una aprobación de plan pendiente; la tarjeta activa es "
                    f"{card.card_id if card else 'ninguna'} — usar answer/get_pending_question"
                ),
            }
        value = "Aprobar el plan" if approved else "Rechazar el plan"
        payload = {
            "card_id": "plan_approval",
            "answers": [{"question_id": "Q-PLAN-1", "value": value}],
            "free_text": comment,
        }
        return _dump(session.answer(payload))

    def authorize_execution() -> dict:
        """(Etapa 5) Autoriza la ejecución de notebooks contra datos reales."""
        return {
            "status": "not_available",
            "message": (
                "La ejecución de notebooks llega en la Etapa 5; la aprobación del "
                "plan es approve_plan."
            ),
        }

    def iterate(instruction: str) -> dict:
        """(Etapa 7) Itera sobre una migración completada (Fase 9)."""
        return {
            "status": "not_available",
            "message": "La iteración post-migración llega en la Etapa 7 (Fase 9).",
        }

    return {
        "start_migration": start_migration,
        "status": status,
        "get_pending_question": get_pending_question,
        "answer": answer,
        "approve_plan": approve_plan,
        "authorize_execution": authorize_execution,
        "iterate": iterate,
    }


def build_server(workspace: Path):
    """Construye el FastMCP con las 7 tools registradas."""
    from mcp.server.fastmcp import FastMCP

    session = MigrationSession(workspace)
    server = FastMCP(SERVER_NAME)
    for name, fn in build_tools(session).items():
        server.tool(name=name)(fn)
    return server


def serve_workspace(workspace: Path) -> None:
    """Entry point de `sas-migrator serve` — bloquea sirviendo por stdio."""
    build_server(workspace).run()
