"""Tests del parser SAS v2 — tokenizador, parsers dirigidos y placement.

Cada caso del tokenizador/statements corresponde a un modo de falla VERIFICADO
del parsing por regex del v1 (documentado en el diagnóstico de la migración).
"""

from __future__ import annotations

from sas_migrator.core.parser.placement import classify_placement, project_db_librefs
from sas_migrator.core.parser.statements import (
    DEFAULT_DB_ENGINES,
    parse_sas_code,
    resolve_db_engines,
)
from sas_migrator.core.parser.tokenizer import code_statements, split_statements


def _io(parse):
    ins = {f"{r.libref}.{r.table}" for r in parse.inputs}
    outs = {f"{r.libref}.{r.table}" for r in parse.outputs}
    return ins, outs


# ── Tokenizador ──────────────────────────────────────────────────────────────

def test_star_comment_does_not_eat_multiplication() -> None:
    code = "data work.a; monto = a * b; run; * esto si es comentario;"
    stmts = code_statements(code)
    assert "monto = a * b" in stmts
    assert not any("esto si es comentario" in s for s in stmts)


def test_block_comment_removed_but_strings_preserved() -> None:
    code = "data w.a; x = '/* no soy comentario */'; /* si soy */ y = 1; run;"
    stmts = code_statements(code)
    assert any("/* no soy comentario */" in s for s in stmts)
    assert not any("si soy" in s for s in stmts)


def test_semicolon_inside_string_does_not_split() -> None:
    stmts = split_statements("x = 'a;b'; y = 2")
    assert stmts == ["x = 'a;b'", "y = 2"]


# ── DATA step ────────────────────────────────────────────────────────────────

def test_data_statement_captures_all_outputs() -> None:
    parse = parse_sas_code("data work.a work.b; set work.x(keep=id) work.y; run;")
    ins, outs = _io(parse)
    assert outs == {"WORK.A", "WORK.B"}, "v1 solo capturaba el primero"
    assert ins == {"WORK.X", "WORK.Y"}, "v1 solo capturaba el primero del SET"


def test_data_null_is_not_an_output() -> None:
    parse = parse_sas_code("data _null_; set work.x; put x; run;")
    _, outs = _io(parse)
    assert outs == set()


def test_merge_captures_all_inputs() -> None:
    parse = parse_sas_code("data w.m; merge w.a(in=ina) w.b(in=inb); by id; run;")
    ins, _ = _io(parse)
    assert ins == {"W.A", "W.B"}


# ── PROC SQL ─────────────────────────────────────────────────────────────────

def test_create_table_is_an_output() -> None:
    parse = parse_sas_code(
        "proc sql; create table gob.ventas_agg as select * from gob.ventas; quit;"
    )
    ins, outs = _io(parse)
    assert outs == {"GOB.VENTAS_AGG"}, "v1 no veía CREATE TABLE — el construct más común de EG"
    assert ins == {"GOB.VENTAS"}


def test_joins_are_inputs() -> None:
    parse = parse_sas_code(
        "proc sql; create table w.r as select * from gob.ventas a "
        "inner join gob.cli b on a.id=b.id left join w.extra c on a.id=c.id; quit;"
    )
    ins, _ = _io(parse)
    assert ins == {"GOB.VENTAS", "GOB.CLI", "W.EXTRA"}, "v1 solo capturaba FROM"


def test_insert_delete_update_are_writes() -> None:
    parse = parse_sas_code(
        "proc sql; delete from gob.destino where anio=2026; "
        "insert into gob.destino select * from work.stage; quit;"
    )
    ins, outs = _io(parse)
    assert "GOB.DESTINO" in outs
    assert "WORK.STAGE" in ins


def test_proc_sort_data_and_out() -> None:
    parse = parse_sas_code("proc sort data=work.ventas(keep=id monto) out=work.ordenado; by id; run;")
    ins, outs = _io(parse)
    assert ins == {"WORK.VENTAS"}
    assert outs == {"WORK.ORDENADO"}


def test_one_letter_libref_survives() -> None:
    parse = parse_sas_code("libname g odbc noprompt='...'; data g.t; set work.x; run;")
    assert "G" in parse.librefs_declared, "v1 exigía librefs de ≥2 caracteres"
    _, outs = _io(parse)
    assert "G.T" in outs


def test_macro_dependent_ref_is_flagged_not_lost() -> None:
    parse = parse_sas_code("data &lib..tabla; set work.x; run;")
    assert any("&lib" in m for m in parse.macro_refs), "v1 perdía &lib..tabla en silencio"


def test_connect_to_marks_passthrough() -> None:
    parse = parse_sas_code(
        "proc sql; connect to odbc as plat (datasrc='X'); "
        "create table w.r as select * from connection to plat (select * from dbo.t); "
        "quit;"
    )
    assert parse.has_passthrough
    assert "PLAT" in parse.passthrough_aliases


