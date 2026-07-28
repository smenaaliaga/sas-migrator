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
- strategy del NodeTranslation debe coincidir con la del target del plan;
- relleno que aparenta funcionar: placeholders, ``except:`` desnudo,
  autoasignación, DataFrame vacío bajo guardia, ``WHERE 1=1`` sin predicados;
- escritura fila a fila (``iterrows()`` + ``execute``/``to_sql``).

``check_node_translation`` es local al nodo, así que lo corre TAMBIÉN la fase
6 apenas traduce, con reintento: un nodo que no pasa nunca llega al disco.
El chequeo de nombres sin definir (``core.validation.symbols``) necesita el
notebook entero y solo puede correr acá, al ensamblar.

Convención de rutas (única): ``notebook_path`` es SIEMPRE relativo a la raíz
del workspace con prefijo ``output/`` — tanto en el plan como en el mapping.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import nbformat

from sas_migrator.core.models.translation import (
    MappingEntry,
    NodeTranslation,
    SasPythonMapping,
)
from sas_migrator.core.validation.symbols import undefined_in_cells

BASELINE_IMPORTS = ("import pandas as pd", "import numpy as np")

# Default de librerías de terceros permitidas en el código DESTINO. La fuente
# de verdad configurable es `translation.allowed_imports` (project_config.yaml);
# este frozenset la espeja para los callers sin config (stubs, tests). Validar
# contra el entorno del MIGRADOR era el bug: los notebooks corren en OTRO
# entorno, documentado por output/requirements.txt.
DEFAULT_ALLOWED_IMPORTS = frozenset({
    "matplotlib", "numpy", "openpyxl", "pandas", "pyreadstat", "requests",
    "scipy", "sqlalchemy",
})
# drop table / if_exists=replace: destruyen DDL (permisos, índices) — el
# reemplazo estilo SAS se replica con DELETE FROM sin WHERE + INSERT.
FORBIDDEN_SUBSTRINGS = ("to_parquet", "duckdb", "drop table")
_REPLACE_WRITE = re.compile(r"if_exists\s*=\s*['\"]replace['\"]")
# Forma de sentencia, no palabra suelta: un print(f"insert completado: {n}")
# contiene "insert " y no es SQL. Se exige el par verbo+cláusula.
_SQL_STATEMENT = re.compile(
    r"(?is)\b(select\b.*?\bfrom\b|insert\s+into\b|update\b.*?\bset\b|delete\s+from\b)"
)

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
    reason: str  # syntax_error | unresolvable_import | forbidden_pattern | strategy_mismatch | empty_translation | secret_detected | absolute_path | placeholder_stub | bare_except | self_assignment | empty_frame_guard | sql_no_op | row_by_row_write | undefined_name
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
            )
            if _SQL_STATEMENT.search(text):
                return " ".join(text.split())[:120]
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


# ── Relleno que aparenta funcionar ──────────────────────────────────────────
#
# Esta familia de chequeos no busca código roto: busca código que corre entero
# y entrega números equivocados sin lanzar nada. Es lo que produce un traductor
# cuando no supo resolver algo y no quiso decirlo. El estándar del proyecto es
# que un hueco sea `raise NotImplementedError(...)` — ruidoso y localizable —,
# nunca un DataFrame vacío ni un `except:` que se traga el fallo.

_PLACEHOLDER_WORDS = re.compile(
    r"(?i)\b(placeholder|marcador de posici[oó]n|por ahora se deja|se completar[ií]a)\b"
)


def _placeholder_comment(src: str) -> str | None:
    """Comentario que anuncia un relleno.

    Se tokeniza en vez de buscar '#' en el texto: así un '#' dentro de un
    string (un literal de SQL, un nombre de columna) no cuenta como comentario.
    """
    import io
    import tokenize

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    for tok in tokens:
        if tok.type == tokenize.COMMENT and _PLACEHOLDER_WORDS.search(tok.string):
            return tok.string.strip()[:100]
    return None


def _is_empty_frame_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "DataFrame"
    return isinstance(func, ast.Name) and func.id == "DataFrame"


def _empty_frame_guard(trees: list[ast.AST]) -> str | None:
    """``x = pd.DataFrame()`` y después ``if len(x) > 0:`` — la rama muere.

    Es el patrón exacto que produce cifras faltantes sin una sola excepción: el
    traductor deja el DataFrame vacío "para que lo cargue el revisor" y el
    resto del nodo se saltea en silencio.

    Solo se marca si el nombre se asigna UNA vez en todo el nodo. Un acumulador
    (``out = pd.DataFrame()`` … ``out = pd.concat(...)``) se reasigna, y ahí el
    guard sí puede entrar: no es este bug.
    """
    assign_count: dict[str, int] = {}
    empty: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets = [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        assign_count[sub.id] = assign_count.get(sub.id, 0) + 1
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and _is_empty_frame_call(node.value)
            ):
                empty.add(node.targets[0].id)

    suspect = {name for name in empty if assign_count.get(name, 0) == 1}
    if not suspect:
        return None
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp)):
                continue
            for sub in ast.walk(node.test):
                if isinstance(sub, ast.Name) and sub.id in suspect:
                    return sub.id
    return None


