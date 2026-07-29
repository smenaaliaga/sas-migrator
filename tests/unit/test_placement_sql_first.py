"""Placement sql-first: SQL en la BD por default, y por BLOQUE en vez de por nodo.

El caso que motivó estos tests es Síntesis_M_CR18 / ``S2_01_Inicio``: un nodo que
ingesta un ``.sas7bdat`` en su primer PROC SQL y después le hace decenas de
``UPDATE``/``DELETE`` a una tabla de SQL Server. Con un veredicto único por nodo,
la evidencia de archivo del primer bloque contaminaba a los demás: el nodo salía
``hybrid``, el traductor bajaba la tabla completa a memoria y los ``UPDATE``
terminaban como ``df.loc[mask, col] = valor`` (95 veces en el notebook real).
"""

from __future__ import annotations

from sas_migrator.core.parser.placement import classify_placement
from sas_migrator.core.parser.segments import (
    classify_segments,
    node_placement_from_segments,
    split_code_segments,
)
from sas_migrator.core.parser.statements import parse_sas_code

# ── Lógica de DATA step: procedural vs. expresable en SQL ────────────────────

def test_asignacion_e_if_no_son_procedurales() -> None:
    """`x = y * 2` es una columna calculada y `if x > 0;` es un WHERE. Marcarlos
    como "lógica fila a fila" empujaba a pandas cualquier nodo con un DATA step."""
    parse = parse_sas_code(
        "data work.limpio; set work.crudo; neto = monto * 1.19; if neto > 0; run;"
    )
    assert parse.has_data_step_logic  # sigue habiendo lógica
    assert parse.has_sql_expressible_logic
    assert not parse.has_procedural_logic


def test_retain_lag_first_last_y_do_si_son_procedurales() -> None:
    for codigo in (
        "data work.a; set work.b; retain acum 0; acum = acum + monto; run;",
        "data work.a; set work.b; anterior = lag(monto); run;",
        "data work.a; set work.b; by cliente; if first.cliente then n = 0; run;",
        "data work.a; set work.b; do i = 1 to 10; total = total + i; end; run;",
        "data work.a; set work.b; if monto > 0 then output; run;",
        "data work.a; set work.b; acumulado + monto; run;",
    ):
        parse = parse_sas_code(codigo)
        assert parse.has_procedural_logic, codigo
        assert parse.procedural_evidence, codigo


def test_sql_first_manda_a_la_bd_lo_que_mezcla_work_con_tablas() -> None:
    """WORK es un schema temporal del server, no memoria del proceso."""
    codigo = (
        "libname gob odbc; proc sql; create table work.stage as "
        "select * from gob.ventas; quit; "
        "data gob.destino; set work.stage; nuevo = monto * 2; run;"
    )
    parse = parse_sas_code(codigo)
    assert classify_placement(parse).placement == "hybrid"  # histórico
    assert classify_placement(parse, sql_first=True).placement == "sql_pushdown"


def test_sql_first_respeta_lo_procedural() -> None:
    parse = parse_sas_code(
        "libname gob odbc; data gob.out; set gob.in; retain acumulado; "
        "acumulado + monto; run;"
    )
    d = classify_placement(parse, sql_first=True)
    assert d.placement == "hybrid"
    assert any("procedural" in r for r in d.reasons)


def test_work_solo_es_tabla_temporal_si_el_proyecto_tiene_bd() -> None:
    """Sin ningún libref de BD confirmado no hay server donde poner una tabla
    temp: pedirle una conexión a un flujo que solo usa WORK es inventar infra."""
    parse = parse_sas_code("data work.b; set work.a; neto = monto * 2; run;")
    d = classify_placement(parse, sql_first=True)
    assert d.placement == "pandas"
    assert d.evidence["work_as_temp_table"] is False


def test_proc_que_dependen_del_orden_se_quedan_en_pandas() -> None:
    """Una tabla SQL no tiene orden: un PROC SORT materializado como ORDER BY no
    garantiza que el consumidor lea en ese orden, y el DATA step siguiente
    procesa BY-grupos confiando en eso."""
    parse = parse_sas_code(
        "libname gob odbc; proc sort data=gob.ventas out=gob.ordenado; by total; run;"
    )
    d = classify_placement(parse, sql_first=True)
    assert d.placement == "hybrid"
    assert any("orden de filas" in r for r in d.reasons)
    assert d.evidence["order_dependent_procs"] == ["sort"]


# ── Statements que el parser no veía ────────────────────────────────────────

