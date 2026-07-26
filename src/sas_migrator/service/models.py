"""Contratos de la capa de servicio — lo que ven CLI y tools MCP."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from sas_migrator.core.models.interview import InterviewCard


class SessionStatus(str, Enum):
    NOT_STARTED = "not_started"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class SessionResult(BaseModel):
    """Resultado de cualquier operación de sesión (JSON-serializable)."""

    status: SessionStatus
    phase: int = -1
    pending_card: InterviewCard | None = None
    gate_errors: list[str] = Field(default_factory=list)
    completed_phases: list[int] = Field(default_factory=list)
    # Mensajes de flujo estilo lean ("✅ Fase N completada") — los imprime el
    # cliente; el grafo no habla con el usuario.
    messages: list[str] = Field(default_factory=list)
