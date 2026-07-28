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

El backend se elige con `llm.provider` (`anthropic` | `foundry`); las
credenciales salen del entorno o de un `.env` (ver `llm/env.py`), nunca del
YAML. Un proveedor que no sea Claude implicaría una clase nueva que cumpla
`StructuredCaller`, no un parámetro más acá: el contrato es la frontera.
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
    """Caller real sobre el SDK oficial de Anthropic (messages.parse).

    Sirve a los dos backends de Claude soportados —API directa y Microsoft
    Foundry— porque después de construido el cliente la superficie es la misma
    (`messages.parse`, bloques `system` con `cache_control`). Lo único que
    cambia es la clase que se instancia y de dónde salen las credenciales.
    """

    def __init__(self, config: LlmConfig | None = None):
        try:
            import anthropic  # lazy: solo quien construye el caller necesita el SDK
        except ModuleNotFoundError as exc:  # pragma: no cover - mensaje de instalación
            raise RuntimeError(
                "El modo LLM real requiere el extra 'llm': "
                "pip install sas-migrator[llm] (y la credencial en el entorno o .env)."
            ) from exc

        self._anthropic = anthropic
        self.config = config or LlmConfig()
        self._client = self._build_client(anthropic, self.config)
        # "auto" arranca nativo y degrada a tool en el primer rechazo del
        # backend; el modo resuelto se conserva para el resto de la corrida.
        self._mode = "tool" if self.config.structured_mode == "tool" else "native"

    @staticmethod
    def _build_client(anthropic: Any, config: LlmConfig) -> Any:
        """Instancia el cliente del proveedor configurado.

        Falla temprano y con nombre propio: qué falta y dónde ponerlo. Un
        cliente mal construido acá se manifestaría recién en la fase 6, después
        de que el usuario aprobó el plan.
        """
        import os

        if config.provider == "anthropic":
            # Sin pre-chequeo de ANTHROPIC_API_KEY: el SDK resuelve la credencial
            # por varias vías (API key, ANTHROPIC_AUTH_TOKEN, perfil de `ant auth
            # login`). Exigir la variable rechazaría setups válidos; que decida el
            # SDK y traducimos su error a uno accionable.
            try:
                return anthropic.Anthropic(max_retries=config.max_transport_retries)
            except Exception as exc:
                from sas_migrator.llm.env import describe_sources

                raise RuntimeError(
                    "No se pudo autenticar contra la API de Anthropic "
                    f"({exc}). Definí ANTHROPIC_API_KEY en <workspace>/.env o "
                    "en el entorno. Para usar Azure: llm.provider: foundry en "
                    f"project_config.yaml.\n{describe_sources()}"
                ) from exc

        if config.provider == "foundry":
            client_cls = getattr(anthropic, "AnthropicFoundry", None)
            if client_cls is None:  # pragma: no cover - depende de la versión del SDK
                raise RuntimeError(
                    "El SDK instalado no expone AnthropicFoundry. Actualizá: "
                    "pip install -U anthropic"
                )
            api_key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
            if not api_key:
                from sas_migrator.llm.env import describe_sources

                raise RuntimeError(
                    "Falta ANTHROPIC_FOUNDRY_API_KEY. Es la key del recurso de "
                    "Azure AI Foundry (portal → Keys and Endpoint).\n"
                    f"{describe_sources()}\n"
                    "Ponela en <workspace>/.env, o exportala al entorno."
                )
            resource = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or config.foundry_resource
            if not resource:
                raise RuntimeError(
                    "Falta el recurso de Foundry: llm.foundry_resource en "
                    "project_config.yaml, o ANTHROPIC_FOUNDRY_RESOURCE en el "
                    "entorno. Es el <resource> de "
                    "https://<resource>.services.ai.azure.com"
                )
            return client_cls(
                api_key=api_key, resource=resource,
                max_retries=config.max_transport_retries,
            )

        raise RuntimeError(  # pragma: no cover - Literal lo impide en config válida
            f"llm.provider desconocido: {config.provider!r} (anthropic | foundry)"
        )

    def _system_param(self, system_blocks: list[str]) -> list[dict[str, Any]] | None:
        if not system_blocks:
            return None
        # Prompt caching: los bloques system son estables por diseño (las
        # tablas de patrones, convenciones del proyecto); el breakpoint va en
        # el último bloque y lo volátil viaja en el mensaje user.
        blocks: list[dict[str, Any]] = [{"type": "text", "text": b} for b in system_blocks]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    TOOL_NAME = "responder"

    @staticmethod
    def _is_structured_unsupported(exc: Exception) -> bool:
        """¿El backend rechazó los structured outputs del API?

        Los workspaces de Foundry sin la beta habilitada responden 400
        'structured_outputs not supported in your workspace'. Es una capacidad
        ausente, no un error de la petición: amerita degradar, no fallar.
        """
        return "structured_outputs" in str(exc)

    def _invoke(
        self,
        *,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | None,
        output_model: type[T],
        max_tokens: int,
    ) -> Any:
        """Una llamada al backend en el modo vigente, con degradación única.

        En `auto` se intenta el camino nativo y, si el backend no lo soporta, se
        conmuta a tool use para esta instancia y se reintenta en el acto: la
        degradación no consume un intento de validación.
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": list(messages),
        }
        if system is not None:
            kwargs["system"] = system

        if self._mode == "tool":
            return self._client.messages.create(
                **kwargs,
                tools=[{
                    "name": self.TOOL_NAME,
                    "description": (
                        "Entrega la respuesta estructurada. Es el ÚNICO medio de "
                        "responder: no escribas texto fuera de la herramienta."
                    ),
                    "input_schema": output_model.model_json_schema(),
                }],
                tool_choice={"type": "tool", "name": self.TOOL_NAME},
            )

        try:
            return self._client.messages.parse(**kwargs, output_format=output_model)
        except self._anthropic.APIError as exc:
            if self.config.structured_mode != "auto" or not self._is_structured_unsupported(exc):
                raise
            self._mode = "tool"
            return self._invoke(
                messages=messages, system=system, output_model=output_model,
                max_tokens=max_tokens,
            )

    def _extract(self, response: Any, output_model: type[T]) -> T:
        """Objeto validado desde la respuesta, según el modo que la produjo."""
        if self._mode == "tool":
            for block in getattr(response, "content", []) or []:
                if getattr(block, "type", None) == "tool_use":
                    return output_model.model_validate(block.input)
            raise ValueError("la respuesta no incluyó la llamada a la herramienta")

        parsed = response.parsed_output
        if parsed is None:
            raise ValueError("respuesta sin parsed_output")
        return output_model.model_validate(
            parsed if isinstance(parsed, dict) else parsed.model_dump()
        )

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
            try:
                response = self._invoke(
                    messages=messages, system=system, output_model=output_model,
                    max_tokens=max_tokens or self.config.max_tokens,
                )
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
                return self._extract(response, output_model)
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