def _bare_except(tree: ast.AST) -> int | None:
    """``except:`` sin tipo — convierte un fallo real en un dato faltante."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            return node.lineno
    return None


def _self_assignment(tree: ast.AST) -> str | None:
    """``t_sectorizacion = t_sectorizacion``.

    El traductor lo escribe para "declarar" que el nombre viene de otro nodo.
    No declara nada: o el nombre ya existe y la línea sobra, o no existe y esto
    es un NameError disfrazado de contrato.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
            and node.targets[0].id == node.value.id
        ):
            return node.targets[0].id
    return None


_WHERE_NOOP = re.compile(r"(?is)\bwhere\s+1\s*=\s*1\b(?!.*?\b(and|or)\b)")


def _sql_no_op(tree: ast.AST) -> str | None:
    """``WHERE 1=1`` sin ningún predicado detrás: el filtro del SAS se perdió."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _WHERE_NOOP.search(node.value)
        ):
            return " ".join(node.value.split())[:100]
    return None


def _calls_iterrows(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr in ("iterrows", "itertuples"):
            return True
    return False


def _row_by_row_write(tree: ast.AST) -> str | None:
    """``for _, row in df.iterrows(): conn.execute(INSERT ...)``.

    Un round-trip por fila contra la base. La traducción correcta de un APPEND
    de SAS es una escritura masiva (``to_sql`` fuera del loop, o executemany).
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)) or not _calls_iterrows(node.iter):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                    and inner.func.attr in ("execute", "executemany", "to_sql"):
                return inner.func.attr
    return None


