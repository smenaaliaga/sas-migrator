"""Capa de servicio compartida (Etapa 3).

`MigrationSession` es la ÚNICA vía de acceso al grafo con checkpointer: la CLI
la usa in-process y las tools del servidor MCP son wrappers delgados sobre
ella. Un solo camino de código testeado para ambos frentes (ADR-0005).
"""

from sas_migrator.service.models import SessionResult, SessionStatus
from sas_migrator.service.session import MigrationSession

__all__ = ["MigrationSession", "SessionResult", "SessionStatus"]
