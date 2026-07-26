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

El default replica la localidad que SAS ya tenía; mover cómputo de lugar es una
mejora M-xxx aprobada, no una decisión silenciosa del traductor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sas_migrator.core.parser.statements import DEFAULT_DB_ENGINES, NodeParse

PLACEMENTS = ("sql_passthrough", "sql_pushdown", "pandas", "hybrid", "ambiguous")


@dataclass
class PlacementDecision:
    placement: str
    evidence: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _origin(libref: str, db_librefs: set[str]) -> str:
    """Origen de un dataset: db | work | lib_desconocida."""
    if libref == "WORK":
        return "work"
    if libref.upper() in db_librefs:
        return "db"
    return "unknown_lib"


def classify_placement(
    parse: NodeParse,
    db_librefs: set[str] | None = None,
    db_engines: frozenset[str] | set[str] | None = None,
) -> PlacementDecision:
    """Clasifica un nodo parseado.

    ``db_librefs``: librefs confirmados como BD a nivel proyecto (LIBNAME con
    engine de BD en cualquier nodo + confirmaciones de la entrevista B4b).
    Los LIBNAME del propio nodo se agregan automáticamente.

    ``db_engines``: set de engines de BD a reconocer (default: los de SAS/ACCESS;
    los proyectos con engines custom lo resuelven vía ``resolve_db_engines``).
    """
    engines = db_engines if db_engines is not None else DEFAULT_DB_ENGINES
    db_libs = {lr.upper() for lr in (db_librefs or set())}
    db_libs |= {lr for lr, eng in parse.librefs_declared.items() if eng in engines}

    in_origins = {_origin(r.libref, db_libs) for r in parse.inputs}
    out_origins = {_origin(r.libref, db_libs) for r in parse.outputs}
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
            {r.libref for r in [*parse.inputs, *parse.outputs] if _origin(r.libref, db_libs) == "unknown_lib"}
        )
        return decision("ambiguous", f"librefs sin confirmar como BD o ruta: {unknown_libs}")

    # 4. Solo BD, lógica SQL pura → pushdown.
    if touches_db and not touches_files and not touches_work and not parse.has_data_step_logic:
        return decision("sql_pushdown", "entradas y salidas en BD, lógica PROC SQL pura")

    # 5. BD mezclada con archivos/WORK o con lógica fila a fila → hybrid.
    if touches_db:
        reasons = []
        if touches_files:
            reasons.append("mezcla tablas BD con archivos")
        if touches_work:
            reasons.append("mezcla tablas BD con datasets WORK")
        if parse.has_data_step_logic:
            reasons.append("lógica DATA step no expresable en SQL puro")
        return decision("hybrid", *reasons)

    # 6. Sin BD: pandas en el servidor.
    if parse.inputs or parse.outputs or touches_files or parse.has_data_step_logic:
        return decision("pandas", "solo archivos y/o datasets WORK en memoria")

    # 7. Nodo sin referencias a datos (macros utilitarias, %let, etc.).
    if parse.macro_refs:
        return decision("ambiguous", f"referencias macro-dependientes: {parse.macro_refs[:5]}")
    return decision("pandas", "sin referencias a datos; utilitario — traduce a Python plano")


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
