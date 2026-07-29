"""Ensamblador determinista de notebooks con nbformat.

El contrato clave de v2: el traductor (LLM o stub) produce ``NodeTranslation``
—código sin headers ni anclas— y ESTE módulo construye el notebook, calcula
``cell_index`` desde los índices reales al ensamblar (nunca a mano) y escribe
``sas_python_mapping.json`` correcto por construcción.

Chequeos estáticos por nodo ANTES de escribir. Un fallo NO descarta el nodo:
lo emite marcado (celda markdown con el motivo + ``degraded`` en el mapping) y
lo devuelve como ``NodeAssemblyFailure`` para que el caller lo registre en
needs_human. Un notebook que puede romper al ejecutar es preferible a uno
silenciosamente incompleto: lo primero se ve, lo segundo no (ver ADR-0006 §3).
Los chequeos:

- ``ast.parse`` de cada celda (sintaxis);
- imports resolubles vía ``importlib.util.find_spec`` (sin importar);
- patrones prohibidos: ``to_parquet``, ``duckdb``, SQL con un VALOR interpolado
  por f-string (el NOMBRE de tabla sí se permite: el SAS lo arma con macro
  vars y no es parametrizable), y ``{var}`` en un string SQL sin la ``f``;
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
from sas_migrator.core.utils.pysource import parse_generated
from sas_migrator.core.validation.symbols import undefined_in_cells

BASELINE_IMPORTS = ("import pandas as pd", "import numpy as np")

# Válvula de escape AUDITABLE para el chequeo de escritura fila a fila: hay
# casos legítimos (SPs por fila, APIs sin batch). El comentario deja rastro
# greppeable y el motivo a la vista del revisor; sin motivo no vale.
_ESCAPE_ROW_LOOP = re.compile(r"#\s*sas-migrator:\s*permitir-loop-filas\s*[—-]\s*\S")

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
# reemplazo estilo SAS se replica con DELETE FROM sin WHERE + INSERT. Se
# buscan sobre el fuente SIN comentarios: tres nodos reales cayeron porque un
# comentario decía "no usar drop table". La forma exigida (\b y \s+) evita
# que "backdrop_tables" cuente.
FORBIDDEN_SUBSTRINGS = ("to_parquet", "duckdb")
_DROP_TABLE = re.compile(r"(?i)\bdrop\s+table\b")
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
    reason: str  # syntax_error | unresolvable_import | import_in_cell | forbidden_pattern | strategy_mismatch | empty_translation | secret_detected | absolute_path | placeholder_stub | bare_except | swallowed_exception | self_assignment | empty_frame_guard | sql_no_op | row_by_row_write | undefined_name
    detail: str
    emitted: bool = True  # el nodo igual fue al notebook, marcado como degradado


# Los dos únicos chequeos cuyo nodo NO se puede emitir tal cual: uno no parsea
# y el otro no tiene código que emitir. Los otros 14 producen Python válido —
# dudoso, pero válido— y el nodo va al notebook con su banner.
NON_EMITTABLE_KINDS = frozenset({"syntax_error", "empty_translation"})


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


# Una interpolación dentro de SQL puede estar en dos posiciones muy distintas, y
# solo una es inyección:
#
#   FROM TABLAS.BD_R{ANIO}{TRIM}   ← NOMBRE de objeto. Es lo que el SAS obliga
#                                     cuando la macro var arma el nombre de
#                                     tabla (`TABLAS.BD_R&ANIO&TRIM`), y ningún
#                                     driver parametriza identificadores.
#   WHERE C_SI_SCN = '{sector}'    ← VALOR. Esto es lo que el chequeo existe
#                                     para prohibir: va como bind param.
#
# Prohibir las dos volvía la regla insatisfacible para los nodos con macro var
# en el nombre de tabla — y el modelo, acorralado, terminó escribiendo un
# string SIN la `f` (ver `_sql_with_dead_placeholder`), que pasa el chequeo y
# manda las llaves literales a la base.
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_.]")
_PLACEHOLDER = "\x00"


def _interpolates_a_value(flat: str) -> bool:
    """¿Alguna interpolación de ``flat`` cae en posición de valor?

    Decide el carácter inmediatamente anterior, SIN saltar espacios: un
    identificador solo continúa a otro carácter de identificador, a un punto o
    a otra interpolación. Comilla, `=`, `(`, `,` o espacio son posición de
    valor. Sesga a rechazar — `FROM {schema}.{tabla}` queda prohibido, y está
    bien: el esquema se escribe literal.
    """
    for i, ch in enumerate(flat):
        if ch != _PLACEHOLDER:
            continue
        if i == 0:
            return True
        prev = flat[i - 1]
        if prev != _PLACEHOLDER and not _IDENT_CHAR.match(prev):
            return True
        # Dentro de un literal SQL ('...{x}...') es valor aunque el carácter
        # previo sea alfanumérico.
        if flat.count("'", 0, i) % 2 == 1:
            return True
    return False


def _fstring_sql(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        # Aplanado con centinela: conserva la POSICIÓN de cada interpolación,
        # que es justo lo que la versión anterior tiraba al concatenar solo las
        # partes constantes.
        flat = "".join(
            v.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
            else _PLACEHOLDER
            for v in node.values
        )
        if not _SQL_STATEMENT.search(flat):
            continue
        if _interpolates_a_value(flat):
            return " ".join(flat.replace(_PLACEHOLDER, "{…}").split())[:120]
    return None


# `{var}` en un string SQL que NO es f-string: nadie lo sustituye y las llaves
# viajan literales a la base. Es la forma que toma el chequeo anterior cuando el
# modelo lo esquiva en vez de satisfacerlo — pasó en producción con
# S8_wtw - Copy, y el SQL roto llegó al notebook entregado.
_DEAD_PLACEHOLDER = re.compile(r"\{[A-Za-z_]\w*\}")


def _sql_with_dead_placeholder(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        # Las partes constantes de un f-string llegan acá sin sus llaves (esas
        # son FormattedValue), así que un f-string legítimo nunca cae en esto.
        if not _SQL_STATEMENT.search(node.value):
            continue
        match = _DEAD_PLACEHOLDER.search(node.value)
        if match:
            return match.group(0)
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


def _strip_comments(src: str) -> str:
    """El fuente sin comentarios, para los chequeos de substring.

    Un comentario que dice "no usar drop table" no es un DROP TABLE. Si el
    fuente no tokeniza (celda con syntax error — ya reportada aparte), se
    devuelve tal cual: mejor un falso positivo que un falso negativo.
    """
    import io
    import tokenize

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    lines = src.splitlines(keepends=True)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:  # un comentario nunca abarca 2 líneas
            row, scol = tok.start[0] - 1, tok.start[1]
            line = lines[row]
            lines[row] = line[:scol] + line[tok.end[1]:]
    return "".join(lines)


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


def _is_noise_stmt(stmt: ast.stmt) -> bool:
    """¿La sentencia solo hace ruido (print/log/pass) sin re-lanzar ni marcar?"""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr):
        if isinstance(stmt.value, ast.Constant):
            return True  # docstring/comentario disfrazado
        call = stmt.value
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Name) and func.id in ("print", "_log"):
                return True
            if isinstance(func, ast.Attribute):
                root = func.value
                if isinstance(root, ast.Name) and root.id in ("logging", "logger", "log"):
                    return True
    return False


def _swallowed_exception(tree: ast.AST) -> int | None:
    """``except Exception as e: print(e)`` — el hueco silencioso REAL de
    producción (CodeTask-jo2ncH7bTsseLYP8): el handler tiene tipo, así que
    ``bare_except`` no lo ve, pero su cuerpo solo imprime y sigue — todos los
    chequeos pasan y el nodo entrega datos incompletos sin fallar.

    Falso positivo curado: un handler que re-lanza (``raise``), retorna, o
    asigna algo (marca el fallo en un dato) NO se marca.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if node.body and all(_is_noise_stmt(s) for s in node.body):
            return node.lineno
    return None


