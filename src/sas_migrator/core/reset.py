"""Limpia artefactos de state/ y output/ para reiniciar la migración desde cero.

Preserva .gitkeep y recrea state/nodes/. El workspace se recibe explícito:
nunca se deriva de __file__ (el bug del reset original: 4 dirname anidados
resolvían a .github/ y el script reportaba éxito sin borrar nada).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def clean_dir(path: Path) -> int:
    """Elimina archivos (salvo .gitkeep) y subdirectorios vacíos. Devuelve el conteo."""
    count = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            if f == ".gitkeep":
                continue
            os.remove(os.path.join(root, f))
            count += 1
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass
    return count


def reset_workspace(workspace: Path) -> dict[str, int]:
    """Limpia state/, output/ y .sas-migrator-tmp/ bajo el workspace dado."""
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(f"Workspace no existe: {workspace}")
    counts = {
        "state": clean_dir(workspace / "state"),
        "output": clean_dir(workspace / "output"),
        "tmp": clean_dir(workspace / ".sas-migrator-tmp"),
    }
    (workspace / "state" / "nodes").mkdir(parents=True, exist_ok=True)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path,
                        help="Raíz del workspace de migración (contiene state/ y output/)")
    args = parser.parse_args()
    counts = reset_workspace(args.workspace)
    print(
        f"Reset completado: {counts['state']} archivos en state/, "
        f"{counts['output']} en output/, {counts['tmp']} temporales eliminados."
    )
    print("state/nodes/ recreado.")


if __name__ == "__main__":
    main()
