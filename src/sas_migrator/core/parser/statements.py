"""Parsers dirigidos por statement — capa 2 del parser v2.

No es un AST completo de SAS: son parsers específicos para los constructs que
alimentan lineage y placement (DATA/SET/MERGE, PROC SQL, LIBNAME, CONNECT TO,
IMPORT/EXPORT, data= / out= de PROCs). Casos que el v1 perdía y aquí se cubren:

- ``DATA a b;`` → ambos outputs (v1: solo ``a``).
- ``SET x y;`` → ambos inputs (v1: solo ``x``).
- ``CREATE TABLE t AS SELECT`` → output (v1: invisible — el construct más
  frecuente de EG).
- ``INNER/LEFT/RIGHT/FULL JOIN t`` → inputs (v1: solo FROM).
- ``LIBNAME x`` de 1 letra (v1: regex exigía ≥2 caracteres).
- ``&lib..tabla`` → se registra como referencia macro-dependiente, no se pierde
  en silencio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sas_migrator.core.config import load_project_config
from sas_migrator.core.parser.tokenizer import code_statements, words

# Engines de BD reconocidos en LIBNAME — única definición del proyecto
# (analysis/indexes.py y analysis/analyze.py importan de aquí). Cubre los
# nombres de engine reales de SAS/ACCESS; el migrador es genérico, así que la
# lista es amplia y además extensible por project_config.yaml
# (parser.extra_db_engines / parser.ignore_db_engines → resolve_db_engines).
DEFAULT_DB_ENGINES = frozenset({
    # genéricos / puentes
    "ODBC", "OLEDB", "JDBC",
    # relacionales comerciales
    "ORACLE", "DB2", "TERADATA", "SQLSVR", "SYBASE", "SAPASE", "SAPIQ",
    "SAPHANA", "INFORMIX",
    # open source
    "MYSQL", "POSTGRES",
    # MPP / analíticas
    "NETEZZA", "GREENPLM", "VERTICA", "ASTER", "HAWQ", "YBRICK",
    # cloud / big data
    "REDSHIFT", "SNOW", "BIGQUERY", "SPARK", "HADOOP", "IMPALA",
    "SINGLESTORE",
    # NoSQL / SaaS
    "MONGO", "SFORCE",
    # alias frecuentes en código real (no oficiales pero inequívocos)
    "SQLSRV", "MSSQL",
})


def resolve_db_engines(workspace: Path | str | None = None) -> frozenset[str]:
    """Set efectivo de engines de BD para un workspace.

    Defaults + ``parser.extra_db_engines`` − ``parser.ignore_db_engines`` de
    project_config.yaml. Sin workspace (o sin config) aplican los defaults.
    """
    cfg = load_project_config(workspace)
    extra = {e.upper() for e in cfg.parser.extra_db_engines}
    ignore = {e.upper() for e in cfg.parser.ignore_db_engines}
    return frozenset((set(DEFAULT_DB_ENGINES) | extra) - ignore)

_DS_OPTS = re.compile(r"\(([^()]|\([^()]*\))*\)")  # opciones (keep=...) con 1 nivel de anidación
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_MACRO_REF = re.compile(r"&\w+")

# ── Lógica de DATA step: procedural vs. expresable en SQL ────────────────────
# La distinción decide el placement. Un DATA step con `nuevo = monto * 2` o un
# `if monto > 0;` es un CASE WHEN y un WHERE: se traduce a SQL sin perder nada.
# Uno con RETAIN, LAG, FIRST./LAST. o DO depende del ORDEN de las filas y del
# estado que sobrevive de una iteración a la otra — eso no tiene equivalente
# declarativo y va a pandas. Tratar ambos como "lógica fila a fila" (el
# comportamiento anterior) empujaba a pandas todo lo que tocara un DATA step.

# Statements declarativos: no son lógica, son metadatos de la tabla.
_DECLARATIVE_HEADS = frozenset({
    "by", "where", "format", "label", "keep", "drop", "rename", "length", "attrib",
})
# Heads sin equivalente declarativo: control de flujo, I/O crudo, estado.
_PROCEDURAL_HEADS = frozenset({
    "retain", "array", "do", "end", "output", "link", "goto", "return", "abort",
    "stop", "delete", "input", "put", "infile", "file", "modify", "remove",
    "replace", "call",
})
# Los mismos constructs cuando aparecen DENTRO de otro statement
# (`if x then output;`, `total = lag(monto);`).
_PROCEDURAL_TOKENS = re.compile(
    r"\b(?:lag|dif)\d*\s*\(|\b_n_\b|\b_i_\b|\bfirst\.|\blast\.|"
    r"\b(?:end|point|nobs|curobs)\s*=|"
    r"\b(?:output|delete|stop|abort|link|goto|return|call)\b",
    re.IGNORECASE,
)
# `acumulado + monto;` — statement SUM: un RETAIN implícito sin la palabra.
_SUM_STATEMENT = re.compile(r"^[A-Za-z_]\w*(?:\[[^\]]*\])?\s*\+")

# Datasets SAS referenciados por RUTA entre comillas — `FROM '/x/base.sas7bdat'`.
# SAS podía hacerlo porque él mismo era el motor SQL; en la migración es una
# INGESTA (leer el archivo y subirlo), no una tabla consultable. words() blanquea
# los strings a "<str>", así que estas referencias no llegaban a ninguna parte:
# el nodo declaraba cero file_reads y la dependencia quedaba invisible para
# lineage y preflight.
_SAS_DATA_FILE = re.compile(
    r"""(['"])([^'"]*\.(?:sas7bdat|sas7bvew|sd2|sd7|xpt))\1""", re.IGNORECASE
)
_FILE_AS_TARGET = re.compile(r"create\s+table\s*['\"]", re.IGNORECASE)

# Palabras que siguen a FROM/JOIN pero no son datasets.
_SQL_STOPWORDS = {"connection", "select", "where", "on", "as", "dual"}


@dataclass
class DatasetRef:
    """Referencia a un dataset con su origen sintáctico."""

    libref: str  # "WORK" si no calificado
    table: str
    raw: str
    statement_kind: str  # data | set | merge | sql_from | sql_join | sql_create | ...
    macro_dependent: bool = False


@dataclass
class NodeParse:
    """Resultado del parseo de un nodo SAS."""

    inputs: list[DatasetRef] = field(default_factory=list)
    outputs: list[DatasetRef] = field(default_factory=list)
    librefs_declared: dict[str, str] = field(default_factory=dict)  # libref → engine|BASE|PATH
    passthrough_aliases: list[str] = field(default_factory=list)  # CONNECT TO ... AS x
    has_passthrough: bool = False
    file_reads: list[str] = field(default_factory=list)
    file_writes: list[str] = field(default_factory=list)
    macro_refs: list[str] = field(default_factory=list)  # refs &lib..tabla sin resolver
    procs: list[str] = field(default_factory=list)
    has_data_step_logic: bool = False  # DATA step con lógica (procedural o no)
    # Desglose de has_data_step_logic: solo lo procedural fuerza pandas.
    has_procedural_logic: bool = False  # retain/lag/do/first.-last./output/I-O crudo
    has_sql_expressible_logic: bool = False  # asignaciones, if-then, subsetting if
    procedural_evidence: list[str] = field(default_factory=list)
    dataset_files: list[str] = field(default_factory=list)  # rutas .sas7bdat citadas
    statements_total: int = 0


def _clean_ds(token: str) -> str | None:
    """Normaliza un token candidato a dataset; None si no es identificador."""
    token = _DS_OPTS.sub("", token).strip().rstrip(",")
    if not token or token.startswith("<"):
        return None
    if token.upper() in ("_NULL_",):
        return None
    if _MACRO_REF.search(token):
        return token  # macro-dependiente: el caller lo marca
    if not _IDENT.match(token):
        return None
    return token


def _to_ref(token: str, kind: str) -> DatasetRef | None:
    cleaned = _clean_ds(token)
    if cleaned is None:
        return None
    macro = bool(_MACRO_REF.search(cleaned))
    if macro:
        return DatasetRef(libref="&MACRO", table=cleaned, raw=token, statement_kind=kind,
                          macro_dependent=True)
    if "." in cleaned:
        lib, table = cleaned.split(".", 1)
        return DatasetRef(libref=lib.upper(), table=table.upper(), raw=token, statement_kind=kind)
    return DatasetRef(libref="WORK", table=cleaned.upper(), raw=token, statement_kind=kind)


def _dataset_list(stmt: str, keyword: str, kind: str) -> list[DatasetRef]:
    """Datasets tras un keyword (DATA/SET/MERGE/UPDATE), sobre el texto crudo.

    Se opera sobre el statement (no sobre words()) porque los '=' de opciones
    (``nobs=n``, ``/ view=x``) marcan el fin de la lista y la tokenización por
    palabras los destruye. Las opciones entre paréntesis se eliminan antes.
    """
    m = re.match(rf"\s*{keyword}\b(.*)$", stmt, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    rest = _DS_OPTS.sub(" ", m.group(1))
    refs: list[DatasetRef] = []
    for tok in rest.split():
        if "=" in tok or tok.startswith("/"):
            break
        ref = _to_ref(tok, kind)
        if ref:
            refs.append(ref)
    return refs


def _parse_sql_statement(stmt: str, parse: NodeParse) -> None:
    """CREATE TABLE / INSERT / DELETE / UPDATE / SELECT dentro de PROC SQL."""
    low = re.sub(r"\s+", " ", stmt).strip().lower()
    toks = words(stmt)
    toks_low = [t.lower() for t in toks]

    def _tok_after(*names: str) -> list[tuple[str, str]]:
        """(token, keyword) para cada aparición de keyword-secuencia."""
        found = []
        for i, t in enumerate(toks_low):
            for name in names:
                parts = name.split()
                if toks_low[i : i + len(parts)] == parts and i + len(parts) < len(toks):
                    found.append((toks[i + len(parts)], parts[0]))
        return found

    # passthrough: connect to X [as Y] / from connection to X / execute ( ) by X
    if "connect to" in low:
        parse.has_passthrough = True
        for m in re.finditer(r"connect\s+to\s+(\w+)(?:\s+as\s+(\w+))?", low):
            parse.passthrough_aliases.append((m.group(2) or m.group(1)).upper())
    if "connection to" in low or re.search(r"\bexecute\b.*\bby\b", low):
        parse.has_passthrough = True

    for tok, _ in _tok_after("create table", "create view"):
        ref = _to_ref(tok, "sql_create")
        if ref is None:
            continue
        if ref.macro_dependent:
            parse.macro_refs.append(ref.table)
        else:
            parse.outputs.append(ref)

    for tok, _ in _tok_after(
        "insert into", "delete from", "update", "alter table", "truncate table"
    ):
        # ALTER TABLE es DDL sobre una tabla existente: sin registrarlo, un
        # bloque `PROC SQL; alter table tablas.T add PROC CHAR; QUIT;` no
        # declaraba NINGUNA referencia a datos y el clasificador lo llamaba
        # "utility" — de ahí que la traducción terminara agregando la columna al
        # DataFrame en vez de a la tabla.
        # "update" también matchea UPDATE de DATA step merge — solo en SQL llegamos aquí
        ref = _to_ref(tok, "sql_write")
        if ref and not ref.macro_dependent:
            parse.outputs.append(ref)
        elif ref:
            parse.macro_refs.append(ref.table)

    for tok, kw in _tok_after("from", "join"):
        if tok.lower() in _SQL_STOPWORDS or tok.startswith("("):
            continue
        ref = _to_ref(tok, f"sql_{kw}")
        if ref and not ref.macro_dependent:
            parse.inputs.append(ref)
        elif ref:
            parse.macro_refs.append(ref.table)


def parse_sas_code(code: str) -> NodeParse:
    """Parsea el código SAS de un nodo y devuelve inputs/outputs/evidencia."""
    # Los code.sas de un .egp real traen BOM UTF-8; pegado al primer token
    # rompía el head del primer statement (bug cazado por el harness).
    code = code.replace("﻿", "")
    parse = NodeParse()
    statements = code_statements(code)
    parse.statements_total = len(statements)

    in_sql = False
    in_data_step = False

    for stmt in statements:
        toks = words(stmt)
        if not toks:
            continue
        head = toks[0].lower()

        # Dataset SAS referenciado por ruta citada. Va antes de cualquier
        # `continue`: aparece tanto dentro de PROC SQL (`FROM '...sas7bdat'`)
        # como en un DATA step (`SET '...sas7bdat'`).
        for m in _SAS_DATA_FILE.finditer(stmt):
            path = m.group(2)
            if path in parse.dataset_files:
                continue
            parse.dataset_files.append(path)
            escribe = head == "data" or bool(_FILE_AS_TARGET.search(stmt))
            (parse.file_writes if escribe else parse.file_reads).append(path)

        # bloques PROC SQL
        if head == "proc" and len(toks) > 1:
            proc_name = toks[1].lower()
            parse.procs.append(proc_name)
            in_data_step = False
            in_sql = proc_name == "sql"
            # data= / out= genéricos de PROCs — sobre el statement crudo,
            # porque words() destruye los '=' (era código muerto en el primer
            # borrador; el test de PROC SORT lo protege ahora).
            clean = _DS_OPTS.sub(" ", stmt)
            for m in re.finditer(r"\b(data|out|outdata|base)\s*=\s*([\w&.]+)", clean,
                                 flags=re.IGNORECASE):
                kind_key = m.group(1).lower()
                ref = _to_ref(m.group(2), f"proc_{proc_name}_{kind_key}")
                if ref is None:
                    continue
                target = parse.inputs if kind_key == "data" else parse.outputs
                if ref.macro_dependent:
                    parse.macro_refs.append(ref.table)
                else:
                    target.append(ref)
            # IMPORT/EXPORT: archivos
            if proc_name in ("import", "export"):
                m = re.search(r"(datafile|outfile)\s*=\s*(\"[^\"]+\"|'[^']+'|\S+)", stmt,
                              flags=re.IGNORECASE)
                if m:
                    path = m.group(2).strip("\"'")
                    if proc_name == "import":
                        parse.file_reads.append(path)
                    else:
                        parse.file_writes.append(path)
            continue

        if head in ("quit", "run"):
            in_sql = False
            in_data_step = False
            continue

        if in_sql:
            _parse_sql_statement(stmt, parse)
            continue

        if head == "libname" and len(toks) > 1:
            # El parser registra el engine tal cual (hecho sintáctico); decidir
            # si es un engine de BD es del clasificador, con el set resuelto
            # por config (placement.classify_placement / resolve_db_engines).
            libref = toks[1].upper()
            engine = toks[2].upper() if len(toks) > 2 and _IDENT.match(toks[2]) else ""
            parse.librefs_declared[libref] = engine or "PATH"
            continue

        if head == "append":
            # `PROC DATASETS; append base=tablas.T data=WORK.X force;` — el
            # APPEND va en un statement propio, no en el del PROC, así que el
            # handler genérico de `data=`/`out=` no lo veía: ni la tabla destino
            # ni la fuente llegaban a lineage. Es el patrón canónico de
            # acumulación en SAS (1188 apariciones en Síntesis_M_CR18) y en SQL
            # es un INSERT INTO base SELECT * FROM data.
            clean = _DS_OPTS.sub(" ", stmt)
            for m in re.finditer(r"\b(base|data)\s*=\s*([\w&.]+)", clean,
                                 flags=re.IGNORECASE):
                kind_key = m.group(1).lower()
                ref = _to_ref(m.group(2), f"append_{kind_key}")
                if ref is None:
                    continue
                target = parse.outputs if kind_key == "base" else parse.inputs
                if ref.macro_dependent:
                    parse.macro_refs.append(ref.table)
                else:
                    target.append(ref)
            continue

        if head == "data":
            in_data_step = True
            for ref in _dataset_list(stmt, "data", "data"):
                if ref.macro_dependent:
                    parse.macro_refs.append(ref.table)
                else:
                    parse.outputs.append(ref)
            continue

        if head in ("set", "merge", "update") and in_data_step:
            for ref in _dataset_list(stmt, head, head):
                if ref.macro_dependent:
                    parse.macro_refs.append(ref.table)
                else:
                    parse.inputs.append(ref)
            continue

        if in_data_step and head not in _DECLARATIVE_HEADS:
            # Hay lógica. Que sea procedural o no decide si puede ir a SQL.
            parse.has_data_step_logic = True
            if (
                head in _PROCEDURAL_HEADS
                or _PROCEDURAL_TOKENS.search(stmt)
                or _SUM_STATEMENT.match(stmt)
            ):
                parse.has_procedural_logic = True
                if len(parse.procedural_evidence) < 20:
                    parse.procedural_evidence.append(re.sub(r"\s+", " ", stmt)[:100])
            else:
                parse.has_sql_expressible_logic = True

        # infile/file = archivos en DATA step
        if head == "infile":
            m = re.search(r"infile\s+(\"[^\"]+\"|'[^']+'|\S+)", stmt, flags=re.IGNORECASE)
            if m:
                parse.file_reads.append(m.group(1).strip("\"'"))
        if head == "file" and in_data_step:
            m = re.search(r"file\s+(\"[^\"]+\"|'[^']+'|\S+)", stmt, flags=re.IGNORECASE)
            if m:
                parse.file_writes.append(m.group(1).strip("\"'"))

    # dedup preservando orden
    def _dedup(refs: list[DatasetRef]) -> list[DatasetRef]:
        seen: set[tuple[str, str]] = set()
        out = []
        for r in refs:
            key = (r.libref, r.table)
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    parse.inputs = _dedup(parse.inputs)
    parse.outputs = _dedup(parse.outputs)
    parse.macro_refs = sorted(set(parse.macro_refs))
    return parse