def strip_self_assignments(nt: NodeTranslation) -> NodeTranslation:
    """Elimina líneas ``X = X`` de nivel módulo antes del chequeo estático.

    Es el tic más frecuente del traductor (13 de 20 rechazos de una corrida
    real): "declara" parámetros de macro SAS con una autoasignación que el
    retry con aviso no corrige. Borrarla es semánticamente neutro — si ``X``
    existe la línea es un no-op; si no existe, el NameError salta igual en el
    primer uso real. Solo statements de nivel módulo y de una línea: dentro de
    un bloque, borrar la única sentencia dejaría un cuerpo vacío.

    Devuelve el mismo objeto si no había nada que sacar.
    """
    new_cells: list[str] = []
    removed: list[str] = []
    for cell in nt.cells:
        try:
            tree = parse_generated(cell)
        except SyntaxError:
            new_cells.append(cell)
            continue
        drop = {
            node.lineno
            for node in tree.body
            if isinstance(node, ast.Assign)
            and node.lineno == node.end_lineno
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
            and node.targets[0].id == node.value.id
        }
        if not drop:
            new_cells.append(cell)
            continue
        lines = cell.splitlines()
        removed.extend(lines[n - 1].strip() for n in sorted(drop))
        new_cells.append("\n".join(l for i, l in enumerate(lines, 1) if i not in drop))
    if not removed:
        return nt
    aviso = (
        "autofix: se eliminaron autoasignaciones sin efecto: "
        + ", ".join(f"'{r}'" for r in removed)
    )
    return nt.model_copy(update={"cells": new_cells, "warnings": [*nt.warnings, aviso]})


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


