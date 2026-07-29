"""Placement por BLOQUE, no por nodo.

``classify_placement`` emite un veredicto para todo el nodo, y eso arrastra
código que no le corresponde. El caso testigo (Síntesis_M_CR18, nodo
``S2_01_Inicio``) es así:

    PROC SQL; CREATE TABLE tablas.BD_CTSI AS
              SELECT * FROM '/sasdata/.../BD_CTSI_CIERRE.sas7bdat'; RUN;
    PROC SQL; DELETE FROM TABLAS.BD_CTSI WHERE AÑO=2002; QUIT;
    PROC SQL; UPDATE tablas.BD_CTSI SET C_CAGENTE='53' WHERE ...; QUIT;
    ... (decenas más de UPDATE/DELETE contra la misma tabla de BD)

El primer bloque es una INGESTA: hay que leer un archivo ``.sas7bdat`` y subirlo,
y eso solo se puede hacer con pandas — ningún servidor SQL sabe leer ese formato
(SAS podía porque él mismo era el motor). Los bloques siguientes son SQL puro
contra una tabla que vive en el server. Con un veredicto único, la evidencia de
archivo del primer bloque contamina a los demás: el nodo sale ``hybrid``, el
traductor baja la tabla completa a memoria y los ``UPDATE`` terminan como
``df.loc[mask, col] = valor``.

Este módulo corta el nodo en bloques ``PROC``/``DATA`` de primer nivel —las
mismas fronteras que usa ``llm.phases.split_sas_blocks``, buscadas sobre
``mask_noncode`` para no cortar dentro de un comentario o un string— y clasifica
cada uno por separado. La traducción resultante es una celda de ingesta con
pandas seguida de N celdas de SQL, en vez de un notebook entero en memoria.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sas_migrator.core.parser.placement import PlacementDecision, classify_placement
from sas_migrator.core.parser.statements import (
    DEFAULT_DB_ENGINES,
    NodeParse,
    parse_sas_code,
)
from sas_migrator.core.parser.tokenizer import mask_noncode, words

# Frontera de bloque: inicio de un PROC/DATA de primer nivel.
_BLOCK_START = re.compile(r"^[ \t]*(?:PROC|DATA)\b", re.IGNORECASE | re.MULTILINE)


@dataclass
class CodeSegment:
    """Un bloque del nodo con su propio parse y su propio placement."""

    index: int
    offset: int  # posición en el código del nodo (para trazar la celda)
    code: str
    kind: str  # "proc sql" | "data" | "proc sort" | ... | "preambulo"
    parse: NodeParse
    placement: str
    reasons: list[str] = field(default_factory=list)

    @property
    def corre_en_bd(self) -> bool:
        return self.placement in ("sql_passthrough", "sql_pushdown")


def split_code_segments(code: str) -> list[tuple[int, str]]:
    """Corta el código en ``(offset, texto)`` por bloque PROC/DATA.

    Lo que precede al primer bloque (LIBNAME, ``%let``, ``options``) es el
    segmento 0. Sin bloques, el nodo entero es un único segmento.
    """
    if not code.strip():
        return []
    cortes = [m.start() for m in _BLOCK_START.finditer(mask_noncode(code))]
    if not cortes:
        return [(0, code)]
    fronteras = [0, *cortes] if cortes[0] > 0 else cortes
    tramos = [
        (ini, code[ini:fin])
        for ini, fin in zip(fronteras, [*fronteras[1:], len(code)], strict=True)
        if code[ini:fin].strip()
    ]
    return tramos


def _kind(code: str) -> str:
    """Etiqueta del bloque: ``proc sql``, ``data``, ``preambulo``…"""
    toks = words(code)
    if not toks:
        return "vacio"
    head = toks[0].lower()
    if head == "proc":
        return f"proc {toks[1].lower()}" if len(toks) > 1 else "proc"
    if head == "data":
        return "data"
    return "preambulo"


def classify_segments(
    code: str,
    db_librefs: set[str] | None = None,
    db_engines: frozenset[str] | set[str] | None = None,
    *,
    sql_first: bool = False,
) -> list[CodeSegment]:
    """Placement bloque por bloque.

    Los LIBNAME viven en el preámbulo y no se repiten en cada bloque, así que
    los librefs declarados en CUALQUIER bloque del nodo se suman al set de BD
    que ve todo el nodo — si no, el primer ``PROC SQL`` vería su libref como
    desconocido y todo el nodo caería en ``ambiguous``.
    """
    tramos = split_code_segments(code)
    if not tramos:
        return []

    parses = [(offset, texto, parse_sas_code(texto)) for offset, texto in tramos]
    engines = db_engines if db_engines is not None else DEFAULT_DB_ENGINES
    db_libs = {lr.upper() for lr in (db_librefs or set())}
    for _, _, parse in parses:
        db_libs |= {
            lr.upper() for lr, eng in parse.librefs_declared.items() if eng in engines
        }

    segments: list[CodeSegment] = []
    for i, (offset, texto, parse) in enumerate(parses):
        decision: PlacementDecision = classify_placement(
            parse, db_libs, engines, sql_first=sql_first
        )
        segments.append(
            CodeSegment(
                index=i,
                offset=offset,
                code=texto,
                kind=_kind(texto),
                parse=parse,
                placement=decision.placement,
                reasons=decision.reasons,
            )
        )
    return segments


def node_placement_from_segments(segments: list[CodeSegment]) -> str:
    """Placement del nodo resumido desde sus bloques.

    Sirve para el índice y los reportes; la traducción usa los bloques. Un nodo
    cuyos bloques corren todos en la BD es pushdown; uno donde todos van a
    pandas es pandas; mezclado es hybrid — y ahí ``hybrid`` significa lo que
    debería significar: "este nodo tiene bloques de los dos tipos", no "algo
    tocó un archivo, mandá todo a memoria".
    """
    con_datos = [s for s in segments if s.placement != "utility"]
    if not con_datos:
        return "utility"
    if any(s.placement == "ambiguous" for s in con_datos):
        return "ambiguous"
    if all(s.corre_en_bd for s in con_datos):
        return "sql_passthrough" if all(
            s.placement == "sql_passthrough" for s in con_datos
        ) else "sql_pushdown"
    if any(s.corre_en_bd for s in con_datos):
        return "hybrid"
    return "hybrid" if any(s.placement == "hybrid" for s in con_datos) else "pandas"
