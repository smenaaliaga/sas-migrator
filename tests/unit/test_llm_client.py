"""Cliente LLM: retry acotado → NeedsHuman, refusal, transporte propaga.

El SDK anthropic NO está instalado en CI (extra `llm` opcional): estos tests
inyectan un módulo fake en sys.modules. Que este archivo importe
`sas_migrator.llm.*` en el top-level ya prueba que el paquete no requiere el
SDK para importarse.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sas_migrator.core.config import LlmConfig, ProjectConfig
from sas_migrator.llm import runtime
from sas_migrator.llm.client import AnthropicCaller
from sas_migrator.llm.errors import NeedsHuman
from sas_migrator.llm.fake import FakeCaller


class Demo(BaseModel):
    x: int


def _ok(x: int) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", parsed_output=Demo(x=x))


def _invalid() -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", parsed_output=None)


def _refusal() -> SimpleNamespace:
    return SimpleNamespace(stop_reason="refusal", parsed_output=None)


def _install_fake_sdk(monkeypatch, script: list):
    mod = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class RateLimitError(APIError):
        pass

    calls: list[dict] = []

    class _Messages:
        def parse(self, **kwargs):
            calls.append(kwargs)
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class Anthropic:
        def __init__(self, **_kw):
            self.messages = _Messages()

    mod.APIError = APIError
    mod.RateLimitError = RateLimitError
    mod.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod, calls


# ── AnthropicCaller ─────────────────────────────────────────────────────────

def test_success_first_try_returns_validated_model(monkeypatch) -> None:
    _, calls = _install_fake_sdk(monkeypatch, [_ok(7)])
    caller = AnthropicCaller(LlmConfig())
    result = caller.call(
        task="demo", system_blocks=["contexto estable"], user_content="dato",
        output_model=Demo,
    )
    assert result == Demo(x=7)
    assert calls[0]["model"] == "claude-opus-5"
    # prompt caching: breakpoint en el último bloque system
    assert calls[0]["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # sin parámetros de sampling ni thinking
    assert "temperature" not in calls[0] and "thinking" not in calls[0]


def test_retry_bounded_then_needs_human(monkeypatch) -> None:
    _, calls = _install_fake_sdk(monkeypatch, [_invalid(), _invalid(), _invalid()])
    caller = AnthropicCaller(LlmConfig(max_validation_retries=3))
    with pytest.raises(NeedsHuman) as exc:
        caller.call(task="traduccion", system_blocks=[], user_content="c",
                    output_model=Demo)
    assert exc.value.reason == "validation_retries_exhausted"
    assert exc.value.attempts == 3
    assert len(calls) == 3
    # cada retry agrega un mensaje user correctivo
    assert len(calls[1]["messages"]) == 2
    assert "no validó" in calls[1]["messages"][-1]["content"]


def test_recovers_on_second_attempt(monkeypatch) -> None:
    _install_fake_sdk(monkeypatch, [_invalid(), _ok(3)])
    caller = AnthropicCaller(LlmConfig())
    assert caller.call(task="t", system_blocks=[], user_content="c",
                       output_model=Demo) == Demo(x=3)


def test_refusal_is_needs_human_not_crash(monkeypatch) -> None:
    _install_fake_sdk(monkeypatch, [_refusal()])
    caller = AnthropicCaller(LlmConfig())
    with pytest.raises(NeedsHuman) as exc:
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)
    assert exc.value.reason == "refusal"


def test_transport_errors_propagate(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    caller = AnthropicCaller(LlmConfig())
    caller._client.messages.parse = lambda **kw: (_ for _ in ()).throw(
        mod.RateLimitError("429")
    )
    with pytest.raises(mod.RateLimitError):
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)


def test_sdk_validation_exception_counts_as_attempt(monkeypatch) -> None:
    _install_fake_sdk(monkeypatch, [ValueError("schema"), ValueError("schema")])
    caller = AnthropicCaller(LlmConfig(max_validation_retries=2))
    with pytest.raises(NeedsHuman) as exc:
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)
    assert exc.value.attempts == 2


# ── FakeCaller ──────────────────────────────────────────────────────────────

def test_fake_caller_validates_and_sequences() -> None:
    fake = FakeCaller({
        "a": Demo(x=1),
        "b": [Demo(x=2), Demo(x=3)],
        "c": NeedsHuman(task="c", reason="refusal", attempts=1),
        "d": lambda user: Demo(x=len(user)),
    })
    assert fake.call(task="a", system_blocks=[], user_content="", output_model=Demo).x == 1
    assert fake.call(task="b", system_blocks=[], user_content="", output_model=Demo).x == 2
    assert fake.call(task="b", system_blocks=[], user_content="", output_model=Demo).x == 3
    with pytest.raises(NeedsHuman):
        fake.call(task="c", system_blocks=[], user_content="", output_model=Demo)
    assert fake.call(task="d", system_blocks=[], user_content="xy", output_model=Demo).x == 2
    with pytest.raises(KeyError):
        fake.call(task="zzz", system_blocks=[], user_content="", output_model=Demo)
    assert [c["task"] for c in fake.calls[:3]] == ["a", "b", "b"]


def test_fake_caller_rejects_contract_violations() -> None:
    from pydantic import ValidationError

    fake = FakeCaller({"a": {"x": "no-es-int"}})
    with pytest.raises(ValidationError):
        fake.call(task="a", system_blocks=[], user_content="", output_model=Demo)


# ── runtime ─────────────────────────────────────────────────────────────────

def test_runtime_override_wins(tmp_path) -> None:
    fake = FakeCaller({"t": Demo(x=9)})
    runtime.set_caller(fake)
    try:
        caller = runtime.get_caller(tmp_path)
        assert caller is fake
    finally:
        runtime.set_caller(None)


def test_llm_config_defaults_and_yaml(tmp_path) -> None:
    assert ProjectConfig().llm.model == "claude-opus-5"
    (tmp_path / "project_config.yaml").write_text(
        "llm:\n  model: claude-sonnet-5\n  max_validation_retries: 2\n",
        encoding="utf-8",
    )
    from sas_migrator.core.config import load_project_config

    cfg = load_project_config(tmp_path)
    assert cfg.llm.model == "claude-sonnet-5"
    assert cfg.llm.max_validation_retries == 2