def check_node_translation_all(
    nt: NodeTranslation,
    allowed_imports: Iterable[str] | None = None,
) -> list[NodeAssemblyFailure]:
    """TODAS las fallas estáticas del nodo (deduplicadas), no solo la primera.

    Devolver solo la primera obligaba al retry a jugar al gato y al ratón: un
    nodo con 4 problemas gastaba 4 rondas de LLM para enterarse de a uno. Con
    la lista completa se corrige en una.
    """
    allowed = frozenset(allowed_imports) if allowed_imports is not None \
        else DEFAULT_ALLOWED_IMPORTS
    if not any(cell.strip() for cell in nt.cells):
        return [NodeAssemblyFailure(nt.node_id, "empty_translation", "sin celdas de código")]

    failures: list[NodeAssemblyFailure] = []

    def add(reason: str, detail: str) -> None:
        failure = NodeAssemblyFailure(nt.node_id, reason, detail)
        if failure not in failures:
            failures.append(failure)

    sources = list(nt.imports) + list(nt.cells)
    for src in sources:
        # Substrings prohibidos sobre el fuente SIN comentarios (un comentario
        # que menciona drop table no es un DROP TABLE). Los secretos, sobre el
        # fuente CRUDO: una credencial en un comentario sigue siendo un leak.
        code_only = _strip_comments(src).lower()
        for token in FORBIDDEN_SUBSTRINGS:
            if token in code_only:
                add("forbidden_pattern", f"patrón prohibido '{token}'")
        if _DROP_TABLE.search(code_only):
            add(
                "forbidden_pattern",
                "DROP TABLE destruye DDL (permisos, índices) — el reemplazo "
                "estilo SAS es DELETE FROM sin WHERE + append",
            )
        if _REPLACE_WRITE.search(code_only):
            add(
                "forbidden_pattern",
                "to_sql(if_exists='replace') destruye DDL — el reemplazo estilo "
                "SAS es DELETE FROM sin WHERE + append",
            )
        for pattern in _SECRET_PATTERNS:
            m = pattern.search(src)
            if m:
                add(
                    "secret_detected",
                    f"posible credencial literal en el código: '{m.group(0)[:30]}…' — "
                    "usar variables de entorno",
                )

    for i, cell in enumerate(nt.cells):
        comment = _placeholder_comment(cell)
        if comment is not None:
            add(
                "placeholder_stub",
                f"celda {i}: relleno anunciado en un comentario ({comment}). Un hueco "
                "va como raise NotImplementedError(...) — falla fuerte y se ve — "
                "nunca como valor vacío que el resto del código consume",
            )

    # Una celda con error de sintaxis se reporta y se excluye del análisis AST;
    # las demás celdas se siguen chequeando — el retry recibe el panorama entero.
    trees: list[ast.AST] = []
    cell_trees: list[tuple[int, ast.AST]] = []
    for i, cell in enumerate(nt.cells):
        try:
            tree = parse_generated(cell)
        except SyntaxError as exc:
            add("syntax_error", f"celda {i}: {exc.msg} (línea {exc.lineno})")
            continue
        trees.append(tree)
        cell_trees.append((i, tree))
    for line in nt.imports:
        try:
            trees.append(parse_generated(line))
        except SyntaxError as exc:
            add("syntax_error", f"import inválido '{line}': {exc.msg}")

    # El contrato dice que los imports van en `imports` (el ensamblador los
    # agrega a la celda de configuración, deduplicados). Un import dentro de
    # una celda escapa a ese dedupe y a la allowlist visible del requirements.
    for i, tree in cell_trees:
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
            add(
                "import_in_cell",
                f"celda {i}: import dentro de la celda — va en el campo "
                "`imports` del NodeTranslation, no en el código",
            )

    escape_row_loop = any(_ESCAPE_ROW_LOOP.search(c) for c in nt.cells)
    roots: set[str] = set()
    for tree in trees:
        sql = _fstring_sql(tree)
        if sql is not None:
            add(
                "forbidden_pattern",
                f"SQL con un VALOR interpolado por f-string: {sql} — el valor va "
                "como bind param (`text(\"... WHERE x = :x\")` + `params={'x': x}`). "
                "Interpolar el NOMBRE de una tabla sí está permitido "
                "(`f\"... FROM TABLAS.BD_R{ANIO}{TRIM}\"`): el SAS lo arma con la "
                "macro var y ningún driver parametriza identificadores",
            )
        sql = _sql_with_dead_placeholder(tree)
        if sql is not None:
            add(
                "forbidden_pattern",
                f"'{sql}' en un string SQL que no es f-string: las llaves viajan "
                "literales a la base. Si el nombre es dinámico, poné la `f`; si "
                "es un valor, va como bind param",
            )
        path = _absolute_path_literal(tree)
        if path is not None:
            add(
                "absolute_path",
                f"ruta absoluta literal '{path[:60]}' — el estándar es ruta "
                "relativa al workspace (declarar el cambio en warnings)",
            )
        lineno = _bare_except(tree)
        if lineno is not None:
            add(
                "bare_except",
                f"'except:' desnudo (línea {lineno}) — convierte un fallo real en "
                "un dato faltante silencioso; capturar la excepción concreta o "
                "dejar que propague",
            )
        name = _self_assignment(tree)
        if name is not None:
            add(
                "self_assignment",
                f"'{name} = {name}' no declara nada: si el nombre viene de un nodo "
                "anterior, la línea sobra; si no viene, es un NameError disfrazado",
            )
        sql = _sql_no_op(tree)
        if sql is not None:
            add(
                "sql_no_op",
                f"'WHERE 1=1' sin predicados: {sql} — el filtro del SAS original "
                "se perdió y la consulta trae la tabla entera",
            )
        lineno = _swallowed_exception(tree)
        if lineno is not None:
            add(
                "swallowed_exception",
                f"except cuyo cuerpo solo imprime/loguea/pasa (línea {lineno}): el "
                "fallo real se vuelve un dato faltante silencioso. Re-lanzar, "
                "capturar la excepción esperada con manejo real, o dejar propagar",
            )
        call = _row_by_row_write(tree)
        if call is not None and not escape_row_loop:
            add(
                "row_by_row_write",
                f"'{call}' dentro de un loop de iterrows(): un round-trip por fila. "
                "El APPEND de SAS se traduce con una escritura masiva (to_sql fuera "
                "del loop). Caso legítimo (SP por fila, API sin batch): comentario "
                "'# sas-migrator: permitir-loop-filas — <motivo>' en la celda",
            )
        roots |= _import_root_names(tree)

    frame = _empty_frame_guard(trees)
    if frame is not None:
        add(
            "empty_frame_guard",
            f"'{frame}' se asigna una sola vez, a un DataFrame vacío, y después se "
            "usa como condición: esa rama nunca corre y el nodo entrega números "
            "faltantes sin error. Cargar el dato de verdad o raise NotImplementedError",
        )

    for name in sorted(roots):
        if not _import_resolvable(name, allowed):
            add(
                "unresolvable_import",
                f"import no permitido: '{name}' — no es stdlib ni está en "
                "translation.allowed_imports (project_config.yaml)",
            )
    return failures


