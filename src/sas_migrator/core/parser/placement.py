"""Clasificador de placement — ¿dónde se ejecuta el procesamiento de cada nodo?

Decisión de arquitectura v2 (acordada con el analista): el SQL se ejecuta donde
viven los datos (SQL Server); pandas corre en el servidor; no existe motor SQL
local. El placement se deriva de la ESTRUCTURA del código SAS:

- ``sql_passthrough``: el nodo ya ejecutaba en la BD (CONNECT TO / EXECUTE BY /
  CONNECTION TO). Se traduce a SQL que sigue corriendo allá.
- ``sql_pushdown``: entradas y salidas son librefs de BD y la lógica es PROC SQL
  puro → se traduce a SQL ejecutado en la BD (DELETE+INSERT INTO...SELECT).
- ``pandas``: entradas solo archivos/WORK (o lógica DATA step fila a fila sin
  tablas BD) → pandas en el servidor.
- ``hybrid``: mezcla BD + archivos/WORK, o lógica no expresable en SQL sobre
  tablas BD → extraer lo mínimo con filtros en el WHERE, procesar en pandas,
  escritura idempotente.
- ``ambiguous``: referencias macro-dependientes sin resolver u otra evidencia
  insuficiente → va a entrevista (bloque B4b), jamás se adivina.
- ``utility``: nodo sin referencias a datos (definiciones ``%macro``, ``%let``,
  options) → traduce a Python plano; no participa de la entrevista B4b.

El default replica la localidad que SAS ya tenía; mover cómputo de lugar es una
mejora M-xxx aprobada, no una decisión silenciosa del traductor.

``sql_first=True`` (``translation.default_target: sql``) corrige dos supuestos
que empujaban a pandas casi todo nodo real: WORK pasa a ser un schema temporal
del server (no memoria del proceso) y solo la lógica PROCEDURAL de un DATA step
—no cualquier statement dentro de uno— impide el pushdown. Ver
``core.parser.segments`` para el veredicto POR BLOQUE: un nodo que ingesta un
archivo en su primer PROC SQL no tiene por qué traducir a pandas los diez
``UPDATE`` que le siguen.

Nota de ejes: ``classification == "utility"`` (analysis/analyze.py) describe QUÉ
es el nodo dentro del pipeline ETL; ``placement == "utility"`` describe DÓNDE
corre su traducción (en ningún motor de datos). Un nodo puede ser
``classification=macro`` y ``placement=utility`` a la vez.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sas_migrator.core.parser.statements import DEFAULT_DB_ENGINES, NodeParse

PLACEMENTS = ("sql_passthrough", "sql_pushdown", "pandas", "hybrid", "ambiguous", "utility")

# PROCs cuya semántica NO sobrevive una traducción ingenua a SQL porque su
# resultado es un ORDEN de filas, y una tabla SQL no tiene orden: un
# ``SELECT ... INTO #t ... ORDER BY`` no garantiza que el consumidor lea en ese
# orden. En SAS el dataset queda marcado ``sortedby`` y el DATA step siguiente
# procesa BY-grupos confiando en eso. Bajo ``sql_first`` estos PROCs cuentan
# como lógica procedural: se quedan en pandas hasta que exista una traducción
# que lleve el ORDER BY al consumidor (es una mejora M-xxx, no un default).
_ORDER_DEPENDENT_PROCS = frozenset({"sort", "rank", "transpose", "expand", "timeseries"})


@dataclass
class PlacementDecision:
    placement: str
    evidence: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _origin(libref: str, db_librefs: set[str], work_is_db: bool = False) -> str:
    """Origen de un dataset: db | work | lib_desconocida."""
    if libref == "WORK":
        return "db" if work_is_db else "work"
    if libref.upper() in db_librefs:
        return "db"
    return "unknown_lib"


def classify_placement(
    parse: NodeParse,
    db_librefs: set[str] | None = None,
    db_engines: frozenset[str] | set[str] | None = None,
    *,
    sql_first: bool = False,
) -> PlacementDecision:
    """Clasifica un nodo parseado.

    ``db_librefs``: librefs confirmados como BD a nivel proyecto (LIBNAME con
    engine de BD en cualquier nodo + confirmaciones de la entrevista B4b).
    Los LIBNAME del propio nodo se agregan automáticamente.

    ``db_engines``: set de engines de BD a reconocer (default: los de SAS/ACCESS;
    los proyectos con engines custom lo resuelven vía ``resolve_db_engines``).

    ``sql_first`` (``translation.default_target: sql``) cambia dos supuestos:

    1. ``WORK`` es un SCHEMA TEMPORAL del server, no memoria del proceso. La
       traducción fiel de ``WORK.X`` es una tabla temp, no un DataFrame — así
       que mezclar tablas de BD con WORK no obliga a bajar los datos al cliente.
    2. Solo la lógica PROCEDURAL de un DATA step fuerza pandas. Una asignación
       o un ``if`` son ``CASE WHEN``/``WHERE``, y arrastraban el nodo entero a
       pandas por estar dentro de un DATA step.

    Sin el flag el comportamiento es idéntico al histórico (WORK ⇒ pandas,
    cualquier statement de DATA step ⇒ no-SQL).
    """
    engines = db_engines if db_engines is not None else DEFAULT_DB_ENGINES
    db_libs = {lr.upper() for lr in (db_librefs or set())}
    db_libs |= {lr for lr, eng in parse.librefs_declared.items() if eng in engines}
    procs_de_orden = sorted(set(parse.procs) & _ORDER_DEPENDENT_PROCS)
    fuerza_pandas = (
        (parse.has_procedural_logic or bool(procs_de_orden))
        if sql_first
        else parse.has_data_step_logic
    )
    # WORK es una tabla temporal DEL SERVER — pero solo si hay server. Un
    # proyecto sin ningún libref de BD confirmado no tiene dónde poner una tabla
    # temp, y pedirle una conexión a un flujo que solo usa WORK es inventarle
    # infraestructura.
    work_is_db = sql_first and bool(db_libs)

    in_origins = {_origin(r.libref, db_libs, work_is_db) for r in parse.inputs}
    out_origins = {_origin(r.libref, db_libs, work_is_db) for r in parse.outputs}
    evidence = {
        "inputs": [f"{r.libref}.{r.table}" for r in parse.inputs],
        "outputs": [f"{r.libref}.{r.table}" for r in parse.outputs],
        "db_librefs": sorted(db_libs),
        "file_reads": parse.file_reads,
        "file_writes": parse.file_writes,
        "macro_refs": parse.macro_refs,
        "procs": parse.procs,
        "has_passthrough": parse.has_passthrough,
        "has_data_step_logic": parse.has_data_step_logic,
        "has_procedural_logic": parse.has_procedural_logic,
        "has_sql_expressible_logic": parse.has_sql_expressible_logic,
        "procedural_evidence": parse.procedural_evidence,
        "order_dependent_procs": procs_de_orden,
        "dataset_files": parse.dataset_files,
        "sql_first": sql_first,
        "work_as_temp_table": work_is_db,
    }

    def decision(placement: str, *reasons: str) -> PlacementDecision:
        return PlacementDecision(placement=placement, evidence=evidence, reasons=list(reasons))

    # 1. Passthrough explícito: ya corría en la BD.
    if parse.has_passthrough:
        return decision("sql_passthrough", "CONNECT TO / CONNECTION TO / EXECUTE BY presente")

    # 2. Referencias macro sin resolver tocando datasets → no se adivina.
    if parse.macro_refs and not parse.inputs and not parse.outputs:
        return decision("ambiguous", f"solo referencias macro-dependientes: {parse.macro_refs[:5]}")

    touches_db = "db" in in_origins or "db" in out_origins
    touches_files = bool(parse.file_reads or parse.file_writes)
    touches_work = "work" in in_origins or "work" in out_origins
    unknown = "unknown_lib" in in_origins or "unknown_lib" in out_origins

    # 3. Librefs no confirmados → ambiguo con evidencia (va a B4b).
    if unknown:
        unknown_libs = sorted(
            {
                r.libref
                for r in [*parse.inputs, *parse.outputs]
                if _origin(r.libref, db_libs, work_is_db) == "unknown_lib"
            }
        )
        return decision("ambiguous", f"librefs sin confirmar como BD o ruta: {unknown_libs}")

    # 4. Solo BD, lógica SQL pura → pushdown.
    if touches_db and not touches_files and not touches_work and not fuerza_pandas:
        detalle = (
            "entradas y salidas en BD (WORK = tabla temporal), lógica expresable en SQL"
            if sql_first
            else "entradas y salidas en BD, lógica PROC SQL pura"
        )
        return decision("sql_pushdown", detalle)

    # 5. BD mezclada con archivos/WORK o con lógica fila a fila → hybrid.
    if touches_db:
        reasons = []
        if touches_files:
            reasons.append("mezcla tablas BD con archivos")
        if touches_work:
            reasons.append("mezcla tablas BD con datasets WORK")
        if fuerza_pandas:
            if not sql_first:
                reasons.append("lógica DATA step no expresable en SQL puro")
            else:
                if parse.has_procedural_logic:
                    reasons.append(
                        "lógica DATA step procedural (orden/estado entre filas) "
                        "no expresable en SQL"
                    )
                if procs_de_orden:
                    reasons.append(
                        f"PROC dependiente del orden de filas: {', '.join(procs_de_orden)} "
                        "(una tabla SQL no tiene orden)"
                    )
        return decision("hybrid", *reasons)

    # 6. Sin BD: pandas en el servidor.
    if parse.inputs or parse.outputs or touches_files or fuerza_pandas:
        if sql_first and procs_de_orden:
            return decision(
                "pandas",
                f"PROC dependiente del orden de filas: {', '.join(procs_de_orden)} "
                "(una tabla SQL no tiene orden)",
            )
        return decision("pandas", "solo archivos y/o datasets WORK en memoria")

    # 7. Nodo sin referencias a datos (macros utilitarias, %let, etc.).
    if parse.macro_refs:
        return decision("ambiguous", f"referencias macro-dependientes: {parse.macro_refs[:5]}")
    return decision("utility", "sin referencias a datos; utilitario — traduce a Python plano")


def project_db_librefs(
    parses: dict[str, NodeParse],
    db_engines: frozenset[str] | set[str] | None = None,
) -> set[str]:
    """Librefs de BD a nivel proyecto: LIBNAME con engine BD en cualquier nodo."""
    engines = db_engines if db_engines is not None else DEFAULT_DB_ENGINES
    libs: set[str] = set()
    for parse in parses.values():
        libs |= {lr for lr, eng in parse.librefs_declared.items() if eng in engines}
    return libs
