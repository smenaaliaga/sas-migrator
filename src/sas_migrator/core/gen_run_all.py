#!/usr/bin/env python3
"""Generate output/run_all.py deterministically from the notebooks that exist.

`run_all.py` is pure boilerplate except for the ordered NOTEBOOKS list. That
list is fully determined by the notebooks the translator already wrote, so it
must never be hand-authored (the list and the real files can drift). This script
globs the generated notebooks in filename order (the zero-padded NB-NN prefix
encodes topological order) and emits run_all.py from the canonical template.

Run this in Phase 6 AFTER the translator produced the notebooks. The translator
has no execute tool, so the orchestrator runs it.

Usage:
    python .github/skills/sas-to-python/scripts/gen_run_all.py --output-dir output

Exit codes:
    0 = run_all.py written
    1 = no notebooks found in output/ (nothing to orchestrate)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sas_migrator.core.utils.fsio import atomic_write_text

TEMPLATE = '''#!/usr/bin/env python3
"""Ejecuta todos los notebooks en orden topológico.

ADVERTENCIA: los notebooks escriben en SQL Server (ver state/db_connections.yaml).
Ejecutar este script carga datos reales en el servidor configurado. La escritura
es idempotente por periodo (DELETE+INSERT), pero corre con conocimiento del usuario.

Generado por gen_run_all.py — no editar a mano; volver a correr el generador.
"""
import argparse, os, subprocess, sys

NOTEBOOKS = [
{notebook_lines}
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notebooks', nargs='*',
                        help='Ejecutar solo estos notebooks (para re-validación parcial)')
    args = parser.parse_args()

    targets = args.notebooks or NOTEBOOKS
    results = {{}}
    for nb in targets:
        if not os.path.exists(nb):
            print(f'⚠ {{nb}} no encontrado, saltando')
            results[nb] = 'SKIPPED'
            continue
        print(f'▶ Ejecutando {{nb}}...')
        rc = subprocess.run(
            ['jupyter', 'nbconvert', '--to', 'notebook', '--execute', nb,
             '--ExecutePreprocessor.timeout=600', '--output', nb],
            capture_output=True, text=True
        )
        if rc.returncode == 0:
            print(f'  ✓ {{nb}} OK')
            results[nb] = 'PASS'
        else:
            print(f'  ✗ {{nb}} FALLÓ')
            print(rc.stderr[-500:] if rc.stderr else 'Sin detalle')
            results[nb] = 'FAIL'

    passed = sum(1 for v in results.values() if v == 'PASS')
    failed = sum(1 for v in results.values() if v == 'FAIL')
    print(f'\\nResultado: {{passed}} OK, {{failed}} fallidos')
    sys.exit(1 if failed > 0 else 0)

if __name__ == '__main__':
    main()
'''


def discover_notebooks(output_dir: Path) -> list[str]:
    """Return notebook filenames (relative to output_dir) in execution order."""
    nbs = sorted(p.name for p in output_dir.glob("NB-*.ipynb"))
    if nbs:
        return nbs
    # single strategy: one self-contained notebook
    flow = output_dir / "flow.ipynb"
    if flow.exists():
        return ["flow.ipynb"]
    return []


def write_run_all(output_dir: Path) -> list[str]:
    """Escribe output/run_all.py desde los notebooks existentes.

    Devuelve la lista ordenada de notebooks incluidos ([] si no hay ninguno —
    en ese caso no escribe nada). Es la vía in-process para el nodo de Fase 6.
    """
    output_dir = Path(output_dir)
    notebooks = discover_notebooks(output_dir)
    if not notebooks:
        return []
    notebook_lines = "\n".join(f"    {nb!r}," for nb in notebooks)
    atomic_write_text(
        output_dir / "run_all.py", TEMPLATE.format(notebook_lines=notebook_lines)
    )
    return notebooks


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate output/run_all.py from existing notebooks")
    parser.add_argument("--output-dir", default="output", help="Directory with generated notebooks (default: output)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    notebooks = write_run_all(output_dir)

    if not notebooks:
        print(f"ERROR: no se encontraron notebooks (NB-*.ipynb ni flow.ipynb) en {output_dir}/", file=__import__("sys").stderr)
        return 1

    print(f"OK run_all.py: {len(notebooks)} notebooks -> {output_dir / 'run_all.py'}")
    for nb in notebooks:
        print(f"  - {nb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
