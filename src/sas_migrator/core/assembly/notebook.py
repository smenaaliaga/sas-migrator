"""Ensamblador determinista de notebooks con nbformat.

El contrato clave de v2: el traductor (LLM o stub) produce ``NodeTranslation``
—código sin headers ni anclas— y ESTE módulo construye el notebook, calcula
``cell_index`` desde los índices reales al ensamblar (nunca a mano) y escribe
``sas_python_mapping.json`` correcto por construcción.

Chequeos estáticos por nodo ANTES de escribir (un fallo omite el nodo del
notebook y del mapping — nunca se escribe un notebook roto; el caller registra
el fallo como needs_human):

- ``ast.parse`` de cada celda (sintaxis);
- imports resolubles vía ``importlib.util.find_spec`` (sin importar);
- patrones prohibidos: ``to_parquet``, ``duckdb``, y SQL dinámico por f-string
  (JoinedStr cuyo texto constante contiene SELECT/INSERT/UPDATE/DELETE);
- strategy del NodeTranslation debe coincidir con la del target del plan.

Convención de rutas (única): ``notebook_path`` es SIEMPRE relativo a la raíz
del workspace con prefijo ``output/`` — tanto en el plan como en el mapping.
"""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import nbformat

from sas_migrator.core.models.translation import (
    MappingEntry,
    NodeTranslation,
    SasPythonMapping,
)

BASELINE_IMPORTS = ("import pandas as pd", "import numpy as np")
FORBIDDEN_SUBSTRINGS = ("to_parquet", "duckdb")
_SQL_WORDS = ("select ", "insert ", "update ", "delete ")


@dataclass
class NodeAssemblyFailure:
    node_id: str
    reason: str  # syntax_error | unresolvable_import | forbidden_pattern | strategy_mismatch | empty_translation
    detail: str


# ── Chequeos estáticos ──────────────────────────────────────────────────────

def _import_root_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # import relativo: irresoluble en un notebook
                names.add(".")
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def _fstring_sql(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ).lower()
            if any(word in text for word in _SQL_WORDS):
                return text[:120]
    return None


def check_node_translation(nt: NodeTranslation) -> NodeAssemblyFailure | None:
    """Primer fallo estático del nodo, o None si es ensamblable."""
    if not any(cell.strip() for cell in nt.cells):
        return NodeAssemblyFailure(nt.node_id, "empty_translation", "sin celdas de código")

    sources = list(nt.imports) + list(nt.cells)
    for src in sources:
        lowered = src.lower()
        for token in FORBIDDEN_SUBSTRINGS:
            if token in lowered:
                return NodeAssemblyFailure(
                    nt.node_id, "forbidden_pattern", f"patrón prohibido '{token}'"
                )

    trees: list[ast.AST] = []
    for i, cell in enumerate(nt.cells):
        try:
            trees.append(ast.parse(cell))
        except SyntaxError as exc:
            return NodeAssemblyFailure(
                nt.node_id, "syntax_error", f"celda {i}: {exc.msg} (línea {exc.lineno})"
            )
    for line in nt.imports:
        try:
            trees.append(ast.parse(line))
        except SyntaxError as exc:
            return NodeAssemblyFailure(
                nt.node_id, "syntax_error", f"import inválido '{line}': {exc.msg}"
            )

    roots: set[str] = set()
    for tree in trees:
        sql = _fstring_sql(tree)
        if sql is not None:
            return NodeAssemblyFailure(
                nt.node_id, "forbidden_pattern", f"SQL dinámico por f-string: {sql}"
            )
        roots |= _import_root_names(tree)
    for name in sorted(roots):
        try:
            resolvable = name != "." and importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            resolvable = False
        if not resolvable:
            return NodeAssemblyFailure(
                nt.node_id, "unresolvable_import", f"módulo no resoluble: '{name}'"
            )
    return None


# ── Ensamblado ──────────────────────────────────────────────────────────────

def _canonical_nb_rel(notebook_path: str) -> str:
    name = Path(notebook_path).name
    return notebook_path if notebook_path.replace("\\", "/").startswith("output/") \
        else f"output/{name}"


