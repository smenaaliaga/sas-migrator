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