def test_dataset_sas_referenciado_por_ruta_citada() -> None:
    """`FROM '/x/base.sas7bdat'`: words() blanquea los strings, así que la
    dependencia de archivo no llegaba a file_reads ni a lineage."""
    parse = parse_sas_code(
        "libname gob odbc; proc sql; create table gob.t as "
        "select * from '/sasdata/pre/BD_CIERRE.sas7bdat'; quit;"
    )
    assert parse.dataset_files == ["/sasdata/pre/BD_CIERRE.sas7bdat"]
    assert "/sasdata/pre/BD_CIERRE.sas7bdat" in parse.file_reads
    # Es una INGESTA: hay que leer el archivo con pandas y subirlo, porque
    # ningún servidor SQL sabe leer .sas7bdat.
    d = classify_placement(parse, sql_first=True)
    assert d.placement == "hybrid"
    assert any("archivos" in r for r in d.reasons)


def test_proc_datasets_append_declara_origen_y_destino() -> None:
    """El APPEND va en un statement propio, no en el del PROC: ni la tabla
    destino (`base=`) ni la fuente (`data=`) llegaban a lineage."""
    parse = parse_sas_code(
        "libname gob odbc; proc datasets; "
        "append base=gob.acumulada data=work.nuevo force; run;"
    )
    assert {f"{r.libref}.{r.table}" for r in parse.outputs} == {"GOB.ACUMULADA"}
    assert {f"{r.libref}.{r.table}" for r in parse.inputs} == {"WORK.NUEVO"}


def test_alter_table_es_una_escritura_a_la_tabla() -> None:
    """Sin registrarlo, `PROC SQL; alter table gob.t add PROC CHAR; QUIT;` no
    declaraba ninguna referencia a datos y el nodo salía `utility` — de ahí que
    la traducción agregara la columna al DataFrame en vez de a la tabla."""
    parse = parse_sas_code(
        "libname gob odbc; proc sql; alter table gob.t add PROC CHAR; quit;"
    )
    assert {f"{r.libref}.{r.table}" for r in parse.outputs} == {"GOB.T"}
    assert classify_placement(parse, sql_first=True).placement == "sql_pushdown"


# ── Placement por bloque ────────────────────────────────────────────────────

def test_split_code_segments_corta_en_fronteras_proc_y_data() -> None:
    code = (
        "libname gob odbc;\n"
        "proc sql; create table gob.a as select * from gob.b; quit;\n"
        "data gob.c; set gob.a; run;\n"
    )
    tramos = split_code_segments(code)
    assert [t[1].split()[0].lower() for t in tramos] == ["libname", "proc", "data"]


def test_split_no_corta_dentro_de_comentarios_ni_strings() -> None:
    code = (
        "proc sql;\n"
        "/* proc sort viejo, comentado */\n"
        "create table gob.a as select * from gob.b where t = 'PROC X';\n"
        "quit;\n"
    )
    assert len(split_code_segments(code)) == 1


def test_una_ingesta_de_archivo_no_arrastra_los_update_a_pandas() -> None:
    """El caso S2_01_Inicio, reducido: bloque 1 lee un .sas7bdat (pandas
    inevitable), bloques 2 y 3 son SQL puro contra una tabla del server."""
    code = (
        "libname tablas odbc;\n"
        "proc sql;\n"
        "create table tablas.bd as select * from '/sasdata/pre/BD_CIERRE.sas7bdat';\n"
        "quit;\n"
        "proc sql; delete from tablas.bd where anio = 2002; quit;\n"
        "proc sql; update tablas.bd set cagente = '53' where cagente in ('7','7.1');\n"
        "quit;\n"
    )
    segs = classify_segments(code, sql_first=True)
    por_placement = [s.placement for s in segs if s.kind != "preambulo"]
    assert por_placement == ["hybrid", "sql_pushdown", "sql_pushdown"]
    assert any("archivos" in r for s in segs for r in s.reasons)
    # El nodo se resume como hybrid — pero ahora eso significa "tiene bloques de
    # los dos tipos", no "algo tocó un archivo, mandá todo a memoria".
    assert node_placement_from_segments(segs) == "hybrid"


def test_libname_del_preambulo_vale_para_todos_los_bloques() -> None:
    """Los LIBNAME viven en el preámbulo y no se repiten: sin propagarlos, el
    primer PROC SQL vería su libref como desconocido y el nodo caería en
    ``ambiguous``."""
    code = (
        "libname gob odbc;\n"
        "proc sql; create table gob.a as select * from gob.b; quit;\n"
    )
    segs = classify_segments(code, sql_first=True)
    assert [s.placement for s in segs] == ["utility", "sql_pushdown"]


def test_nodo_enteramente_sql_se_resume_como_pushdown() -> None:
    code = (
        "libname gob odbc;\n"
        "proc sql; delete from gob.t where anio = 2002; quit;\n"
        "proc sql; update gob.t set x = 1 where y = 2; quit;\n"
    )
    assert node_placement_from_segments(classify_segments(code, sql_first=True)) == (
        "sql_pushdown"
    )
