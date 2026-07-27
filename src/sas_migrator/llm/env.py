"""Carga de credenciales desde `.env` — el único punto donde entran secretos.

Las credenciales no viven en `project_config.yaml` (ese archivo se commitea):
viajan por variables de entorno. Este módulo solo agrega la comodidad de leer
un `.env` en vez de exportarlas a mano en cada terminal.

Precedencia (la primera que define una variable gana):

1. El entorno del proceso — un `ANTHROPIC_API_KEY` ya exportado siempre manda,
   así que CI y `$env:...` puntual no se ven pisados por ningún archivo.
2. `<workspace>/.env` — por si una migración usa una cuenta distinta.
3. `.env` buscado hacia arriba desde el directorio actual (el del repo, caso
   normal: una sola key para todas las migraciones).

`python-dotenv` viene con el extra `llm`. Sin él esto es un no-op silencioso:
quien exporta las variables a mano no necesita la dependencia.
"""

from __future__ import annotations

from pathlib import Path

_loaded: set[Path] = set()


def load_env(workspace: Path | str | None = None) -> None:
    """Puebla el entorno desde los `.env` aplicables. Idempotente por workspace."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ModuleNotFoundError:  # pragma: no cover - depende del extra 'llm'
        return

    key = Path(workspace).resolve() if workspace is not None else Path.cwd()
    if key in _loaded:
        return
    _loaded.add(key)

    # override=False en ambas: el entorno real gana, y el .env del workspace
    # gana sobre el del repo por cargarse primero.
    if workspace is not None:
        ws_env = Path(workspace).resolve() / ".env"
        if ws_env.is_file():
            load_dotenv(ws_env, override=False)

    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=False)