def _import_resolvable(name: str, allowed: frozenset[str] | set[str]) -> bool:
    """¿El import es legítimo en el código destino?

    stdlib ∪ allowlist deciden sin mirar el entorno local; ``find_spec`` queda
    como fallback para lo instalado acá que no está declarado (pasa, pero el
    requirements.txt no lo documenta — la allowlist es el contrato).
    """
    if name == ".":
        return False
    if name in sys.stdlib_module_names or name in allowed:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_node_translation(
    nt: NodeTranslation,
    allowed_imports: Iterable[str] | None = None,
) -> NodeAssemblyFailure | None:
    """Primer fallo estático del nodo, o None si es ensamblable.

    Local al nodo a propósito: así lo puede correr la fase 6 apenas traduce,
    antes de persistir, y usar el ``detail`` como feedback del reintento.
    """
    allowed = frozenset(allowed_imports) if allowed_imports is not None \
        else DEFAULT_ALLOWED_IMPORTS
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

    for i, cell in enumerate(nt.cells):
        comment = _placeholder_comment(cell)
        if comment is not None:
            return NodeAssemblyFailure(
                nt.node_id, "placeholder_stub",
                f"celda {i}: relleno anunciado en un comentario ({comment}). Un hueco "
                "va como raise NotImplementedError(...) — falla fuerte y se ve — "
                "nunca como valor vacío que el resto del código consume",
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
        lineno = _bare_except(tree)
        if lineno is not None:
            return NodeAssemblyFailure(
                nt.node_id, "bare_except",
                f"'except:' desnudo (línea {lineno}) — convierte un fallo real en "
                "un dato faltante silencioso; capturar la excepción concreta o "
                "dejar que propague",
            )
        name = _self_assignment(tree)
        if name is not None:
            return NodeAssemblyFailure(
                nt.node_id, "self_assignment",
                f"'{name} = {name}' no declara nada: si el nombre viene de un nodo "
                "anterior, la línea sobra; si no viene, es un NameError disfrazado",
            )
        sql = _sql_no_op(tree)
        if sql is not None:
            return NodeAssemblyFailure(
                nt.node_id, "sql_no_op",
                f"'WHERE 1=1' sin predicados: {sql} — el filtro del SAS original "
                "se perdió y la consulta trae la tabla entera",
            )
        call = _row_by_row_write(tree)
        if call is not None:
            return NodeAssemblyFailure(
                nt.node_id, "row_by_row_write",
                f"'{call}' dentro de un loop de iterrows(): un round-trip por fila. "
                "El APPEND de SAS se traduce con una escritura masiva (to_sql fuera "
                "del loop)",
            )
        roots |= _import_root_names(tree)

    frame = _empty_frame_guard(trees)
    if frame is not None:
        return NodeAssemblyFailure(
            nt.node_id, "empty_frame_guard",
            f"'{frame}' se asigna una sola vez, a un DataFrame vacío, y después se "
            "usa como condición: esa rama nunca corre y el nodo entrega números "
            "faltantes sin error. Cargar el dato de verdad o raise NotImplementedError",
        )

    for name in sorted(roots):
        if not _import_resolvable(name, allowed):
            return NodeAssemblyFailure(
                nt.node_id, "unresolvable_import",
                f"import no permitido: '{name}' — no es stdlib ni está en "
                "translation.allowed_imports (project_config.yaml)",
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


def _macro_param_names(nb_targets: list[dict]) -> set[str]:
    return {
        str(v) for t in nb_targets for v in (t.get("macro_params") or []) if str(v).strip()
    }


def _declared_input_names(nb_targets: list[dict]) -> set[str]:
    """Nombres que el plan autoriza a usar sin haberlos definido en el notebook.

    ``input_datasets`` dice qué consume el nodo. Un dataset SAS ``WORK.VENTAS``
    llega al Python como ``ventas`` (y a veces como ``work_ventas``): ambas
    formas cuentan. Usar un nombre DECLARADO es legítimo aunque el notebook no
    lo defina — lo trae un paso previo o el flujo anterior. Usar uno que nadie
    declaró ni definió es el bug que buscamos.
    """
    names: set[str] = set()
    for target in nb_targets:
        for dataset in (target.get("input_datasets") or []):
            raw = str(dataset).strip()
            if not raw:
                continue
            names.add(raw.split(".")[-1].lower())
            names.add(raw.replace(".", "_").lower())
    return names


def _names_bound_by_imports(import_lines: list[str]) -> set[str]:
    names: set[str] = set()
    for line in import_lines:
        try:
            tree = ast.parse(line)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names |= {
                    (alias.asname or alias.name.split(".")[0]) for alias in node.names
                }
    return names


def _reject_undefined_names(
    valid: list[NodeTranslation], known: set[str]
) -> tuple[list[NodeTranslation], list[NodeAssemblyFailure]]:
    """Filtra los nodos que usan nombres que el notebook nunca define.

    Recorre en orden de ejecución acumulando lo que cada nodo deja definido. Un
    nodo rechazado igual aporta sus definiciones al set: así el nodo siguiente
    se juzga por lo suyo y no arrastra la culpa del anterior — se reporta la
    causa raíz una vez, no una cascada.
    """
    kept: list[NodeTranslation] = []
    failures: list[NodeAssemblyFailure] = []
    visible = set(known)
    for nt in valid:
        missing, defined = undefined_in_cells(list(nt.cells), visible)
        visible |= defined | _names_bound_by_imports(list(nt.imports))
        if missing:
            failures.append(NodeAssemblyFailure(
                nt.node_id, "undefined_name",
                f"usa {len(missing)} nombre(s) que ninguna celda anterior define: "
                f"{', '.join(missing[:8])}"
                + (" …" if len(missing) > 8 else "")
                + ". Si vienen de otro nodo, ese nodo los dejó en la base (hay que "
                "leerlos con pd.read_sql) o no existen",
            ))
            continue
        kept.append(nt)
    return kept, failures


def assemble_notebooks(
    plan: dict,
    translations: dict[str, NodeTranslation],
    output_dir: Path,
    *,
    db_bootstrap: bool = False,
    allowed_imports: Iterable[str] | None = None,
) -> tuple[SasPythonMapping, list[NodeAssemblyFailure]]:
    """Construye los notebooks del plan y el mapping SAS→Python.

    ``translations``: NodeTranslation por node_id. Un target sin traducción se
    omite en silencio aquí (el caller ya lo registró como needs_human); un
    target cuya traducción falla los chequeos estáticos se omite y se devuelve
    como ``NodeAssemblyFailure``. Además emite ``output/requirements.txt`` con
    las librerías de terceros que los notebooks realmente importan — el
    contrato del entorno DESTINO.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    all_import_lines: list[str] = []

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
            failure = check_node_translation(nt, allowed_imports)
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

        # Nombres sin definir: necesita el notebook entero (imports agregados,
        # parámetros, y los nodos anteriores en orden), así que va acá y no en
        # check_node_translation, que es local al nodo.
        known = (
            _names_bound_by_imports(imports)
            | _macro_param_names(nb_targets)
            | _declared_input_names(nb_targets)
        )
        known.add("faltantes")  # lo define la celda de parámetros
        if db_bootstrap:
            known.add("engine")
        valid, undefined_failures = _reject_undefined_names(valid, known)
        failures.extend(undefined_failures)

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
        all_import_lines.extend(imports)

    _write_requirements(output_dir, all_import_lines)
    return SasPythonMapping(mappings=entries), failures


def _write_requirements(output_dir: Path, import_lines: list[str]) -> None:
    """``output/requirements.txt``: las terceras que los notebooks importan.

    Sin esto, "en qué entorno corren los notebooks" era conocimiento oral. El
    contenido sale de los imports REALES ensamblados (no de la allowlist
    entera: permitir scipy no obliga a instalarlo).
    """
    from sas_migrator.core.utils.fsio import atomic_write_text

    roots: set[str] = set()
    for line in import_lines:
        try:
            roots |= _import_root_names(ast.parse(line))
        except SyntaxError:
            continue
    third_party = sorted(
        r for r in roots if r != "." and r not in sys.stdlib_module_names
    )
    text = (
        "# Entorno destino de los notebooks generados — emitido por el ensamblador\n"
        + "".join(f"{name}\n" for name in third_party)
    )
    atomic_write_text(output_dir / "requirements.txt", text)