def check_node_translation(
    nt: NodeTranslation,
    allowed_imports: Iterable[str] | None = None,
) -> NodeAssemblyFailure | None:
    """Primer fallo estático del nodo, o None si es ensamblable.

    Local al nodo a propósito: así lo puede correr la fase 6 apenas traduce,
    antes de persistir, y usar el ``detail`` como feedback del reintento.
    """
    failures = check_node_translation_all(nt, allowed_imports)
    return failures[0] if failures else None


# ── Ensamblado ──────────────────────────────────────────────────────────────

def _canonical_nb_rel(notebook_path: str) -> str:
    name = Path(notebook_path).name
    return notebook_path if notebook_path.replace("\\", "/").startswith("output/") \
        else f"output/{name}"


def _title_cell_source(title: str, nb_targets: list[dict]) -> str:
    """Celda de título: nombre del notebook + Process Flow(s) de origen.

    El nombre del archivo es posicional (``NB-NN_<slug>``, NN = orden entre los
    flujos in-scope), así que excluir un flujo renumera a todos los siguientes.
    El ``pfd_id`` del .egp es el único identificador estable, y hasta acá no
    aparecía en ningún artefacto posterior a fase 5: para saber qué flujo era
    un notebook había que cruzar ``flow_summary.node_ids`` contra el plan.

    Con ``output_strategy: single`` un notebook junta varios flujos: se listan
    todos. Planes viejos sin ``pfd_id`` caen al título pelado de antes.
    """
    origins: list[tuple[str, str]] = []
    for target in nb_targets:
        pfd_id = str(target.get("pfd_id") or "")
        origin = (pfd_id, str(target.get("pfd_label") or ""))
        if pfd_id and origin not in origins:
            origins.append(origin)
    if not origins:
        return f"# {title}"
    lines = "\n".join(
        f"Process Flow SAS: **{label or pfd_id}** — `{pfd_id}`" for pfd_id, label in origins
    )
    return f"# {title}\n\n{lines}"


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
            tree = parse_generated(line)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names |= {
                    (alias.asname or alias.name.split(".")[0]) for alias in node.names
                }
    return names


