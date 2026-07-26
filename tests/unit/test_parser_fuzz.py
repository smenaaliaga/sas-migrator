"""Fuzzing ligero del parser SAS (hardening Etapa 6): código malformado,
truncado o directamente basura NUNCA debe crashear parse_sas_code — un .egp
real trae de todo y el pipeline debe degradar con gracia (macro_refs,
residuo), no morir."""

from __future__ import annotations

import pytest

from sas_migrator.core.parser.placement import classify_placement
from sas_migrator.core.parser.statements import parse_sas_code

CASES = [
    "",
    "   \n\t  ",
    ";" * 500,
    "proc sql",  # sin ; ni quit
    "proc sql;;;; select from where; quit;",
    "data ; set ; run;",
    "data a.b.c.d; set x..y; run;",  # niveles de calificación inválidos
    "%macro sin_fin(",
    "&&&&macro..&otra...tabla",
    "libname;",
    "libname x 'sin engine",  # comilla sin cerrar
    "/* comentario sin cerrar\ndata w; set z; run;",
    "data w; set z(where=(a=1) keep=a b; run;",  # paréntesis sin cerrar
    "select * from t1 union select * from",  # SQL truncado
    "PROC SQL; CREATE TABLE a AS SELECT * FROM connection to oracle (select); QUIT;",
    "﻿\x00data w; set \x01raro; run;",  # BOM + bytes de control
    "ódatá wórk.tabla; set ñandú.año; run;",  # no-ASCII
    "run; quit; run; data; proc; %mend;",  # orden imposible
    "X" * 20000,  # línea gigante sin estructura
    "data w; set z; run;\n" * 2000,  # muchos statements
]


@pytest.mark.parametrize("code", CASES, ids=range(len(CASES)))
def test_parse_never_crashes(code: str) -> None:
    parse = parse_sas_code(code)
    # El contrato mínimo: estructuras bien tipadas, aunque estén vacías
    assert isinstance(parse.librefs_declared, dict) or parse.librefs_declared is not None
    for ref in list(parse.inputs) + list(parse.outputs):
        assert ref.libref and ref.table


@pytest.mark.parametrize("code", CASES, ids=range(len(CASES)))
def test_placement_never_crashes(code: str) -> None:
    parse = parse_sas_code(code)
    decision = classify_placement(parse, db_librefs=set(), db_engines=frozenset({"ODBC"}))
    assert decision.placement in {
        "sql_passthrough", "sql_pushdown", "pandas", "hybrid", "ambiguous", "utility",
    }