def assemble_notebooks(
    plan: dict,
    translations: dict[str, NodeTranslation],
    output_dir: Path,
    *,
    db_bootstrap: bool = False,
) -> tuple[SasPythonMapping, list[NodeAssemblyFailure]]:
    """Construye los notebooks del plan y el mapping SAS→Python.

    ``translations``: NodeTranslation por node_id. Un target sin traducción se
    omite en silencio aquí (el caller ya lo registró como needs_human); un
    target cuya traducción falla los chequeos estáticos se omite y se devuelve
    como ``NodeAssemblyFailure``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    by_notebook: dict[str, list[dict]] = {}
    for target in plan.get("targets", []):
        nb_rel = _canonical_nb_rel(str(target.get("notebook_path") or "flow.ipynb"))
        by_notebook.setdefault(nb_rel, []).append(target)

    entries: list[MappingEntry] = []
    failures: list[NodeAssemblyFailure] = []

    for nb_rel, nb_targets in by_notebook.items():
        name = Path(nb_rel).name
        title = Path(name).stem

        valid: list[NodeTranslation] = []
        for target in nb_targets:
            nid = str(target.get("node_id"))
            nt = translations.get(nid)
            if nt is None:
                continue  # sin traducción: needs_human ya registrado aguas arriba
            if nt.strategy and target.get("strategy") and nt.strategy != target["strategy"]:
                failures.append(
                    NodeAssemblyFailure(
                        nid, "strategy_mismatch",
                        f"traducción '{nt.strategy}' vs plan '{target['strategy']}'",
                    )
                )
                continue
            failure = check_node_translation(nt)
            if failure is not None:
                failures.append(failure)
                continue
            valid.append(nt)

        # Celda de configuración: imports agregados (dedupe, primera aparición).
        imports: list[str] = list(BASELINE_IMPORTS)
        if db_bootstrap:
            for line in ("import os", "import sqlalchemy"):
                if line not in imports:
                    imports.append(line)
        for nt in valid:
            for line in nt.imports:
                line = line.strip()
                if line and line not in imports:
                    imports.append(line)

        config_source = (
            "# ========= Celda 1: Configuración =========\n" + "\n".join(imports) + "\n"
        )
        if db_bootstrap:
            # La URL la fija el orquestador (ejecución autorizada) vía env var;
            # el notebook queda standalone y sin secretos.
            config_source += (
                "\n# Conexión a BD — la define el orquestador al ejecutar\n"
                'engine = sqlalchemy.create_engine(os.environ["SASMIG_DB_URL"])\n'
            )

        nb = nbformat.v4.new_notebook()
        cells = [
            nbformat.v4.new_markdown_cell(f"# {title}"),
            nbformat.v4.new_code_cell(config_source),
        ]

        for nt in valid:
            label = nt.node_label or nt.node_id
            cells.append(nbformat.v4.new_markdown_cell(f"## {label}"))
            code_index = len(cells)  # índice REAL de la primera celda code
            first, *rest = [c for c in nt.cells if c.strip()]
            cells.append(
                nbformat.v4.new_code_cell(f"# ========= {label} =========\n{first}")
            )
            for extra in rest:
                cells.append(nbformat.v4.new_code_cell(extra))
            entries.append(
                MappingEntry(
                    node_id=nt.node_id,
                    node_label=label,
                    sas_construct=nt.traceability.sas_construct,
                    python_artifact=nb_rel,
                    notebook_path=nb_rel,
                    cell_index=code_index,
                    cell_count=1 + len(rest),
                    business_rule=nt.traceability.business_rule,
                    confidence=nt.confidence,
                )
            )

        # nbformat asigna ids aleatorios; ids fijos por posición = determinismo.
        for i, cell in enumerate(cells):
            cell["id"] = f"cell-{i:03d}"
        nb["cells"] = cells
        nbformat.write(nb, str(output_dir / name))

    return SasPythonMapping(mappings=entries), failures
