"""Errores del subsistema LLM.

`NeedsHuman` es el contrato central del DoD de la Etapa 4: tras el retry
acotado (o un refusal del modelo), el nodo queda marcado para revisión humana
— NUNCA silencio. Los errores de transporte (rate limit, 5xx, red) NO son
NeedsHuman: propagan, el nodo del grafo falla visible y el checkpointer
permite reanudar.
"""

from __future__ import annotations


class NeedsHuman(Exception):
    """El LLM no produjo un output utilizable dentro del presupuesto de intentos."""

    def __init__(self, *, task: str, reason: str, attempts: int, detail: str = ""):
        self.task = task
        self.reason = reason  # "validation_retries_exhausted" | "refusal"
        self.attempts = attempts
        self.detail = detail
        super().__init__(
            f"[{task}] necesita revisión humana: {reason} tras {attempts} intento(s)"
            + (f" — {detail[:200]}" if detail else "")
        )
