"""Resolución del caller LLM en runtime — sin pasar por el estado del grafo.

El estado del grafo va al checkpointer (sqlite): no puede transportar objetos
cliente. Los nodos del grafo piden el caller aquí, SOLO dentro de la rama
no-stub; los tests inyectan un FakeCaller con `set_caller`.
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.llm.client import StructuredCaller

_override: StructuredCaller | None = None


def set_caller(caller: StructuredCaller | None) -> None:
    """Inyecta un caller (tests / evals). None restaura el comportamiento real."""
    global _override
    _override = caller


def caller_injected() -> bool:
    """True si hay un caller inyectado: nadie va a pedir credencial.

    Lo consulta el preflight de `run`: con un FakeCaller puesto (tests, evals,
    replay) una corrida no-stub es perfectamente válida sin key, y exigirla
    sería un chequeo que solo sabe rechazar corridas que sí funcionan.
    """
    return _override is not None


def get_caller(workspace: Path | str) -> StructuredCaller:
    if _override is not None:
        return _override
    from sas_migrator.core.config import load_project_config
    from sas_migrator.llm.client import AnthropicCaller
    from sas_migrator.llm.env import load_env
    from sas_migrator.llm.trace import TracingCaller

    # Antes de construir el cliente: es acá donde las credenciales pasan a hacer
    # falta, y solo en la rama no-stub (los tests inyectan su caller y no llegan).
    load_env(workspace)
    caller = AnthropicCaller(load_project_config(workspace).llm)
    return TracingCaller(caller, Path(workspace) / "state")