def _flag_undefined_names(
    nodes: list[NodeTranslation], known: set[str]
) -> dict[str, NodeAssemblyFailure]:
    """Marca los nodos que usan nombres que el notebook nunca define.

    Recorre en orden de ejecución acumulando lo que cada nodo deja definido. Un
    nodo marcado igual aporta sus definiciones al set: así el nodo siguiente
    se juzga por lo suyo y no arrastra la culpa del anterior — se reporta la
    causa raíz una vez, no una cascada.

    Marca, no filtra: el nodo se emite igual (con banner). Un NameError visible
    al ejecutar es preferible a un notebook al que le falta la mitad del flujo
    sin que se note.
    """
    flagged: dict[str, NodeAssemblyFailure] = {}
    visible = set(known)
    for nt in nodes:
        missing, defined = undefined_in_cells(list(nt.cells), visible)
        visible |= defined | _names_bound_by_imports(list(nt.imports))
        if missing and nt.node_id not in flagged:
            flagged[nt.node_id] = NodeAssemblyFailure(
                nt.node_id, "undefined_name",
                f"usa {len(missing)} nombre(s) que ninguna celda anterior define: "
                f"{', '.join(missing[:8])}"
                + (" …" if len(missing) > 8 else "")
                + ". Si vienen de otro nodo, ese nodo los dejó en la base (hay que "
                "leerlos con pd.read_sql) o no existen",
            )
    return flagged


# Helper `_log` que se define en la celda de configuración cuando el usuario
# aprobó el logging por celda (decisión B5b). El try/except tragado es
# deliberado y vive SOLO acá: un log jamás puede convertir una corrida buena
# en fallida. Los chequeos estáticos no analizan esta celda (solo nt.cells).
_CELL_LOGGING_SNIPPET = '''
# Logging liviano de resultados — aprobado en la entrevista (Fase 4)
_LOG_PATH = Path("log") / "__LOG_FILE__"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
def _log(label, value=None):
    """Una línea por celda: imprime y persiste. Jamás rompe la corrida."""
    try:
        if hasattr(value, "shape"):
            detail = f"{value.shape[0]} filas x {value.shape[1]} cols"
        elif isinstance(value, int):
            detail = f"{value} filas"
        elif value is None:
            detail = "ok"
        else:
            detail = str(value)
        line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {label}: {detail}"
        print(line)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\\n")
    except Exception:
        pass  # el log nunca puede tumbar el notebook
with open(_LOG_PATH, "a", encoding="utf-8") as _fh:
    _fh.write(f"\\n=== corrida {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\\n")
'''


