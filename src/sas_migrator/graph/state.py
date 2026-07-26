"""Estado del grafo de migración.

El contenido de los artefactos vive en disco (``state/``), igual que en v1 —
eso es lo auditable. El estado del grafo es control de flujo: dónde vamos,
qué dijeron los gates, y bajo qué modo corre. Lo escribe el runtime de
LangGraph vía checkpointer; ningún nodo (ni menos un LLM) lo redacta a mano.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GateRecord(TypedDict):
    phase: int
    passed: bool
    errors: list[str]


class MigrationGraphState(TypedDict, total=False):
    # Identidad de la corrida
    workspace: str  # raíz con input/, state/, output/
    egp_file: str  # ruta al .egp dentro de input/egp/

    # Control de flujo
    current_phase: int
    done: bool

    # Resultado del último gate evaluado (se sobreescribe en cada frontera)
    last_gate: GateRecord | None
    # Historial completo de gates (append-only vía reducer)
    gate_history: Annotated[list[GateRecord], operator.add]

    # Etapa 1: los nodos LLM son stubs deterministas. Cuando existan nodos LLM
    # reales, este flag permite seguir corriendo el pipeline completo sin API
    # key (tests, golden runs, CI).
    stub_mode: bool

    # Espacio para que los nodos dejen notas de ejecución (no decisiones).
    notes: Annotated[list[str], operator.add]

    # La pregunta de entrevista pendiente NO vive aquí: interrupt() levanta
    # excepción (el nodo no retorna) y LangGraph ya expone el payload activo
    # vía graph.get_state(config).tasks[*].interrupts — un espejo en el estado
    # sería inevitablemente stale (decisión de la Etapa 3).
