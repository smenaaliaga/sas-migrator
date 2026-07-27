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


# ── structured outputs: nativo y degradación a tool use ─────────────────────

def _tool_response(**payload) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", input=payload)
    return SimpleNamespace(stop_reason="tool_use", content=[block])


def _no_tool_response() -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text="hola")]
    )


def _add_create(mod, script: list, calls: list):
    """Agrega messages.create (camino tool use) al SDK fake."""
    class _Messages(mod.Anthropic().messages.__class__):
        def create(self, **kwargs):
            calls.append(kwargs)
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class Anthropic:
        def __init__(self, **_kw):
            self.messages = _Messages()

    mod.Anthropic = Anthropic
    return mod


def test_tool_mode_forces_the_tool_and_validates(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    create_calls: list = []
    _add_create(mod, [_tool_response(x=11)], create_calls)

    caller = AnthropicCaller(LlmConfig(structured_mode="tool"))
    result = caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)

    assert result == Demo(x=11)
    sent = create_calls[0]
    assert sent["tool_choice"] == {"type": "tool", "name": "responder"}
    assert sent["tools"][0]["input_schema"]["properties"]["x"]["type"] == "integer"
    # nunca strict: es justo lo que el workspace de Foundry no soporta
    assert "strict" not in sent["tools"][0]


def test_auto_degrades_when_backend_lacks_structured_outputs(monkeypatch) -> None:
    """El 400 de Foundry conmuta a tool use sin gastar un intento de validación."""
    mod, parse_calls = _install_fake_sdk(monkeypatch, [])
    create_calls: list = []
    _add_create(mod, [_tool_response(x=4)], create_calls)

    caller = AnthropicCaller(LlmConfig(structured_mode="auto", max_validation_retries=1))
    caller._client.messages.parse = lambda **kw: (_ for _ in ()).throw(
        mod.APIError("400 structured_outputs not supported in your workspace")
    )

    assert caller.call(task="t", system_blocks=[], user_content="c",
                       output_model=Demo) == Demo(x=4)
    assert caller._mode == "tool"  # queda fijado para el resto de la corrida
    assert len(create_calls) == 1


def test_native_mode_does_not_degrade(monkeypatch) -> None:
    """Con structured_mode=native el 400 se propaga: el usuario pidió nativo."""
    mod, _ = _install_fake_sdk(monkeypatch, [])
    caller = AnthropicCaller(LlmConfig(structured_mode="native"))
    caller._client.messages.parse = lambda **kw: (_ for _ in ()).throw(
        mod.APIError("400 structured_outputs not supported in your workspace")
    )
    with pytest.raises(mod.APIError):
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)


def test_other_api_errors_never_degrade(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    caller = AnthropicCaller(LlmConfig(structured_mode="auto"))
    caller._client.messages.parse = lambda **kw: (_ for _ in ()).throw(
        mod.RateLimitError("429")
    )
    with pytest.raises(mod.RateLimitError):
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)
    assert caller._mode == "native"


def test_tool_mode_missing_tool_call_exhausts_to_needs_human(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    _add_create(mod, [_no_tool_response(), _no_tool_response()], [])

    caller = AnthropicCaller(LlmConfig(structured_mode="tool", max_validation_retries=2))
    with pytest.raises(NeedsHuman) as exc:
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)
    assert exc.value.reason == "validation_retries_exhausted"
    assert exc.value.attempts == 2


def test_tool_mode_refusal_is_still_needs_human(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    _add_create(mod, [SimpleNamespace(stop_reason="refusal", content=[])], [])

    caller = AnthropicCaller(LlmConfig(structured_mode="tool"))
    with pytest.raises(NeedsHuman) as exc:
        caller.call(task="t", system_blocks=[], user_content="c", output_model=Demo)
    assert exc.value.reason == "refusal"


# ── selección de proveedor / credenciales ───────────────────────────────────

def _add_foundry(mod, recorder: dict):
    class AnthropicFoundry:
        def __init__(self, **kw):
            recorder.update(kw)
            self.messages = mod.Anthropic().messages

    mod.AnthropicFoundry = AnthropicFoundry
    return mod


def test_foundry_provider_uses_foundry_client(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [_ok(1)])
    built: dict = {}
    _add_foundry(mod, built)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "azure-key")
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_RESOURCE", raising=False)

    caller = AnthropicCaller(LlmConfig(provider="foundry", foundry_resource="mi-recurso"))

    assert built == {"api_key": "azure-key", "resource": "mi-recurso"}
    # la superficie de llamada no cambia entre proveedores
    assert caller.call(task="t", system_blocks=[], user_content="c",
                       output_model=Demo) == Demo(x=1)


def test_foundry_resource_env_overrides_config(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    built: dict = {}
    _add_foundry(mod, built)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "recurso-de-entorno")

    AnthropicCaller(LlmConfig(provider="foundry", foundry_resource="recurso-de-yaml"))

    assert built["resource"] == "recurso-de-entorno"


def test_foundry_missing_key_names_the_variable(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    _add_foundry(mod, {})
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_FOUNDRY_API_KEY"):
        AnthropicCaller(LlmConfig(provider="foundry", foundry_resource="r"))


def test_foundry_missing_resource_is_explicit(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])
    _add_foundry(mod, {})
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_RESOURCE", raising=False)

    with pytest.raises(RuntimeError, match="foundry_resource"):
        AnthropicCaller(LlmConfig(provider="foundry"))


def test_foundry_on_old_sdk_says_to_upgrade(monkeypatch) -> None:
    _install_fake_sdk(monkeypatch, [])  # SDK sin AnthropicFoundry
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")

    with pytest.raises(RuntimeError, match="AnthropicFoundry"):
        AnthropicCaller(LlmConfig(provider="foundry", foundry_resource="r"))


def test_anthropic_provider_does_not_require_the_env_var(monkeypatch) -> None:
    """El SDK resuelve credenciales por varias vías (token, perfil de `ant auth
    login`): exigir ANTHROPIC_API_KEY rechazaría setups válidos."""
    _install_fake_sdk(monkeypatch, [_ok(5)])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    caller = AnthropicCaller(LlmConfig())

    assert caller.call(task="t", system_blocks=[], user_content="c",
                       output_model=Demo) == Demo(x=5)


def test_auth_failure_is_translated_to_actionable_error(monkeypatch) -> None:
    mod, _ = _install_fake_sdk(monkeypatch, [])

    def _boom(**_kw):
        raise Exception("could not resolve authentication method")

    mod.Anthropic = _boom
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicCaller(LlmConfig())


def test_env_loader_precedence(tmp_path, monkeypatch) -> None:
    """workspace/.env gana sobre el del repo; el entorno real gana sobre ambos."""
    pytest.importorskip("dotenv")
    from sas_migrator.llm import env as env_mod

    repo = tmp_path / "repo"
    ws = tmp_path / "ws"
    repo.mkdir()
    ws.mkdir()
    (repo / ".env").write_text(
        "DEL_REPO=repo\nCOMPARTIDA=repo\nYA_EXPORTADA=repo\n", encoding="utf-8"
    )
    (ws / ".env").write_text("COMPARTIDA=workspace\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("YA_EXPORTADA", "del-entorno")
    for name in ("DEL_REPO", "COMPARTIDA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(env_mod, "_loaded", set())

    env_mod.load_env(ws)

    import os

    assert os.environ["COMPARTIDA"] == "workspace"  # el workspace es más específico
    assert os.environ["DEL_REPO"] == "repo"  # el del repo completa lo que falta
    assert os.environ["YA_EXPORTADA"] == "del-entorno"  # nada pisa al entorno


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
