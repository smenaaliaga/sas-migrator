from sas_migrator.core.extractors.egp import (
    _classify_code,
    _extract_datasets,
)
from sas_migrator.core.models.graph import NodeType

SAMPLE = """
libname src odbc dsn=demo;
data work.out;
  set src.in_table;
run;
proc sql;
  create table work.joined as
  select * from work.out;
quit;
"""


def test_classify_proc_sql_and_data_step() -> None:
    assert _classify_code("proc sql; select * from work.a; quit;") == NodeType.PROC_SQL
    assert _classify_code("data work.b; set work.a; run;") == NodeType.DATA_STEP


def test_extract_datasets_uses_parser_v2_qualified_view() -> None:
    inputs, outputs, libraries = _extract_datasets(SAMPLE)

    assert inputs == ["SRC.IN_TABLE", "WORK.OUT"]
    # CREATE TABLE era invisible para el regex v1; el parser v2 lo recupera.
    assert outputs == ["WORK.JOINED", "WORK.OUT"]
    assert libraries == ["SRC"]
