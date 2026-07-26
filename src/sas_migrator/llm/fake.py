"""FakeCaller — doble determinista del protocolo StructuredCaller.

Para tests y para el eval set en modo recorded (CI sin API key). Las
respuestas se registran por `task`:

- una instancia/dict → se devuelve siempre;
- una lista → respuestas secuenciales (testear el retry / múltiples llamadas);
- una Exception → se lanza (simular NeedsHuman, refusal, transporte);
- un callable → se invoca con `user_content` (respuestas por nodo).

Toda respuesta pasa por `output_model.model_validate` — un fake que no cumple
el contrato falla el test, no lo esconde.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class FakeCaller:
    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses = dict(responses or {})
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        *,
        task: str,
        system_blocks: list[str],
        user_content: str,
        output_model: type[T],
        max_tokens: int | None = None,
    ) -> T:
        self.calls.append(
            {
                "task": task,
                "system_blocks": list(system_blocks),
                "user_content": user_content,
                "output_model": output_model.__name__,
            }
        )
        if task not in self.responses:
            raise KeyError(f"FakeCaller sin respuesta registrada para task '{task}'")
        value = self.responses[task]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"FakeCaller agotó las respuestas de '{task}'")
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value) and not isinstance(value, BaseModel):
            value = value(user_content)
        if isinstance(value, Exception):
            raise value
        data = value if isinstance(value, dict) else value.model_dump()
        return output_model.model_validate(data)