# ── Placement ────────────────────────────────────────────────────────────────

def test_placement_pushdown_when_all_db_and_pure_sql() -> None:
    parse = parse_sas_code(
        "libname gob odbc; proc sql; create table gob.agg as "
        "select region, sum(m) from gob.ventas group by region; quit;"
    )
    d = classify_placement(parse)
    assert d.placement == "sql_pushdown"


def test_placement_passthrough() -> None:
    parse = parse_sas_code(
        "proc sql; connect to odbc as p; execute (delete from dbo.t) by p; quit;"
    )
    assert classify_placement(parse).placement == "sql_passthrough"


def test_placement_pandas_when_only_files_and_work() -> None:
    parse = parse_sas_code(
        "proc import datafile='ventas.xlsx' out=work.v dbms=xlsx; run; "
        "data work.limpio; set work.v; if monto > 0; run;"
    )
    assert classify_placement(parse).placement == "pandas"


def test_placement_hybrid_when_db_mixed_with_work() -> None:
    parse = parse_sas_code(
        "libname gob odbc; proc sql; create table work.stage as "
        "select * from gob.ventas; quit; "
        "data gob.destino; set work.stage; nuevo = monto * 2; run;"
    )
    assert classify_placement(parse).placement == "hybrid"


def test_placement_ambiguous_when_unknown_libref() -> None:
    parse = parse_sas_code("data misterio.t; set work.x; run;")
    d = classify_placement(parse)
    assert d.placement == "ambiguous"
    assert any("MISTERIO" in r for r in d.reasons)


def test_placement_uses_project_level_db_librefs() -> None:
    # El LIBNAME vive en otro nodo; el clasificador recibe el set del proyecto.
    node_a = parse_sas_code("libname gob odbc;")
    node_b = parse_sas_code(
        "proc sql; create table gob.r as select * from gob.v; quit;"
    )
    db_libs = project_db_librefs({"a": node_a, "b": node_b})
    assert classify_placement(node_b, db_libs).placement == "sql_pushdown"


def test_placement_default_replicates_sas_locality() -> None:
    """Regla acordada: BD con lógica DATA step = hybrid, nunca pushdown forzado."""
    parse = parse_sas_code(
        "libname gob odbc; data gob.out; set gob.in; retain acumulado; "
        "acumulado + monto; run;"
    )
    assert classify_placement(parse).placement == "hybrid"


# ── Engines de BD (set default + config del proyecto) ────────────────────────

def test_default_db_engines_cover_common_sas_access_engines() -> None:
    # El migrador es genérico: cualquier engine SAS/ACCESS habitual debe
    # clasificar como BD sin configuración extra.
    for engine in ("ODBC", "ORACLE", "TERADATA", "DB2", "SNOW", "REDSHIFT",
                   "SAPHANA", "MYSQL", "BIGQUERY", "SQLSVR", "SQLSRV"):
        assert engine in DEFAULT_DB_ENGINES, engine


def test_expanded_engine_classifies_as_db_without_config() -> None:
    parse = parse_sas_code(
        "libname dwh teradata; proc sql; create table dwh.agg as "
        "select * from dwh.base; quit;"
    )
    assert classify_placement(parse).placement == "sql_pushdown"
    parse2 = parse_sas_code(
        "libname sf snow; proc sql; create table sf.agg as select * from sf.base; quit;"
    )
    assert classify_placement(parse2).placement == "sql_pushdown"


def test_resolve_db_engines_reads_project_config(tmp_path) -> None:
    (tmp_path / "project_config.yaml").write_text(
        "parser:\n  extra_db_engines: [miengine]\n  ignore_db_engines: [ODBC]\n",
        encoding="utf-8",
    )
    engines = resolve_db_engines(tmp_path)
    assert "MIENGINE" in engines
    assert "ODBC" not in engines
    assert "ORACLE" in engines  # el resto del default sobrevive


def test_resolve_db_engines_without_config_is_default(tmp_path) -> None:
    assert resolve_db_engines(tmp_path) == DEFAULT_DB_ENGINES
    assert resolve_db_engines(None) == DEFAULT_DB_ENGINES


def test_custom_engine_via_config_reaches_placement(tmp_path) -> None:
    engines = resolve_db_engines(None) | {"MIENGINE"}
    parse = parse_sas_code(
        "libname cli miengine server=x; proc sql; "
        "create table cli.r as select * from cli.v; quit;"
    )
    # sin el engine custom: libref sin confirmar → ambiguous (va a B4b)
    assert classify_placement(parse).placement == "ambiguous"
    # con el set resuelto por config: BD confirmada → pushdown
    assert classify_placement(parse, db_engines=engines).placement == "sql_pushdown"
    assert project_db_librefs({"n": parse}, engines) == {"CLI"}
