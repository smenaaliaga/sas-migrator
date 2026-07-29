"""Scaffold del workspace — crear la estructura que una migración necesita.

El espejo de ``workspace_reset``: uno borra lo derivado, este escribe lo que un
humano tendría que escribir a mano. Está fuera de la CLI por la misma razón que
aquel: qué archivos forman un workspace es una decisión del dominio, no de la
interfaz.

Dos garantías que hacen que ``init`` se pueda correr sin mirar:

* **No pisa nada.** Un archivo que ya existe se reporta y se deja como está, así
  que correrlo de nuevo sobre una migración en curso no puede tocar un
  ``project_config.yaml`` editado ni el ``.egp`` ya copiado. Por eso no hay
  ``--force``: no hay nada que forzar.
* **No escribe secretos.** Se escribe ``.env.example``, nunca ``.env``: la key
  la pone el usuario, y el archivo que la lleva no lo crea una herramienta.

Los templates viven DENTRO del paquete (``sas_migrator/templates/``) y se leen
con ``importlib.resources``. Vivían en la raíz del repo, y como el wheel solo
empaqueta ``src/sas_migrator``, el hint "copiá project_config.example.yaml" era
imposible de seguir para quien instaló el CLI sin clonar el repo.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from sas_migrator.core.config import CONFIG_FILENAME

# Carpetas del workspace, en orden de lectura: primero lo que pone el usuario,
# después lo que escribe el runtime.
WS_DIRS = ("input/egp", "input/data", "input/docs", "state", "output")

# Las de entrada nacen vacías, y un directorio vacío no sobrevive un commit: sin
# `.gitkeep` el que clona el workspace no ve dónde va el .egp.
KEEP_DIRS = ("input/egp", "input/data", "input/docs")

# Template en el paquete → nombre en el workspace. El gitignore se guarda sin
# punto: un `.gitignore` dentro del paquete serían reglas reales para esa
# carpeta, no un template.
TEMPLATES = {
    "project_config.yaml": CONFIG_FILENAME,
    "env.example": ".env.example",
    "gitignore": ".gitignore",
}

CREATED = "creado"
EXISTED = "ya existía"


@dataclass(frozen=True)
class Item:
    """Algo que ``init`` escribió, o encontró ya escrito."""

    path: str  # relativo al workspace, con / (es lo que se le muestra al usuario)
    status: str  # CREATED | EXISTED


def template_text(name: str) -> str:
    """Contenido de un template del paquete.

    Por ``importlib.resources`` y no por ``__file__``: es lo mismo en instalación
    editable y en wheel/pipx, que es justamente el caso que este módulo vino a
    arreglar.
    """
    return (files("sas_migrator") / "templates" / name).read_text(encoding="utf-8")


def _write_if_absent(path: Path, text: str, rel: str) -> Item:
    if path.exists():
        return Item(rel, EXISTED)
    path.write_text(text, encoding="utf-8")
    return Item(rel, CREATED)


def _check_egp(egp: Path) -> Path:
    """Valida el .egp antes de crear nada: un typo en la ruta no deja a medias."""
    if not egp.exists():
        raise ValueError(f"No existe el archivo: {egp}")
    if not egp.is_file():
        raise ValueError(f"No es un archivo: {egp}")
    if egp.suffix.lower() != ".egp":
        raise ValueError(
            f"{egp.name} no es un proyecto de Enterprise Guide (se espera .egp)"
        )
    return egp


def init_workspace(workspace: Path | str, egp: Path | str | None = None) -> list[Item]:
    """Crea (o completa) el workspace y devuelve qué se escribió.

    ``egp`` es opcional: sin él, ``input/egp/`` queda vacío y ``doctor`` dice que
    falta. Con él, el archivo se COPIA — el original del usuario no se mueve de
    donde estaba.
    """
    ws = Path(workspace)
    source = _check_egp(Path(egp)) if egp is not None else None
    if ws.exists() and not ws.is_dir():
        raise ValueError(f"{ws} existe y no es un directorio")

    items: list[Item] = []
    if ws.is_dir():
        items.append(Item(".", EXISTED))
    else:
        ws.mkdir(parents=True)
        items.append(Item(".", CREATED))

    for rel in WS_DIRS:
        target = ws / rel
        existed = target.is_dir()
        target.mkdir(parents=True, exist_ok=True)
        items.append(Item(f"{rel}/", EXISTED if existed else CREATED))

    for rel in KEEP_DIRS:
        target = ws / rel
        # Solo mientras la carpeta esté vacía: con contenido real el .gitkeep es
        # ruido, y borrarlo después no es asunto de este comando.
        if not any(target.iterdir()):
            (target / ".gitkeep").write_text("", encoding="utf-8")

    for name, dest in TEMPLATES.items():
        items.append(_write_if_absent(ws / dest, template_text(name), dest))

    if source is not None:
        dest = ws / "input" / "egp" / source.name
        rel = f"input/egp/{source.name}"
        if dest.exists():
            items.append(Item(rel, EXISTED))
        else:
            shutil.copy2(source, dest)
            items.append(Item(rel, CREATED))

    return items
