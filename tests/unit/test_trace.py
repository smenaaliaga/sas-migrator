"""Trazas LLM locales (Etapa 6): TracingCaller registra CADA llamada —
outcome ok / needs_human / error — en state/llm_trace.jsonl, y summarize()
agrega por task."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from sas_migrator.llm.errors import NeedsHuman
from sas_migrator.llm.fake import FakeCaller
from sas_migrator.llm.trace import TracingCaller, summarize


class _Out(BaseModel):
    value: str = "ok"


def _read_trace(state_dir: Path) -> list[dict]:
    path = state_dir / "llm_trace.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _call(caller, task: str = "translation"):
    return caller.call(
        task=task,
        system_blocks=["prompt v1"],
        user_content="hola",
        output_model=_Out,
    )


def test_summarize_agrega_tokens_y_cache_por_task(tmp_path: Path) -> None:
    """cache_read=0 en produccion se descubrió leyendo el JSONL a mano; ahora
    el resumen lo agrega por task."""
    state = tmp_path / "state"
    caller = TracingCaller(_PricedInner(), state)
    _call(caller)
    _call(caller)
    entry = summarize(state)["by_task"]["translation"]
    assert entry["input_tokens"] == 2_000_000
    assert entry["output_tokens"] == 20_000
    assert entry["cache_read_tokens"] == 0 and entry["cache_creation_tokens"] == 0


# ── costos y presupuesto ────────────────────────────────────────────────────

class _PricedInner:
    """Caller fake con config de LlmConfig y usage como el real."""

    def __init__(self, budget: float = 0.0, model: str = "claude-opus-5"):
        from sas_migrator.core.config import LlmConfig

        self.config = LlmConfig(model=model, max_run_cost_usd=budget)
        self.last_usage: dict | None = None

    def call(self, **kwargs):
        self.last_usage = {
            "input_tokens": 1_000_000,  # $5 en opus → cada llamada cuesta ~$5.xx
            "output_tokens": 10_000,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        return _Out(value="ok")


def test_costo_por_llamada_y_acumulado_en_el_trace(tmp_path: Path) -> None:
    from sas_migrator.llm.costs import run_totals

    state = tmp_path / "state"
    caller = TracingCaller(_PricedInner(), state)
    _call(caller)
    rec = _read_trace(state)[0]
    assert rec["cost_usd"] > 5.0  # 1M in × $5/MTok + salida

    totals = run_totals(state)
    assert totals["priced_calls"] == 1 and totals["unpriced_calls"] == 0
    assert totals["cost_usd"] == rec["cost_usd"]
    assert totals["tokens_by_task"]["translation"] == 1_010_000


def test_presupuesto_corta_antes_de_llamar_y_sobrevive_reinicios(tmp_path: Path) -> None:
    """El tope es reanudable: corta gasto FUTURO, nunca invalida lo persistido."""
    import pytest

    from sas_migrator.llm.costs import BudgetExceeded

    state = tmp_path / "state"
    caller = TracingCaller(_PricedInner(budget=8.0), state)
    _call(caller)  # ~$5.05 gastados < 8 → pasa
    with pytest.raises(BudgetExceeded) as exc:
        _call(caller)  # el acumulado ya supera el tope → corta ANTES
        _call(caller)
    assert "max_run_cost_usd" in str(exc.value)
    assert len(_read_trace(state)) == 2, "una llamada ok + el intento que pasó antes del tope"

    # Reinicio del proceso: un TracingCaller nuevo relee el trace y el tope sigue
    caller2 = TracingCaller(_PricedInner(budget=8.0), state)
    with pytest.raises(BudgetExceeded):
        _call(caller2)


def test_modelo_sin_precio_se_declara_no_se_inventa(tmp_path: Path) -> None:
    from sas_migrator.llm.costs import run_totals

    state = tmp_path / "state"
    caller = TracingCaller(_PricedInner(model="modelo-desconocido-9000"), state)
    _call(caller)
    rec = _read_trace(state)[0]
    assert "cost_usd" not in rec
    totals = run_totals(state)
    assert totals["unpriced_calls"] == 1 and totals["cost_usd"] == 0.0
    assert totals["input_tokens"] == 1_000_000, "los tokens igual se cuentan"


def test_trace_records_ok(tmp_path: Path) -> None:
    state = tmp_path / "state"
    caller = TracingCaller(FakeCaller({"translation": {"value": "ok"}}), state)
    result = _call(caller)
    assert result.value == "ok"

    records = _read_trace(state)
    assert len(records) == 1
    rec = records[0]
    assert rec["task"] == "translation"
    assert rec["outcome"] == "ok"
    assert rec["output_model"] == "_Out"
    assert len(rec["prompt_hash"]) == 12
    assert rec["user_chars"] == 4
    assert "duration_ms" in rec


def test_trace_records_needs_human(tmp_path: Path) -> None:
    state = tmp_path / "state"

    class _Refusing:
        def call(self, **kwargs):
            raise NeedsHuman(task="translation", reason="refusal", attempts=1)

    caller = TracingCaller(_Refusing(), state)
    try:
        _call(caller)
        raise AssertionError("debía propagar NeedsHuman")
    except NeedsHuman:
        pass

    rec = _read_trace(state)[0]
    assert rec["outcome"] == "needs_human:refusal"
    assert rec["attempts"] == 1


def test_trace_records_transport_error(tmp_path: Path) -> None:
    state = tmp_path / "state"

    class _Broken:
        def call(self, **kwargs):
            raise ConnectionError("boom")

    caller = TracingCaller(_Broken(), state)
    try:
        _call(caller)
        raise AssertionError("debía propagar el error")
    except ConnectionError:
        pass

    assert _read_trace(state)[0]["outcome"] == "error:ConnectionError"


def test_prompt_hash_changes_with_prompt(tmp_path: Path) -> None:
    state = tmp_path / "state"
    caller = TracingCaller(FakeCaller({"translation": {"value": "ok"}}), state)
    caller.call(task="translation", system_blocks=["prompt v1"],
                user_content="x", output_model=_Out)
    caller.call(task="translation", system_blocks=["prompt v2"],
                user_content="x", output_model=_Out)
    records = _read_trace(state)
    assert records[0]["prompt_hash"] != records[1]["prompt_hash"]


def test_summarize_aggregates_by_task(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fake = FakeCaller({"translation": {"value": "ok"}, "docs": {"value": "ok"}})
    caller = TracingCaller(fake, state)
    _call(caller, "translation")
    _call(caller, "translation")
    _call(caller, "docs")

    class _Refusing:
        def call(self, **kwargs):
            raise NeedsHuman(task="matching", reason="refusal", attempts=1)

    try:
        _call(TracingCaller(_Refusing(), state), "matching")
    except NeedsHuman:
        pass

    summary = summarize(state)
    assert summary["calls"] == 4
    assert summary["by_task"]["translation"]["ok"] == 2
    assert summary["by_task"]["docs"]["calls"] == 1
    assert summary["by_task"]["matching"]["needs_human"] == 1


def test_summarize_empty_without_trace(tmp_path: Path) -> None:
    assert summarize(tmp_path) == {"calls": 0, "by_task": {}}
