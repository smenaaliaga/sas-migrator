from sas_migrator.core.extractors.egp import _classify_code, _extract_datasets
from sas_migrator.core.models.graph import NodeType


def test_classify_proc_sql_and_data_step() -> None:
    assert _classify_code("proc sql; select * from work.a; quit;") == NodeType.PROC_SQL
    assert _classify_code("data work.b; set work.a; run;") == NodeType.DATA_STEP


def test_extract_datasets_from_common_sas_code() -> None:
    code = """
    libname src odbc dsn=demo;
    data work.out;
      set src.in_table;
    run;
    proc sql;
      create table work.joined as
      select * from work.out;
    quit;
    """

    inputs, outputs, libraries = _extract_datasets(code)

    assert "src.in_table" in inputs
    assert "work.out" in inputs
    assert "work.out" in outputs
    assert "src" in libraries