def _strip_log_calls(nt: NodeTranslation) -> NodeTranslation:
    """Elimina las llamadas ``_log(...)`` de una traducción (feature OFF).

    Defensa de coherencia para resume: ``state/translations/`` pudo generarse
    con la decisión B5b en ON y el ensamblado correr en OFF — sin esto cada
    ``_log`` caería como undefined_name. Se localizan por AST (statements Expr
    cuyo call es el nombre ``_log``) y se borran sus líneas, conservando el
    resto del texto tal cual.
    """
    changed = False
    cells: list[str] = []
    for cell in nt.cells:
        try:
            tree = parse_generated(cell)
        except SyntaxError:
            cells.append(cell)  # lo reporta el chequeo de sintaxis, no este
            continue
        drop: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_log"
            ):
                drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if not drop:
            cells.append(cell)
            continue
        changed = True
        kept = [
            line
            for i, line in enumerate(cell.splitlines(keepends=True), start=1)
            if i not in drop
        ]
        new_cell = "".join(kept)
        if new_cell.strip():
            cells.append(new_cell)
    if not changed or not cells:
        return nt  # sin _log, o el nodo era puro _log: que lo diga el chequeo
    return nt.model_copy(update={"cells": cells})


def _import_aislado(failure: NodeAssemblyFailure | None) -> bool:
    """¿Los imports de este nodo tienen que quedarse en su propia celda?"""
    return failure is not None and failure.reason == "unresolvable_import"


def _db_bootstrap_source(db_url_default: str) -> str:
    """Celda de configuración: cómo obtiene ``engine`` el notebook.

    El orquestador (ejecución autorizada) sigue mandando vía ``SASMIG_DB_URL``.
    Lo que cambia es qué pasa si nadie la exporta: antes ``KeyError`` en la
    celda 1 y notebook muerto; ahora cae a la cadena canónica del proyecto
    (``core/db/engine.connection_string``), con autenticación integrada y sin
    credenciales en el código. Si el proyecto tampoco define un servidor, el
    caller manda cadena vacía y se conserva el error explícito de antes.
    """
    if not db_url_default:
        return (
            "\n# Conexión a BD — la define el orquestador al ejecutar\n"
            'engine = sqlalchemy.create_engine(os.environ["SASMIG_DB_URL"])\n'
        )
    return (
        "\n# Conexión a BD — la fija el orquestador con SASMIG_DB_URL; si no está,\n"
        "# se usa la conexión por defecto del proyecto (SUPUESTO: verificar servidor\n"
        "# y base antes de correr contra datos reales).\n"
        "engine = sqlalchemy.create_engine(\n"
        f"    os.environ.get(\"SASMIG_DB_URL\", {db_url_default!r})\n"
        ")\n"
    )


def _hueco_msg(label: str, failure: NodeAssemblyFailure) -> str:
    """Mensaje del ``NotImplementedError`` de un nodo que no se pudo emitir."""
    return (
        f"{label}: traducción no emitible ({failure.reason}). "
        "El código original quedó en la celda markdown de arriba; "
        "el detalle, en state/needs_human.yaml."
    )


