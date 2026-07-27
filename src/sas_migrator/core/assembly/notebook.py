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
import re
from dataclasses import dataclass
from pathlib import Path

import nbformat

from sas_migrator.core.models.translation import (
    MappingEntry,
    NodeTranslation,
    SasPythonMapping,
)

BASELINE_IMPORTS = ("import pandas as pd", "import numpy as np")
# drop table / if_exists=replace: destruyen DDL (permisos, índices) — el
# reemplazo estilo SAS se replica con DELETE FROM sin WHERE + INSERT.
FORBIDDEN_SUBSTRINGS = ("to_parquet", "duckdb", "drop table")
_REPLACE_WRITE = re.compile(r"if_exists\s*=\s*['\"]replace['\"]")
_SQL_WORDS = ("select ", "insert ", "update ", "delete ")

# Scanner de secretos (hardening Etapa 6): antes era una regla de prompt —
# ahora es código. Un secreto literal en una celda es fallo de ensamblado.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(password|passwd|pwd)\s*=\s*['\"][^'\"]{3,}['\"]"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
)


@dataclass
class NodeAssemblyFailure:
    node_id: str
    reason: str  # syntax_error | unresolvable_import | forbidden_pattern | strategy_mismatch | empty_translation | secret_detected | absolute_path
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


# Rutas absolutas literales (hardening): el SAS original vive lleno de rutas
# de otro mundo (servidores SAS, unidades de red); el estándar de traducción
# es ruta RELATIVA al workspace + warning. Se detecta sobre los strings del
# AST — no sobre el fuente crudo — para no confundir comentarios ni regex.
_ABS_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]"),            # C:\... o C:/...
    re.compile(r"^\\{1,2}[\w.$-]+\\"),         # \\servidor\share\... (o \raíz\...)
    re.compile(r"^/(?:[\w.-]+/)+[\w.-]+"),     # /ruta/unix/con/segmentos
)


def _absolute_path_literal(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "://" in text:  # URLs y connection strings no son rutas de disco
                continue
            for pattern in _ABS_PATH_PATTERNS:
                if pattern.match(text):
                    return text
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
        if _REPLACE_WRITE.search(lowered):
            return NodeAssemblyFailure(
                nt.node_id, "forbidden_pattern",
                "to_sql(if_exists='replace') destruye DDL — el reemplazo estilo "
                "SAS es DELETE FROM sin WHERE + append",
            )
        for pattern in _SECRET_PATTERNS:
            m = pattern.search(src)
            if m:
                return NodeAssemblyFailure(
                    nt.node_id, "secret_detected",
                    f"posible credencial literal en el código: '{m.group(0)[:30]}…' — "
                    "usar variables de entorno",
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
        path = _absolute_path_literal(tree)
        if path is not None:
            return NodeAssemblyFailure(
                nt.node_id, "absolute_path",
                f"ruta absoluta literal '{path[:60]}' — el estándar es ruta "
                "relativa al workspace (declarar el cambio en warnings)",
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


def _parameters_cell(nb_targets: list[dict], values: dict | None = None):
    """Celda de parámetros del notebook, o None si el flujo no usa macro vars.

    Las macro vars de SAS (``&ANIO``) se resuelven fuera del .egp —autoexec del
    servidor, prompts de EG, la sesión del analista—, así que su valor no está
    en ningún artefacto. Fijarlas como literal en medio del código convertiría
    un proceso parametrizado por período en uno que solo sirve para un período.

    Van arriba, en una celda con la tag ``parameters``: es la convención de
    papermill, así que además quedan inyectables desde afuera sin editar el
    notebook.
    """
    nombres = sorted({
        str(v) for t in nb_targets for v in (t.get("macro_params") or []) if str(v).strip()
    })
    if not nombres:
        return None

    values = values or {}
    lines = [
        "# ========= Parámetros =========",
        "# Variables macro del SAS original. El .egp NO las define (venían del",
        "# entorno SAS): su valor sale de project_config.yaml → run.macro_params,",
        "# o se inyecta acá (celda 'parameters' de papermill).",
        "",
    ]
    lines.extend(
        f"{name} = {values[name]!r}  # &{name}" if name in values
        else f"{name} = None  # &{name} — sin declarar en run.macro_params"
        for name in nombres
    )
    if any(name not in values for name in nombres):
        # Fallar acá y con nombre propio: sin esto el None viaja hasta el filtro
        # y revienta con un TypeError de pandas que no dice qué parámetro falta.
        lines.append("")
        lines.append(
            "faltantes = [n for n, v in "
            + "{" + ", ".join(f'"{n}": {n}' for n in nombres) + "}.items() if v is None]"
        )
        lines.append(
            'if faltantes:\n'
            '    raise ValueError(f"Parámetros sin valor: {faltantes}")'
        )
    cell = nbformat.v4.new_code_cell("\n".join(lines))
    cell.metadata["tags"] = ["parameters"]
    return cell


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
        cells = [nbformat.v4.new_markdown_cell(f"# {title}")]
        params_cell = _parameters_cell(nb_targets, plan.get("macro_param_values"))
        if params_cell is not None:
            cells.append(params_cell)
        cells.append(nbformat.v4.new_code_cell(config_source))

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
