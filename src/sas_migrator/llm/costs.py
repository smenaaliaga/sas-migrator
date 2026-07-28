"""Precios y presupuesto de la corrida LLM.

Una migración real son cientos de llamadas durante horas: sin un acumulado de
costo, "cuánto llevamos gastado" no tiene respuesta, y sin un tope, un loop
desbocado gasta hasta que alguien mira. El acumulado se deriva SIEMPRE del
trace (state/llm_trace.jsonl) — sobrevive reinicios porque el trace es
append-only en disco.

La tabla de precios es por prefijo de modelo (USD por millón de tokens,
precios del API de Anthropic; Foundry factura las mismas tarifas vía
Marketplace). Un modelo que no matchea ningún prefijo NO se inventa: la
llamada cuenta como "sin precio" y el resumen lo declara. Override por
``llm.prices`` en project_config.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path

from sas_migrator.llm.trace import TRACE_FILE

# USD por MTok. cache_write = input × 1.25 (TTL 5m); cache_read = input × 0.1.
DEFAULT_PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
}


class BudgetExceeded(RuntimeError):
    """El tope llm.max_run_cost_usd se alcanzó ANTES de una llamada.

    Es un corte reanudable, no un NeedsHuman: el estado quedó consistente
    (checkpointer + traducciones persistidas). Subir el tope y `resume`.
    """


def prices_for(model: str, overrides: dict | None = None) -> dict[str, float] | None:
    """Tarifa del modelo por prefijo más largo; None si no hay precio conocido."""
    table = {**DEFAULT_PRICES_PER_MTOK, **(overrides or {})}
    best: tuple[int, dict[str, float]] | None = None
    for prefix, price in table.items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), dict(price))
    return best[1] if best else None


def usage_cost(model: str, usage: dict | None, overrides: dict | None = None) -> float | None:
    """USD de una llamada según su usage; None si el modelo no tiene precio."""
    if not usage:
        return None
    price = prices_for(str(model), overrides)
    if price is None:
        return None
    tokens = {
        "input": usage.get("input_tokens") or 0,
        "output": usage.get("output_tokens") or 0,
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "cache_write": usage.get("cache_creation_input_tokens") or 0,
    }
    return sum(tokens[k] * price.get(k, 0.0) for k in tokens) / 1_000_000


def run_totals(state_dir: Path, overrides: dict | None = None) -> dict:
    """Acumulado de la corrida releyendo el trace — sobrevive reinicios.

    Devuelve tokens por tipo, costo estimado, y cuántas llamadas quedaron sin
    precio (modelo desconocido) — ese número se DECLARA, no se esconde: un
    resumen que omite llamadas sin decirlo es un resumen que miente.
    """
    totals = {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "priced_calls": 0,
        "unpriced_calls": 0,
        "tokens_by_task": {},
    }
    path = Path(state_dir) / TRACE_FILE
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # línea cortada por un kill: no invalida el resto
        usage = rec.get("usage") or {}
        totals["input_tokens"] += usage.get("input_tokens") or 0
        totals["output_tokens"] += usage.get("output_tokens") or 0
        totals["cache_read_tokens"] += usage.get("cache_read_input_tokens") or 0
        totals["cache_write_tokens"] += usage.get("cache_creation_input_tokens") or 0
        task = str(rec.get("task", "?"))
        spent = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        totals["tokens_by_task"][task] = totals["tokens_by_task"].get(task, 0) + spent
        cost = rec.get("cost_usd")
        if cost is None:
            cost = usage_cost(str(rec.get("model", "")), usage, overrides)
        if cost is None:
            if usage:
                totals["unpriced_calls"] += 1
            continue
        totals["cost_usd"] += float(cost)
        totals["priced_calls"] += 1
    return totals