def _node_markdown(
    label: str, nt: NodeTranslation, failure: NodeAssemblyFailure | None, verdict: str
) -> str:
    """Celda markdown que precede a cada nodo.

    ``## {label}`` va SIEMPRE en su propia línea: es el ancla de trazabilidad
    que chequea el audit. Debajo, lo que hasta ahora se persistía y nunca
    llegaba al notebook —confianza, veredicto del verificador, supuestos— y,
    si el nodo falló un chequeo, por qué está marcado.
    """
    conf = str(getattr(nt.confidence, "value", nt.confidence))
    construct = (nt.traceability.sas_construct or "").strip()

    if failure is None:
        partes = [f"confianza: {conf}"]
        if verdict:
            partes.append(f"verificador: {verdict}")
        if construct:
            partes.append(f"SAS: {construct}")
        return f"## {label}\n\n*{' · '.join(partes)}*"

    linea_conf = f"**Confianza del traductor:** {conf}"
    if verdict:
        linea_conf += f" · **Verificador:** {verdict}"
    bloque = [
        "> ⚠️ **Nodo degradado — revisar antes de ejecutar**",
        f"> **Chequeo fallido:** `{failure.reason}` — {failure.detail}",
        f"> {linea_conf}",
    ]
    if nt.warnings:
        bloque.append("> **Supuestos del traductor:**")
        bloque.append("\n".join(f"> - {w}" for w in nt.warnings))
    if construct:
        bloque.append(f"> **SAS de origen:** {construct}")
    if not failure.emitted:
        bloque.append(
            "> **El código no se pudo emitir** (no parsea o está vacío): queda "
            "abajo, en texto, y la celda levanta `NotImplementedError`."
        )
    partes = [f"## {label}", "\n>\n".join(bloque)]
    if not failure.emitted:
        original = "\n\n".join(c.rstrip() for c in nt.cells if c.strip())
        if original:
            partes.append(f"```python\n{original}\n```")
    return "\n\n".join(partes)


