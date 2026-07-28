r"""Carga de credenciales desde `.env` — el único punto donde entran secretos.

Las credenciales no viven en `project_config.yaml` (ese archivo se commitea):
viajan por variables de entorno. Este módulo solo agrega la comodidad de leer
un `.env` en vez de exportarlas a mano en cada terminal.

Precedencia (la primera que define una variable gana):

1. El entorno del proceso — un `ANTHROPIC_API_KEY` ya exportado siempre manda,
   así que CI y `$env:...` puntual no se ven pisados por ningún archivo.
2. `<workspace>/.env` — por si una migración usa una cuenta distinta.
3. `.env` buscado hacia arriba desde el directorio actual (el del repo, cuando
   se corre desde ahí).
4. `~/.sas-migrator/.env` — la credencial de la MÁQUINA, último recurso.

El nivel 4 existe por el comando instalado global: `find_dotenv` sube desde el
cwd, y parado en `D:\Migraciones\proyecto` esa cadena nunca pasa por el repo
—son ramas hermanas del árbol—. Sin un lugar independiente del cwd, instalar
con pipx obliga a duplicar la key en cada workspace. Va último para que el
workspace y el repo siempre puedan pisarlo.

`python-dotenv` viene con el extra `llm`. Sin él esto es un no-op silencioso:
quien exporta las variables a mano no necesita la dependencia.
"""

from __future__ import annotations

from pathlib import Path

# Credencial a nivel máquina, independiente del directorio de trabajo.
USER_ENV = Path.home() / ".sas-migrator" / ".env"

_loaded: set[Path] = set()
# Qué se miró y si estaba — sin esto, "falta la key" no dice dónde ponerla.
_consulted: list[tuple[str, bool]] = []


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
        _consulted.append((str(ws_env), ws_env.is_file()))
        if ws_env.is_file():
            load_dotenv(ws_env, override=False)

    found = find_dotenv(usecwd=True)
    _consulted.append(
        (found or f"búsqueda hacia arriba desde {Path.cwd()}", bool(found))
    )
    if found:
        load_dotenv(found, override=False)

    _consulted.append((str(USER_ENV), USER_ENV.is_file()))
    if USER_ENV.is_file():
        load_dotenv(USER_ENV, override=False)


def describe_sources() -> str:
    """De dónde se intentó leer, para pegar en un error de credencial faltante.

    El `.env` de la raíz del repo se encuentra buscando hacia arriba desde el
    directorio actual, así que con el comando instalado global y ejecutado
    desde el workspace NO se lee. Callarlo convierte un fix de treinta segundos
    en una cacería.
    """
    if not _consulted:
        return "No se consultó ningún .env (falta el extra 'llm': python-dotenv)."
    lines = [
        f"  {'✓' if ok else '·'} {path}" + ("" if ok else "  (no existe)")
        for path, ok in _consulted
    ]
    return ".env consultados:\n" + "\n".join(lines)


def user_env_hint() -> str:
    """Cómo dejar la credencial una sola vez para todas las migraciones."""
    return (
        f"Para una key por máquina (sirve desde cualquier carpeta): {USER_ENV}"
    )
