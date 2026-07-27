"""Builder de .egp sintético — compartido por tests y golden runs de CLI.

El generador replica el formato mínimo que parsea ``sas_migrator.extractors.egp``:
``project.xml`` UTF-16 con sección ``<Elements>`` (wrappers con ``<Element>``
interno: ID/Type/Label/Container/InputIDs) y wrappers ``<PFD>`` con
``<Process>/<Dependencies>``. Si el formato de un .egp real difiere, ajustar
solo este archivo.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# CodeTask-1: multi-construct a propósito (DATA step + PROC SQL en el mismo nodo).
CODE_TASK_1_SAS = """
data work.ventas;
  set src.ventas_raw;
  monto_ajustado = monto * factor;
run;

proc sql;
  create table work.ventas_agg as
  select region, sum(monto_ajustado) as total
  from work.ventas
  group by region;
quit;
"""

CODE_TASK_2_SAS = """
proc sort data=work.ventas_agg out=work.ventas_ord;
  by descending total;
run;
"""

QUERY_1_SAS = """
proc sql;
  create table work.consulta as
  select * from src.clientes;
quit;
"""

# Log de ejecución de un Query Builder tal como lo escribe EG: prefijo de tipo
# ('s' fuente, 'n' nota, 't' título), número de línea y el texto alineado a una
# columna fija. Es la única fuente del SQL cuando la tarea no dejó payload, y
# trae el preámbulo de EG (ODS/%LET) que NO es del nodo.
QUERY_1_LOG = "\r\n".join([
    "t11                          The SAS System                    10:38 Tuesday",
    "t ",
    "s 1          ;*';*\";*/;quit;run;",
    "s 2          %LET _CLIENTTASKLABEL='Query Builder';",
    "s 3          ODS _ALL_ CLOSE;",
    "n NOTE: Writing TAGSETS.SASREPORT13(EGSR) Body file: EGSR",
    "s 4          PROC SQL;",
    "s 5             CREATE TABLE WORK.QUERY_FOR_CLIENTES AS",
    "s 6             SELECT DISTINCT t1.REGION",
    "s 7                FROM SRC.CLIENTES t1;",
    "n NOTE: Table WORK.QUERY_FOR_CLIENTES created, with 12 rows and 1 columns.",
    "s 8          QUIT;",
    "s 9          ODS _ALL_ CLOSE;",
])

# CodeTask-3 (opcional, flag db_write_task): escribe a una tabla de BD real —
# habilita el e2e contra una BD de prueba (verify + ejecución + cascada).
CODE_TASK_3_SAS = """
libname gob odbc dsn=dwh;

proc sql;
  create table gob.resumen_regional as
  select region, sum(monto_ajustado) as monto
  from work.ventas
  group by region;
quit;
"""


def _element(el_id: str, el_type: str, label: str, container: str = "") -> str:
    return (
        "<ElementWrapper><Element>"
        f"<ID>{el_id}</ID>"
        f"<Type>{el_type}</Type>"
        f"<Label>{label}</Label>"
        f"<Container>{container}</Container>"
        "<InputIDs></InputIDs>"
        "</Element></ElementWrapper>"
    )


def _process(el_id: str, deps: list[str]) -> str:
    deps_xml = "".join(f"<Dep>{d}</Dep>" for d in deps)
    return (
        "<Process>"
        f"<Element><ID>{el_id}</ID></Element>"
        f"<Dependencies>{deps_xml}</Dependencies>"
        "</Process>"
    )


def build_egp(
    dest: Path,
    *,
    include_query_payload: bool = True,
    orphan_code_entry: bool = False,
    codetask_without_file: bool = False,
    unknown_dependency: bool = False,
    db_write_task: bool = False,
    query_log_only: bool = False,
) -> Path:
    """Construye un .egp sintético (ZIP + project.xml UTF-16) y devuelve su ruta.

    Flags de inyección de fallas:
    - ``orphan_code_entry``: agrega ``CodeTask-99/code.sas`` sin elemento en
      ``<Elements>`` (código SAS que el extractor debe reportar como residuo).
    - ``codetask_without_file``: declara ``CodeTask-3`` sin ``code.sas`` en el ZIP.
    - ``unknown_dependency``: agrega una dependencia hacia ``GhostTask-1``, un ID
      que no existe en ``<Elements>``.
    - ``query_log_only``: ``Query-1`` sin payload pero con log de ejecución —
      el caso real de un Query Builder armado en la GUI.
    """
    elements = [
        _element("PFD-1", "CONTAINER", "Flujo Principal"),
        _element("CodeTask-1", "TASK", "Carga datos", "PFD-1"),
        _element("CodeTask-2", "TASK", "Ordena datos", "PFD-1"),
        _element("Query-1", "TASK", "Consulta clientes", "PFD-1"),
        _element("ImportTask-1", "TASK", "Importar Excel", "PFD-1"),
    ]
    if codetask_without_file:
        elements.append(_element("CodeTask-3", "TASK", "Sin codigo", "PFD-1"))
    if db_write_task:
        elements.append(_element("CodeTask-9", "TASK", "Carga a BD", "PFD-1"))

    codetask2_deps = ["CodeTask-1"]
    if unknown_dependency:
        codetask2_deps.append("GhostTask-1")

    processes = [
        _process("CodeTask-2", codetask2_deps),
        _process("Query-1", ["CodeTask-2"]),
        _process("ImportTask-1", ["Query-1"]),
    ]
    if db_write_task:
        processes.append(_process("CodeTask-9", ["CodeTask-1"]))

    xml_text = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<Project EGVersion="8.1">'
        "<Element><Label>Proyecto Demo</Label></Element>"
        "<Elements>"
        + "".join(elements)
        + "<ElementWrapper><PFD>"
        + "".join(processes)
        + "</PFD></ElementWrapper>"
        "</Elements>"
        "</Project>"
    )

    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("project.xml", xml_text.encode("utf-16"))
        zf.writestr("CodeTask-1/code.sas", CODE_TASK_1_SAS)
        zf.writestr("CodeTask-2/code.sas", CODE_TASK_2_SAS)
        if query_log_only:
            zf.writestr(
                "Query-1/Log-abc/result.log", QUERY_1_LOG.encode("utf-8-sig")
            )
        elif include_query_payload:
            zf.writestr("Query-1/code.sas", QUERY_1_SAS)
        if db_write_task:
            zf.writestr("CodeTask-9/code.sas", CODE_TASK_3_SAS)
        if orphan_code_entry:
            zf.writestr("CodeTask-99/code.sas", "data work.huerfano; x=1; run;")
    return dest


