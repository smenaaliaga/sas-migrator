"""Cliente LLM con structured outputs y retry acotado.

Contrato: `StructuredCaller.call(...)` devuelve una instancia VALIDADA del
modelo Pydantic pedido, o lanza `NeedsHuman` (≤ max_validation_retries
intentos de validación, o refusal). Los errores de transporte del SDK
(RateLimitError, APIStatusError, APIConnectionError) propagan tal cual — el
SDK ya reintenta 429/5xx con backoff por su cuenta.

`anthropic` se importa LAZY dentro de `AnthropicCaller.__init__`: importar
este módulo (o correr CI, que no instala el extra `llm`) nunca requiere el
SDK. El modelo va pineado por config (`project_config.yaml` → `llm.model`,
default claude-opus-5); sin parámetros de sampling (removidos del API).

`thinking` y `effort` también salen de la config y se resuelven POR TAREA, como
`model` y `max_tokens`. Ausentes, no se mandan y rige el default del modelo —
que no es el mismo entre familias: Haiku 4.5 no piensa si no se lo piden,
Sonnet 5 y Opus 4.7+ piensan por default. Dejar eso implícito hacía que cambiar
el ID del modelo cambiara el comportamiento en silencio.

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

        import threading

        self._anthropic = anthropic
        self.config = config or LlmConfig()
        # last_usage/last_model son POR THREAD: con traducción paralela, dos
        # llamadas concurrentes no pueden pisarse el usage que el trace lee.
        self._tls = threading.local()
        # Tareas cuya configuración ya se anunció (una vez por corrida, no por
        # llamada). Se comparte entre threads a propósito: con max_workers>1 el
        # anuncio de `translation` tiene que salir una sola vez, no ocho.
        self._config_anunciada: set[str] = set()
        self._client = self._build_client(anthropic, self.config)
        # "auto" arranca nativo y degrada a tool en el primer rechazo del
        # backend; el modo resuelto se conserva para el resto de la corrida.
        self._mode = "tool" if self.config.structured_mode == "tool" else "native"

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return getattr(self._tls, "usage", None)

    @last_usage.setter
    def last_usage(self, value: dict[str, Any] | None) -> None:
        self._tls.usage = value

    @property
    def last_model(self) -> str | None:
        return getattr(self._tls, "model", None)

    @last_model.setter
    def last_model(self, value: str | None) -> None:
        self._tls.model = value

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
                from sas_migrator.llm.env import describe_sources, user_env_hint

                raise RuntimeError(
                    "Falta ANTHROPIC_FOUNDRY_API_KEY. Es la key del recurso de "
                    "Azure AI Foundry (portal → Keys and Endpoint).\n"
                    f"{describe_sources()}\n{user_env_hint()}"
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

    # El API admite hasta 4 breakpoints de cache por request.
    MAX_CACHE_BREAKPOINTS = 4

    def _system_param(self, system_blocks: list[str]) -> list[dict[str, Any]] | None:
        if not system_blocks:
            return None
        # Prompt caching ESCALONADO: cada bloque system lleva su breakpoint
        # (≤4). Con uno solo en el último bloque, cambiar el bloque final
        # (contexto del proyecto) invalidaba también el prefijo compartido
        # (contrato + patrones + few-shot). Lo volátil viaja en el user.
        blocks: list[dict[str, Any]] = [{"type": "text", "text": b} for b in system_blocks]
        for block in blocks[-self.MAX_CACHE_BREAKPOINTS:]:
            block["cache_control"] = {"type": "ephemeral"}
        return blocks

    TOOL_NAME = "responder"
    # Sobre este tope de salida, AMBOS caminos (nativo y tool use) pasan a
    # streaming. 16000 es el máximo probado no-streaming en producción; por
    # encima, el SDK veta el request ("Streaming is required for operations
    # that may take longer than 10 minutes") antes de llamar a la API, y el
    # timeout HTTP por conexión ociosa deja de ser teórico.
    STREAM_THRESHOLD_TOKENS = 16000

    @classmethod
    def _needs_stream(cls, max_tokens: int) -> bool:
        return max_tokens > cls.STREAM_THRESHOLD_TOKENS

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
        model: str | None = None,
        thinking: str | None = None,
        effort: str | None = None,
    ) -> Any:
        """Una llamada al backend en el modo vigente, con degradación única.

        En `auto` se intenta el camino nativo y, si el backend no lo soporta, se
        conmuta a tool use para esta instancia y se reintenta en el acto: la
        degradación no consume un intento de validación.
        """
        kwargs: dict[str, Any] = {
            "model": model or self.config.model,
            "max_tokens": max_tokens,
            "messages": list(messages),
        }
        if system is not None:
            kwargs["system"] = system
        # Solo se mandan si la config los pidió: sin esto el default del modelo
        # es el que manda, que es el comportamiento histórico.
        if thinking:
            kwargs["thinking"] = {"type": thinking}
        if effort:
            # `effort` va DENTRO de output_config, no al tope. En el camino
            # nativo el SDK mergea este dict con el `format` que arma desde
            # `output_format`, así que conviven sin pisarse.
            kwargs["output_config"] = {"effort": effort}

        if self._mode == "tool":
            tool_kwargs = dict(
                kwargs,
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
            if self._needs_stream(max_tokens):
                with self._client.messages.stream(**tool_kwargs) as stream:
                    return stream.get_final_message()
            return self._client.messages.create(**tool_kwargs)

        try:
            if self._needs_stream(max_tokens):
                # get_final_message() devuelve un ParsedMessage: expone el mismo
                # `.parsed_output` que messages.parse(), así que _extract no
                # distingue de dónde vino la respuesta.
                with self._client.messages.stream(
                    **kwargs, output_format=output_model
                ) as stream:
                    return stream.get_final_message()
            return self._client.messages.parse(**kwargs, output_format=output_model)
        except self._anthropic.APIError as exc:
            if self.config.structured_mode != "auto" or not self._is_structured_unsupported(exc):
                raise
            self._mode = "tool"
            return self._invoke(
                messages=messages, system=system, output_model=output_model,
                max_tokens=max_tokens, model=model,
                thinking=thinking, effort=effort,
            )

    def _anunciar_config(
        self,
        task: str,
        model: str,
        max_tokens: int,
        thinking: str | None,
        effort: str | None,
    ) -> None:
        """Dice con qué se está llamando, la primera vez que se usa cada tarea.

        Sale de las MISMAS variables resueltas que viajan en el request, así que
        no puede mentir: si alguien cambia la resolución por tarea, el anuncio
        cambia solo. Y vive acá y no en cada fase porque así cubre las siete
        tareas sin que ninguna tenga que acordarse de anunciarse.

        Una vez por tarea, no por llamada: la fase 6 son cientos de llamadas con
        la misma configuración.
        """
        if task in self._config_anunciada:
            return
        self._config_anunciada.add(task)

        import sys

        # "—" y no "default": el valor real lo decide el modelo, y cada familia
        # decide distinto (Haiku 4.5 no piensa, Sonnet 5 sí). Escribir un valor
        # concreto que no mandamos sería inventar.
        partes = [
            model,
            f"max_tokens {max_tokens:,}",
            f"thinking {thinking or '—'}",
            f"effort {effort or '—'}",
        ]
        print(f"    LLM · {task}: " + " · ".join(partes), file=sys.stderr, flush=True)

    @staticmethod
    def _validate_repairing(payload: Any, output_model: type[T]) -> T:
        """Valida, y ante un campo contenedor entregado como string JSON, repara.

        Bug conocido de modelos chicos: devuelven ``cells`` (u otra lista) como
        '["..."]' — un string que ES la lista serializada. Sin esto, la
        validación falla las N veces del retry con el mismo error y el nodo cae
        a needs_human por un problema puramente mecánico.
        """
        import json as _json

        from pydantic import ValidationError

        try:
            return output_model.model_validate(payload)
        except ValidationError:
            if not isinstance(payload, dict):
                raise
            repaired = dict(payload)
            cambio = False
            for key, value in payload.items():
                if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
                    try:
                        parsed = _json.loads(value)
                    except _json.JSONDecodeError:
                        continue
                    if isinstance(parsed, (list, dict)):
                        repaired[key] = parsed
                        cambio = True
            if not cambio:
                raise
            return output_model.model_validate(repaired)

    def _extract(self, response: Any, output_model: type[T]) -> T:
        """Objeto validado desde la respuesta, según el modo que la produjo."""
        if self._mode == "tool":
            for block in getattr(response, "content", []) or []:
                if getattr(block, "type", None) == "tool_use":
                    return self._validate_repairing(block.input, output_model)
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
        # Sin reset, una llamada que falla en transporte deja el usage de la
        # llamada ANTERIOR y el trace le atribuye tokens que no consumió.
        self.last_usage = None
        resolved_max_tokens = (
            max_tokens
            or self.config.max_tokens_by_task.get(task)
            or self.config.max_tokens
        )
        # Modelo por tarea; last_model queda expuesto para que el trace (y el
        # costo) registren el modelo REAL de cada llamada, no el default.
        resolved_model = self.config.models_by_task.get(task) or self.config.model
        self.last_model = resolved_model
        # Razonamiento y esfuerzo, con la misma resolución por tarea: con
        # modelos mezclados, mandarle `effort` al que no lo soporta es un 400.
        resolved_thinking = (
            self.config.thinking_by_task.get(task) or self.config.thinking
        )
        resolved_effort = self.config.effort_by_task.get(task) or self.config.effort
        self._anunciar_config(
            task, resolved_model, resolved_max_tokens, resolved_thinking, resolved_effort
        )

        for attempts in range(1, retries + 1):
            try:
                response = self._invoke(
                    messages=messages, system=system, output_model=output_model,
                    max_tokens=resolved_max_tokens, model=resolved_model,
                    thinking=resolved_thinking, effort=resolved_effort,
                )
            except self._anthropic.APIError:
                raise  # transporte/API: visible, reanudable — jamás NeedsHuman
            except Exception as exc:
                # El veto del SDK a requests no-streaming largos NO es un error
                # de validación: reintentarlo produce el mismo veto instantáneo
                # N veces y disfraza un bug de configuración/código de
                # validation_retries_exhausted (pasó con 27 nodos en producción).
                if "Streaming is required" in str(exc):
                    raise RuntimeError(
                        f"el SDK exige streaming para max_tokens={resolved_max_tokens} "
                        "y este camino no lo usa — bug del migrador, no del contenido"
                    ) from exc
                # Validación del structured output dentro del SDK (sin response
                # utilizable que mostrar de vuelta).
                last_error = str(exc)
                messages.extend(self._correction_turns(None, last_error))
                continue

            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "cache_read_input_tokens": getattr(
                        usage, "cache_read_input_tokens", None
                    ),
                    "cache_creation_input_tokens": getattr(
                        usage, "cache_creation_input_tokens", None
                    ),
                }

            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "refusal":
                raise NeedsHuman(
                    task=task,
                    reason="refusal",
                    attempts=attempts,
                    detail="el modelo declinó la solicitud (stop_reason=refusal)",
                )
            if stop_reason == "max_tokens":
                # Reintentar es gastar lo mismo para truncar igual: el mismo
                # prompt con el mismo tope produce el mismo corte. A la cola
                # humana con el remedio en la mano.
                raise NeedsHuman(
                    task=task,
                    reason="output_truncated",
                    attempts=attempts,
                    detail=(
                        f"respuesta cortada por max_tokens={resolved_max_tokens} "
                        f"(stop_reason=max_tokens). Subí llm.max_tokens_by_task."
                        f"{task} en project_config.yaml"
                    ),
                )

            try:
                return self._extract(response, output_model)
            except Exception as exc:
                last_error = str(exc)
                messages.extend(self._correction_turns(response, last_error))

        raise NeedsHuman(
            task=task,
            reason="validation_retries_exhausted",
            attempts=attempts,
            detail=last_error,
        )

    def _correction_turns(self, response: Any, error: str) -> list[dict[str, Any]]:
        """Turnos del reintento de validación — CON la respuesta fallida.

        Un retry que no muestra la respuesta rechazada es la misma tirada otra
        vez: el modelo no sabe qué produjo ni qué corregir. En modo tool el
        turno assistant lleva el tool_use fallido y la corrección viaja como
        ``tool_result`` con ``is_error`` (requisito del API: todo tool_use
        exige su tool_result). En nativo, el texto del assistant si existe.
        """
        correction = (
            "La respuesta anterior no validó contra el schema requerido: "
            f"{error[:500]}. Responde únicamente con el objeto JSON corregido, "
            "sin texto adicional."
        )
        blocks = list(getattr(response, "content", None) or []) if response else []
        if self._mode == "tool":
            tool_use = next(
                (b for b in blocks if getattr(b, "type", None) == "tool_use"), None
            )
            if tool_use is not None and getattr(tool_use, "id", None):
                return [
                    {
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use", "id": tool_use.id,
                            "name": self.TOOL_NAME, "input": tool_use.input,
                        }],
                    },
                    {
                        "role": "user",
                        "content": [{
                            "type": "tool_result", "tool_use_id": tool_use.id,
                            "is_error": True, "content": correction,
                        }],
                    },
                ]
        text = "".join(
            getattr(b, "text", "") for b in blocks
            if getattr(b, "type", None) == "text"
        )
        if text.strip():
            return [
                {"role": "assistant", "content": text},
                {"role": "user", "content": correction},
            ]
        return [{"role": "user", "content": correction}]