def assemble_notebooks(
    plan: dict,
    translations: dict[str, NodeTranslation],
    output_dir: Path,
    *,
    db_bootstrap: bool = False,
    db_url_default: str = "",
    allowed_imports: Iterable[str] | None = None,
    cell_logging: bool = False,
    verdicts: dict[str, str] | None = None,
) -> tuple[SasPythonMapping, list[NodeAssemblyFailure]]:
    """Construye los notebooks del plan y el mapping SAS→Python.

    ``translations``: NodeTranslation por node_id. Un target sin traducción se
    omite en silencio aquí (el caller ya lo registró como needs_human). Un
    target cuya traducción falla un chequeo estático SÍ se emite, precedido de
    un banner con el motivo y marcado ``degraded`` en el mapping, y se devuelve
    igual como ``NodeAssemblyFailure`` (el caller lo sigue registrando en
    needs_human, y el audit lo bloquea con un issue ``degraded`` high). Solo dos
    kinds no se pueden emitir tal cual —ver ``NON_EMITTABLE_KINDS``—: ahí va el
    banner, el código original en markdown y una celda que levanta
    ``NotImplementedError``.

    ``verdicts``: node_id → veredicto del verificador LLM, para la etiqueta de
    confianza de cada nodo. Sale de ``state/translation_review.json``, que lee
    el caller: este módulo es determinista puro y no toca ``state/``.

    ``db_url_default``: cadena de conexión canónica del proyecto, para cuando
    nadie exporta ``SASMIG_DB_URL``. La construye el caller por la misma razón.

    Además emite ``output/requirements.txt`` con las librerías de terceros que
    los notebooks realmente importan — el contrato del entorno DESTINO.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    verdicts = verdicts or {}
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

        nodes: list[NodeTranslation] = []
        degraded: dict[str, NodeAssemblyFailure] = {}
        for target in nb_targets:
            nid = str(target.get("node_id"))
            nt = translations.get(nid)
            if nt is None:
                continue  # sin traducción: needs_human ya registrado aguas arriba
            if not cell_logging:
                nt = _strip_log_calls(nt)
            if nt.strategy and target.get("strategy") and nt.strategy != target["strategy"]:
                failure = NodeAssemblyFailure(
                    nid, "strategy_mismatch",
                    f"traducción '{nt.strategy}' vs plan '{target['strategy']}'",
                )
            else:
                failure = check_node_translation(nt, allowed_imports)
            if failure is not None:
                failure.emitted = failure.reason not in NON_EMITTABLE_KINDS
                degraded[nid] = failure
            nodes.append(nt)

        # Celda de configuración: imports agregados (dedupe, primera aparición).
        imports: list[str] = list(BASELINE_IMPORTS)
        if db_bootstrap:
            for line in ("import os", "import sqlalchemy"):
                if line not in imports:
                    imports.append(line)
        if cell_logging:
            # stdlib para el helper _log; _write_requirements los filtra.
            for line in ("import datetime", "from pathlib import Path"):
                if line not in imports:
                    imports.append(line)
        aislados: list[str] = []
        for nt in nodes:
            for line in nt.imports:
                line = line.strip()
                if not line:
                    continue
                # Un import que no resuelve iría a la celda 1 y tumbaría el
                # notebook ENTERO antes del primer nodo. Se aísla en la celda
                # del nodo que lo pidió: solo ese nodo revienta.
                if _import_aislado(degraded.get(nt.node_id)):
                    if line not in aislados:
                        aislados.append(line)
                elif line not in imports:
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
        if cell_logging:
            known.add("_log")  # lo define la celda de configuración
        for nid, failure in _flag_undefined_names(nodes, known).items():
            # Un nodo reporta UN motivo: el primero que encontró. Encadenarle
            # undefined_name a un nodo que ya falló otra cosa sería ruido.
            degraded.setdefault(nid, failure)

        failures.extend(degraded[str(t.get("node_id"))] for t in nb_targets
                        if str(t.get("node_id")) in degraded)

        config_source = (
            "# ========= Celda 1: Configuración =========\n" + "\n".join(imports) + "\n"
        )
        if db_bootstrap:
            config_source += _db_bootstrap_source(db_url_default)
        if cell_logging:
            # El notebook corre con cwd=output/ (nbclient y run_all.py), así
            # que Path("log") resuelve a output/log/.
            config_source += _CELL_LOGGING_SNIPPET.replace("__LOG_FILE__", f"{title}.log")

        nb = nbformat.v4.new_notebook()
        cells = [nbformat.v4.new_markdown_cell(_title_cell_source(title, nb_targets))]
        params_cell = _parameters_cell(nb_targets, plan.get("macro_param_values"))
        if params_cell is not None:
            cells.append(params_cell)
        cells.append(nbformat.v4.new_code_cell(config_source))

        for nt in nodes:
            label = nt.node_label or nt.node_id
            failure = degraded.get(nt.node_id)
            cells.append(nbformat.v4.new_markdown_cell(
                _node_markdown(label, nt, failure, verdicts.get(nt.node_id, ""))
            ))
            code_index = len(cells)  # índice REAL de la primera celda code

            if failure is not None and not failure.emitted:
                cells.append(nbformat.v4.new_code_cell(
                    f"# ========= {label} =========\n"
                    f"raise NotImplementedError(\n"
                    f"    {_hueco_msg(label, failure)!r}\n"
                    f")\n"
                ))
                extra_cells = 0
            else:
                cabecera = f"# ========= {label} =========\n"
                if _import_aislado(failure):
                    cabecera += (
                        "# imports propios del nodo: no resuelven en el entorno "
                        "declarado, así que\n# quedan acá y no en la celda de "
                        "configuración (rompe este nodo, no el notebook).\n"
                        + "\n".join(line.strip() for line in nt.imports if line.strip())
                        + "\n"
                    )
                first, *rest = [c for c in nt.cells if c.strip()]
                cells.append(nbformat.v4.new_code_cell(f"{cabecera}{first}"))
                for extra in rest:
                    cells.append(nbformat.v4.new_code_cell(extra))
                extra_cells = len(rest)

            entries.append(
                MappingEntry(
                    node_id=nt.node_id,
                    node_label=label,
                    sas_construct=nt.traceability.sas_construct,
                    python_artifact=nb_rel,
                    notebook_path=nb_rel,
                    cell_index=code_index,
                    cell_count=1 + extra_cells,
                    business_rule=nt.traceability.business_rule,
                    confidence=nt.confidence,
                    degraded=failure is not None,
                    degraded_reason=(
                        f"{failure.reason}: {failure.detail}" if failure else ""
                    ),
                )
            )

        # nbformat asigna ids aleatorios; ids fijos por posición = determinismo.
        for i, cell in enumerate(cells):
            cell["id"] = f"cell-{i:03d}"
        nb["cells"] = cells
        nbformat.write(nb, str(output_dir / name))
        # Los aislados también son contrato del entorno destino: que no resuelvan
        # HOY es justamente lo que requirements.txt tiene que dejar dicho.
        all_import_lines.extend(imports + aislados)

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
            roots |= _import_root_names(parse_generated(line))
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
