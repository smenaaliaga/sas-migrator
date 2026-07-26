"""Cliente LLM con structured outputs y retry acotado.

Contrato: `StructuredCaller.call(...)` devuelve una instancia VALIDADA del
modelo Pydantic pedido, o lanza `NeedsHuman` (≤ max_validation_retries
intentos de validación, o refusal). Los errores de transporte del SDK
(RateLimitError, APIStatusError, APIConnectionError) propagan tal cual — el
SDK ya reintenta 429/5xx con backoff por su cuenta.

`anthropic` se importa LAZY dentro de `AnthropicCaller.__init__`: importar
este módulo (o correr CI, que no instala el extra `llm`) nunca requiere el
SDK. El modelo va pineado por config (`project_config.yaml` → `llm.model`,
default claude-opus-5); sin parámetros de sampling (removidos del API) y sin
configurar thinking (adaptive por default en los modelos actuales).
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from sas_migrator.core.config import LlmConfig
from sas_migrator.llm.errors import NeedsHuman

T = TypeVar("T", bound=BaseModel)


class StructuredCaller(Protocol):
    def call(
        self,
        *,
        task: str,
        system_blocks: list[str],
        user_content: str,
        output_model: type[T],
        max_tokens: int | None = None,
    ) -> T: ...


class AnthropicCaller:
    """Caller real sobre el SDK oficial de Anthropic (messages.parse)."""

    def __init__(self, config: LlmConfig | None = None):
        try:
            import anthropic  # lazy: solo quien construye el caller necesita el SDK
        except ModuleNotFoundError as exc:  # pragma: no cover - mensaje de instalación
            raise RuntimeError(
                "El modo LLM real requiere el extra 'llm': "
                "pip install sas-migrator[llm] (y ANTHROPIC_API_KEY en el entorno)."
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.config = config or LlmConfig()

    def _system_param(self, system_blocks: list[str]) -> list[dict[str, Any]] | None:
        if not system_blocks:
            return None
        # Prompt caching: los bloques system son estables por diseño (las
        # tablas de patrones, convenciones del proyecto); el breakpoint va en
        # el último bloque y lo volátil viaja en el mensaje user.
        blocks: list[dict[str, Any]] = [{"type": "text", "text": b} for b in system_blocks]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    def call(
        self,
        *,
        task: str,
        system_blocks: list[str],
        user_content: str,
        output_model: type[T],
        max_tokens: int | None = None,
    ) -> T:
        system = self._system_param(system_blocks)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        retries = max(1, self.config.max_validation_retries)
        last_error = ""
        attempts = 0

        for attempts in range(1, retries + 1):
            kwargs: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": max_tokens or self.config.max_tokens,
                "messages": list(messages),
                "output_format": output_model,
            }
            if system is not None:
                kwargs["system"] = system

            try:
                response = self._client.messages.parse(**kwargs)
            except self._anthropic.APIError:
                raise  # transporte/API: visible, reanudable — jamás NeedsHuman
            except Exception as exc:
                # Validación del structured output dentro del SDK.
                last_error = str(exc)
                messages.append(self._correction(last_error))
                continue

            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "cache_read_input_tokens": getattr(
                        usage, "cache_read_input_tokens", None
                    ),
                }

            if getattr(response, "stop_reason", None) == "refusal":
                raise NeedsHuman(
                    task=task,
                    reason="refusal",
                    attempts=attempts,
                    detail="el modelo declinó la solicitud (stop_reason=refusal)",
                )

            try:
                parsed = response.parsed_output
                if parsed is None:
                    raise ValueError("respuesta sin parsed_output")
                return output_model.model_validate(
                    parsed if isinstance(parsed, dict) else parsed.model_dump()
                )
            except Exception as exc:
                last_error = str(exc)
                messages.append(self._correction(last_error))

        raise NeedsHuman(
            task=task,
            reason="validation_retries_exhausted",
            attempts=attempts,
            detail=last_error,
        )

    @staticmethod
    def _correction(error: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                "La respuesta anterior no validó contra el schema requerido: "
                f"{error[:500]}. Responde únicamente con el objeto JSON corregido, "
                "sin texto adicional."
            ),
        }
