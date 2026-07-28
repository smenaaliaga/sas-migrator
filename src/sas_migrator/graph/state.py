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

    # True = pipeline completo con stubs deterministas, sin API key (tests,
    # golden runs, CI, smoke). False = entrevistas reales + nodos LLM reales.
    stub_mode: bool

    # Espacio para que los nodos dejen notas de ejecución (no decisiones).
    notes: Annotated[list[str], operator.add]

    # Decisión de la pausa sagrada (fase 7): la escribe phase7_authorize y la
    # lee phase7_execute_validate. Viaja por el estado —no por state/— porque
    # es exactamente lo que el checkpointer sabe persistir y rebobinar; el
    # TypedDict es total=False, así que checkpoints anteriores a este campo
    # siguen deserializando.
    execution_authorized: bool

    # La pregunta de entrevista pendiente NO vive aquí: interrupt() levanta
    # excepción (el nodo no retorna) y LangGraph ya expone el payload activo
    # vía graph.get_state(config).tasks[*].interrupts — un espejo en el estado
    # sería inevitablemente stale (decisión de la Etapa 3).
