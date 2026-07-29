"""Ensamblador determinista: template, cell_index por construcción y
chequeos estáticos que marcan al nodo dudoso en vez de descartarlo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sas_migrator.core.assembly.notebook import (
    assemble_notebooks,
    check_node_translation,
)
from sas_migrator.core.models.translation import NodeTranslation


def _nt(node_id: str, cells: list[str], **kwargs) -> NodeTranslation:
    return NodeTranslation(node_id=node_id, node_label=f"Nodo {node_id}",
                           cells=cells, **kwargs)


def _plan(*node_ids: str, notebook: str = "output/NB-01_demo.ipynb") -> dict:
    return {
        "targets": [
            {"node_id": nid, "node_label": f"Nodo {nid}", "strategy": "pandas",
             "notebook_path": notebook}
            for nid in node_ids
        ]
    }


def _read_nb(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Template y cell_index ───────────────────────────────────────────────────

def test_template_anchors_config_and_cell_index(tmp_path: Path) -> None:
    translations = {
        "A": _nt("A", ["x = pd.DataFrame()\n"], imports=["import json"]),
        "B": _nt("B", ["y = np.array([1])\n", "z = y.sum()\n"],
                 imports=["import json", "from pathlib import Path"]),
    }
    mapping, failures = assemble_notebooks(_plan("A", "B"), translations, tmp_path / "output")

    assert failures == []
    nb = _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")
    sources = ["".join(c["source"]) for c in nb["cells"]]

    assert sources[0].startswith("# NB-01_demo")
    # celda de configuración con imports agregados y dedupeados
    assert "# ========= Celda 1: Configuración =========" in sources[1]
    assert sources[1].count("import json") == 1
    assert "from pathlib import Path" in sources[1]
    # anclas del audit (el `## label` abre la celda; debajo va la confianza)
    assert "## Nodo A\n\n*confianza: low*" in sources
    assert "## Nodo B\n\n*confianza: low*" in sources
    # ids fijos por posición (determinismo)
    assert [c["id"] for c in nb["cells"]] == [f"cell-{i:03d}" for i in range(len(nb["cells"]))]

    by_id = {m.node_id: m for m in mapping.mappings}
    # cell_index apunta a la PRIMERA celda code del nodo (calculado, no a mano)
    a = by_id["A"]
    assert nb["cells"][a.cell_index]["cell_type"] == "code"
    assert "# ========= Nodo A =========" in "".join(nb["cells"][a.cell_index]["source"])
    b = by_id["B"]
    assert b.cell_count == 2
    assert nb["cells"][b.cell_index + 1]["cell_type"] == "code"
    assert by_id["A"].notebook_path == "output/NB-01_demo.ipynb"


def test_titulo_identifica_el_process_flow_de_origen(tmp_path: Path) -> None:
    """El nombre NB-NN es posicional; el pfd_id es el ancla estable."""
    plan = _plan("A", "B")
    for t in plan["targets"]:
        t["pfd_id"] = "ProcessFlowContainer-2o1NZYl9FKnoVXKH"
        t["pfd_label"] = "Análisis WTW"
    assemble_notebooks(plan, {"A": _nt("A", ["x = 1\n"]), "B": _nt("B", ["y = 2\n"])},
                       tmp_path / "output")

    titulo = "".join(_read_nb(tmp_path / "output" / "NB-01_demo.ipynb")["cells"][0]["source"])
    assert titulo.startswith("# NB-01_demo")
    # el flujo compartido por los dos nodos aparece UNA vez, con label e id
    assert titulo.count("ProcessFlowContainer-2o1NZYl9FKnoVXKH") == 1
    assert "Análisis WTW" in titulo


def test_titulo_lista_todos_los_flujos_en_estrategia_single(tmp_path: Path) -> None:
    plan = _plan("A", "B", notebook="output/flow.ipynb")
    plan["targets"][0].update(pfd_id="PFD-uno", pfd_label="Cargadores")
    plan["targets"][1].update(pfd_id="PFD-dos", pfd_label="Salidas")
    assemble_notebooks(plan, {"A": _nt("A", ["x = 1\n"]), "B": _nt("B", ["y = 2\n"])},
                       tmp_path / "output")

    titulo = "".join(_read_nb(tmp_path / "output" / "flow.ipynb")["cells"][0]["source"])
    assert "PFD-uno" in titulo and "PFD-dos" in titulo


def test_plan_sin_pfd_id_conserva_el_titulo_pelado(tmp_path: Path) -> None:
    """Planes viejos (o el stub) no tienen el campo: no se inventa un origen."""
    assemble_notebooks(_plan("A"), {"A": _nt("A", ["x = 1\n"])}, tmp_path / "output")
    titulo = "".join(_read_nb(tmp_path / "output" / "NB-01_demo.ipynb")["cells"][0]["source"])
    assert titulo == "# NB-01_demo"


def test_bare_notebook_name_becomes_canonical_output_path(tmp_path: Path) -> None:
    plan = _plan("A", notebook="NB-02_x.ipynb")
    mapping, _ = assemble_notebooks(plan, {"A": _nt("A", ["x = 1\n"])}, tmp_path / "output")
    assert mapping.mappings[0].notebook_path == "output/NB-02_x.ipynb"
    assert (tmp_path / "output" / "NB-02_x.ipynb").exists()


def test_missing_translation_is_skipped_without_failure(tmp_path: Path) -> None:
    mapping, failures = assemble_notebooks(
        _plan("A", "B"), {"A": _nt("A", ["x = 1\n"])}, tmp_path / "output"
    )
    # B sin traducción: needs_human ya registrado aguas arriba; aquí solo se omite
    assert failures == []
    assert [m.node_id for m in mapping.mappings] == ["A"]


# ── Chequeos estáticos ──────────────────────────────────────────────────────

def test_syntax_error_deja_hueco_explicito_no_silencio(tmp_path: Path) -> None:
    """No parsea: es uno de los dos kinds que NO se pueden emitir tal cual.

    Aun así el nodo existe en el notebook — banner, código original en markdown
    y una celda que levanta NotImplementedError. Antes desaparecía sin dejar
    rastro, que es la falla que este comportamiento revierte.
    """
    translations = {"A": _nt("A", ["def broken(:\n"]), "B": _nt("B", ["ok = 1\n"])}
    mapping, failures = assemble_notebooks(_plan("A", "B"), translations, tmp_path / "output")

    assert [f.node_id for f in failures] == ["A"]
    assert failures[0].reason == "syntax_error"
    assert failures[0].emitted is False
    assert [m.node_id for m in mapping.mappings] == ["A", "B"]

    by_id = {m.node_id: m for m in mapping.mappings}
    assert by_id["A"].degraded and by_id["A"].degraded_reason.startswith("syntax_error:")
    assert by_id["B"].degraded is False

    nb = _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")
    sources = ["".join(c["source"]) for c in nb["cells"]]
    banner = next(s for s in sources if s.startswith("## Nodo A"))
    assert "Nodo degradado" in banner and "`syntax_error`" in banner
    assert "def broken(:" in banner, "el código original se conserva, en markdown"
    hueco = "".join(nb["cells"][by_id["A"].cell_index]["source"])
    assert nb["cells"][by_id["A"].cell_index]["cell_type"] == "code"
    assert "raise NotImplementedError(" in hueco
    assert "def broken(:" not in hueco, "el código que no parsea no va a una celda code"


def test_forbidden_patterns() -> None:
    assert check_node_translation(_nt("A", ["df.to_parquet('x')\n"])).reason == "forbidden_pattern"
    assert check_node_translation(_nt("A", ["import duckdb\n"])).reason == "forbidden_pattern"
    fstring_sql = _nt("A", ['q = f"SELECT * FROM {tabla}"\n'])
    failure = check_node_translation(fstring_sql)
    assert failure.reason == "forbidden_pattern" and "f-string" in failure.detail


def test_fstring_without_sql_is_fine() -> None:
    assert check_node_translation(_nt("A", ['msg = f"filas: {n}"\n'])) is None


def test_unresolvable_import_fails() -> None:
    failure = check_node_translation(
        _nt("A", ["x = 1\n"], imports=["import paquete_inexistente_xyz"])
    )
    assert failure.reason == "unresolvable_import"
    # dentro de una celda además viola el contrato de ubicación
    failure2 = check_node_translation(_nt("A", ["import otro_fantasma_zz\nx = 1\n"]))
    assert failure2.reason == "import_in_cell"


def test_allowlist_no_depende_del_entorno_del_migrador() -> None:
    """El bug real: requests/matplotlib eran válidos por prompt y auditoría,
    pero el chequeo validaba con find_spec contra ESTE entorno y rechazó 7
    nodos. Un import de la allowlist pasa aunque no esté instalado acá."""
    nt = _nt("A", ["x = 1\n"], imports=["import libreria_del_cliente_zz"])
    assert check_node_translation(nt).reason == "unresolvable_import"
    assert check_node_translation(nt, ["libreria_del_cliente_zz"]) is None
    # la stdlib pasa siempre, sin declararla
    assert check_node_translation(_nt("A", ["x = 1\n"], imports=["import json"])) is None
    # los default (contrato de prompts + auditoría) pasan sin config
    assert check_node_translation(
        _nt("A", ["x = 1\n"], imports=["import requests", "import matplotlib"])
    ) is None


def test_requirements_txt_documenta_el_entorno_destino(tmp_path: Path) -> None:
    translations = {
        "A": _nt("A", ["r = requests.get('https://api').status_code\nx = 1\n"],
                 imports=["import requests", "import json"]),
    }
    _, failures = assemble_notebooks(_plan("A"), translations, tmp_path / "output")
    assert not failures
    req = (tmp_path / "output" / "requirements.txt").read_text(encoding="utf-8")
    lines = [l for l in req.splitlines() if l and not l.startswith("#")]
    # terceros usados de verdad (baseline pandas/numpy + requests); json es stdlib
    assert lines == ["numpy", "pandas", "requests"]


def test_empty_translation_fails() -> None:
    assert check_node_translation(_nt("A", ["   \n"])).reason == "empty_translation"


def test_swallowed_exception_y_su_falso_positivo() -> None:
    """El hueco real de producción: except tipado cuyo cuerpo solo imprime."""
    tragado = _nt("A", [
        "try:\n    x = int('a')\nexcept ValueError as e:\n    print(e)\n"
    ])
    assert check_node_translation(tragado).reason == "swallowed_exception"
    solo_pass = _nt("A", [
        "try:\n    x = int('a')\nexcept ValueError:\n    pass\n"
    ])
    assert check_node_translation(solo_pass).reason == "swallowed_exception"
    # falso positivo curado: loguear Y re-lanzar es manejo legítimo
    relanza = _nt("A", [
        "try:\n    x = int('a')\nexcept ValueError as e:\n    print(e)\n    raise\n"
    ])
    assert check_node_translation(relanza) is None
    # asignar un default explícito también es una decisión, no un silencio
    con_manejo = _nt("A", [
        "try:\n    x = int('a')\nexcept ValueError:\n    x = 0\n"
    ])
    assert check_node_translation(con_manejo) is None


def test_drop_table_en_comentario_no_es_drop_table() -> None:
    """3 nodos reales cayeron por comentarios que mencionaban drop table."""
    comentario = _nt("A", [
        "# jamás hacer drop table acá: el reemplazo es DELETE+append\nx = 1\n"
    ])
    assert check_node_translation(comentario) is None
    real = _nt("A", ['sql = "DROP TABLE dbo.resumen"\nx = 1\n'])
    failure = check_node_translation(real)
    assert failure.reason == "forbidden_pattern" and "DROP TABLE" in failure.detail
    # to_parquet comentado tampoco cuenta
    assert check_node_translation(_nt("A", ["# sin to_parquet\nx = 1\n"])) is None


def test_row_by_row_con_escape_auditable() -> None:
    loop = (
        "for _, row in df.iterrows():\n"
        "    conn.execute(stmt, row['id'])\n"
    )
    assert check_node_translation(_nt("A", [loop])).reason == "row_by_row_write"
    # la válvula exige MOTIVO; el marcador queda greppeable para el revisor
    con_escape = _nt("A", [
        "# sas-migrator: permitir-loop-filas — SP legado exige una llamada por fila\n"
        + loop
    ])
    assert check_node_translation(con_escape) is None
    # marcador sin motivo no vale
    sin_motivo = _nt("A", ["# sas-migrator: permitir-loop-filas\n" + loop])
    assert check_node_translation(sin_motivo).reason == "row_by_row_write"


def test_check_all_reporta_todas_las_fallas_en_una_pasada() -> None:
    """Un nodo con 4 problemas se corrige en 1 ronda de retry, no en 4."""
    from sas_migrator.core.assembly.notebook import check_node_translation_all

    nt = _nt("A", [
        "try:\n    x = carga()\nexcept:\n    pass\n",       # bare_except
        'q = f"SELECT * FROM {tabla}"\n',                    # fstring SQL
        "df.to_parquet('x.parquet')\n",                      # forbidden
        "y = (\n",                                           # syntax error
    ])
    reasons = {f.reason for f in check_node_translation_all(nt)}
    assert {"bare_except", "forbidden_pattern", "syntax_error"} <= reasons
    assert len(reasons) >= 3
    # la primera falla sigue siendo el contrato de check_node_translation
    assert check_node_translation(nt).reason in reasons


def test_node_translation_contrato_fuerte() -> None:
    """cells:[] validó DOS veces en producción y produjo nodos vacíos que
    parecían traducidos. Ahora es error de validación (retry inmediato del
    LLM), y la strategy inventada también."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        NodeTranslation(node_id="A", cells=[])
    with pytest.raises(pydantic.ValidationError):
        NodeTranslation(node_id="A", cells=["x = 1\n"], strategy="polars")
    # las descriptions viajan en el JSON Schema al modelo: son instrucciones
    schema = NodeTranslation.model_json_schema()
    assert all(
        "description" in prop for prop in schema["properties"].values()
    ), "todos los campos del output_model deben instruir al modelo"


def test_strategy_mismatch_reported(tmp_path: Path) -> None:
    translations = {"A": _nt("A", ["x = 1\n"], strategy="sql_pushdown")}
    _, failures = assemble_notebooks(_plan("A"), translations, tmp_path / "output")
    assert failures and failures[0].reason == "strategy_mismatch"


# ── Scanner de secretos (hardening Etapa 6) ─────────────────────────────────

def test_secret_password_literal_fails() -> None:
    failure = check_node_translation(_nt("A", ["password = 'hunter22'\nx = 1\n"]))
    assert failure.reason == "secret_detected"


def test_secret_api_key_and_token_fail() -> None:
    assert check_node_translation(
        _nt("A", ["KEY = 'sk-ant-abc123XYZ_9'\n"])
    ).reason == "secret_detected"
    assert check_node_translation(
        _nt("A", ["h = {'Authorization': 'Bearer abcdef0123456789TOKEN'}\n"])
    ).reason == "secret_detected"
    assert check_node_translation(
        _nt("A", ["aws = 'AKIAIOSFODNN7EXAMPLE'\n"])
    ).reason == "secret_detected"


def test_secret_env_lookup_is_fine() -> None:
    ok = _nt("A", ["pwd = os.environ.get('DB_PASSWORD')\nx = 1\n"],
             imports=["import os"])
    assert check_node_translation(ok) is None


# ── Rutas absolutas (hardening) ─────────────────────────────────────────────

def test_absolute_paths_fail() -> None:
    for cell in (
        'df = pd.read_csv("C:/datos/ventas.csv")\n',
        'df = pd.read_csv(r"C:\datos\ventas.csv")\n',
        'df.to_excel(r"\\\\srvarchivos\\publico\\salida.xlsx")\n',
        'df = pd.read_sas("/BCCH/GEM_DCNI/Data/t_aj.sas7bdat")\n',
    ):
        failure = check_node_translation(_nt("A", [cell]))
        assert failure is not None and failure.reason == "absolute_path", cell


def test_relative_paths_urls_and_formats_are_fine() -> None:
    ok = _nt("A", [
        'salida = Path("salidas") / "resumen.csv"\n'
        'df.to_csv(salida, index=False)\n'
        'fecha = x.strftime("%d/%m/%Y")\n'
        'api = "https://api.ejemplo.cl/v1/datos"\n',
    ], imports=["from pathlib import Path"])
    assert check_node_translation(ok) is None


def test_drop_table_and_replace_write_fail() -> None:
    assert check_node_translation(
        _nt("A", ["engine.execute('DROP TABLE dbo.X')\n"])
    ).reason == "forbidden_pattern"
    failure = check_node_translation(
        _nt("A", ["df.to_sql('X', engine, if_exists='replace')\n"])
    )
    assert failure.reason == "forbidden_pattern" and "DDL" in failure.detail


# ── Celda de parámetros: macro vars del SAS que el .egp no define ───────────

def test_macro_vars_suben_a_celda_parameters_con_valor_del_proyecto() -> None:
    from sas_migrator.core.assembly.notebook import _parameters_cell

    cell = _parameters_cell(
        [{"macro_params": ["ANIO", "TRIM"]}, {"macro_params": ["TRIM"]}],
        {"ANIO": 2024, "TRIM": 4},
    )
    assert cell.metadata["tags"] == ["parameters"], "papermill inyecta por esta tag"
    assert "ANIO = 2024" in cell.source and "TRIM = 4" in cell.source
    # Todo declarado ⇒ nada que reclamar en runtime.
    assert "raise ValueError" not in cell.source


def test_macro_var_sin_valor_falla_al_ejecutar_diciendo_cual() -> None:
    """Un proceso trimestral sin trimestre no tiene default razonable."""
    from sas_migrator.core.assembly.notebook import _parameters_cell

    cell = _parameters_cell([{"macro_params": ["ANIO", "TRIM"]}], {"ANIO": 2024})
    assert "TRIM = None" in cell.source
    assert "raise ValueError" in cell.source
    ns: dict = {}
    with pytest.raises(ValueError, match="TRIM"):
        exec(cell.source, ns)  # noqa: S102 - se valida el código emitido


def test_flujo_sin_macro_vars_no_lleva_celda_de_parametros() -> None:
    from sas_migrator.core.assembly.notebook import _parameters_cell

    assert _parameters_cell([{"macro_params": []}, {}]) is None


def test_credenciales_nunca_llegan_a_los_parametros_del_notebook() -> None:
    """El notebook se commitea: &user/&password van a os.environ, no acá."""
    from sas_migrator.core.planning import is_credential_macro

    for secreto in ("user", "password", "&PWD", "api_key", "auth_token", "MiSecret"):
        assert is_credential_macro(secreto), secreto
    for normal in ("ANIO", "TRIM", "Sector", "Entrada", "C_WTW"):
        assert not is_credential_macro(normal), normal


# ── Relleno que aparenta funcionar ──────────────────────────────────────────
#
# Todo lo de acá compila y corre. El chequeo no busca código roto: busca código
# que entrega números equivocados sin lanzar nada.

def test_placeholder_comment_fails() -> None:
    f = check_node_translation(_nt("A", [
        "# placeholder; se cargaría con pd.read_sas(...)\n"
        "ratio = pd.DataFrame({'x': []})\n"
    ]))
    assert f is not None and f.reason == "placeholder_stub"
    assert "NotImplementedError" in f.detail


def test_placeholder_word_inside_a_string_is_not_a_comment() -> None:
    """Tokenizamos: un '#' dentro de un literal no es un comentario."""
    assert check_node_translation(_nt("A", [
        "col = '#placeholder'\n"
        "df = pd.DataFrame({'c': [col]})\n"
    ])) is None


def test_empty_frame_under_a_guard_fails() -> None:
    """El patrón que produce cifras faltantes sin una sola excepción."""
    f = check_node_translation(_nt("A", [
        "ratio = pd.DataFrame()\n",
        "if len(ratio) > 0:\n    total = ratio['x'].sum()\n",
    ]))
    assert f is not None and f.reason == "empty_frame_guard"
    assert "ratio" in f.detail


def test_empty_frame_accumulator_is_fine() -> None:
    """Si se reasigna, el guard sí puede entrar: no es este bug."""
    assert check_node_translation(_nt("A", [
        "acumulado = pd.DataFrame()\n"
        "for parte in [1, 2]:\n"
        "    acumulado = pd.concat([acumulado, pd.DataFrame({'x': [parte]})])\n",
        "if len(acumulado) > 0:\n    total = acumulado['x'].sum()\n",
    ])) is None


def test_empty_frame_without_a_guard_is_fine() -> None:
    assert check_node_translation(_nt("A", ["vacio = pd.DataFrame()\n"])) is None


def test_bare_except_fails() -> None:
    f = check_node_translation(_nt("A", [
        "try:\n    dcv = pd.read_parquet('inputs/dcv.parquet')\nexcept:\n    dcv = None\n"
    ]))
    assert f is not None and f.reason == "bare_except"


def test_typed_except_is_fine() -> None:
    assert check_node_translation(_nt("A", [
        "try:\n    dcv = pd.read_parquet('inputs/dcv.parquet')\n"
        "except FileNotFoundError:\n    raise\n"
    ])) is None


def test_self_assignment_fails() -> None:
    f = check_node_translation(_nt("A", ["t_sector = t_sector\n"]))
    assert f is not None and f.reason == "self_assignment"
    assert "t_sector" in f.detail


def test_strip_self_assignments_cura_el_tic_de_nivel_modulo() -> None:
    from sas_migrator.core.assembly.notebook import strip_self_assignments

    nt = _nt("A", ["ANIO = ANIO\nventas = 1\n"])
    fixed = strip_self_assignments(nt)
    assert "ANIO = ANIO" not in fixed.cells[0]
    assert "ventas = 1" in fixed.cells[0]
    assert any("autofix" in w and "ANIO = ANIO" in w for w in fixed.warnings)
    assert check_node_translation(fixed) is None


def test_strip_self_assignments_no_toca_bloques_anidados() -> None:
    """Dentro de un if/for, borrar la única sentencia dejaría un cuerpo vacío
    (SyntaxError): ahí no se toca y el chequeo sigue rechazando."""
    from sas_migrator.core.assembly.notebook import strip_self_assignments

    nt = _nt("A", ["if True:\n    t_sector = t_sector\n"])
    fixed = strip_self_assignments(nt)
    assert fixed is nt
    f = check_node_translation(fixed)
    assert f is not None and f.reason == "self_assignment"


def test_chequear_codigo_generado_no_ensucia_la_consola() -> None:
    """Una celda con una regex en string no-raw ('D\\.') hacía que Python
    emitiera un SyntaxWarning por CADA pasada de parseo (chequeo, símbolos,
    ensamblado, auditoría). El usuario no puede corregir eso a mitad de
    corrida y ahogaba el progreso por nodo."""
    import warnings

    cell = (
        "ventas = pd.DataFrame({'c': ['D.1']})\n"
        "malos = ventas['c'].str.contains('D\\.', na=False)\n"
    )
    with warnings.catch_warnings(record=True) as capturadas:
        warnings.simplefilter("always")
        assert check_node_translation(_nt("A", [cell])) is None

    assert [w for w in capturadas if issubclass(w.category, SyntaxWarning)] == []


def test_error_de_sintaxis_real_sigue_reportandose() -> None:
    """Silenciar el ruido no puede tapar lo accionable: un SyntaxError de
    verdad se sigue rechazando (y el nodo se re-traduce)."""
    f = check_node_translation(_nt("A", ["ventas = (1 + \n"]))
    assert f is not None and f.reason == "syntax_error"


def test_strip_self_assignments_sin_tic_devuelve_el_mismo_objeto() -> None:
    from sas_migrator.core.assembly.notebook import strip_self_assignments

    nt = _nt("A", ["ventas = 1\n"])
    assert strip_self_assignments(nt) is nt


def test_sql_where_1_equals_1_without_predicates_fails() -> None:
    f = check_node_translation(_nt("A", [
        'q = """SELECT * FROM bd_ctsi WHERE 1=1 ORDER BY ANIO"""\n'
    ]))
    assert f is not None and f.reason == "sql_no_op"


def test_sql_where_1_equals_1_with_predicates_is_fine() -> None:
    assert check_node_translation(_nt("A", [
        'q = "SELECT * FROM bd_ctsi WHERE 1=1 AND SECTOR = 412"\n'
    ])) is None


def test_row_by_row_insert_fails() -> None:
    f = check_node_translation(_nt("A", [
        "for _, row in af62.iterrows():\n"
        "    conn.execute(q, {'sector': row['SECTOR']})\n"
    ]))
    assert f is not None and f.reason == "row_by_row_write"
    assert "to_sql" in f.detail


def test_bulk_write_is_fine() -> None:
    assert check_node_translation(_nt("A", [
        "af62.to_sql('bd_ctsi', engine, if_exists='append', index=False)\n"
    ])) is None


def test_iterrows_without_a_write_is_fine() -> None:
    assert check_node_translation(_nt("A", [
        "for _, row in af62.iterrows():\n    print(row['SECTOR'])\n"
    ])) is None


# ── Nombres sin definir (cruza celdas y nodos) ──────────────────────────────

def test_undefined_name_marca_el_nodo_pero_lo_emite(tmp_path: Path) -> None:
    """'asume que viene de un nodo anterior' cuando no viene de ninguno.

    El caso que costó 55 nodos en producción: se sigue reportando, pero el
    trabajo queda EN el notebook, marcado y revisable, en vez de perdido.
    """
    translations = {"A": _nt("A", ["total = bd_ctsi['DATO'].sum()\n"],
                             confidence="medium", warnings=["asumí que bd_ctsi ya existe"])}
    mapping, failures = assemble_notebooks(
        _plan("A"), translations, tmp_path / "output",
        verdicts={"A": "revise"},
    )

    assert [f.reason for f in failures] == ["undefined_name"]
    assert failures[0].emitted is True
    assert "bd_ctsi" in failures[0].detail

    entry = mapping.mappings[0]
    assert entry.degraded is True
    assert entry.degraded_reason.startswith("undefined_name:")

    nb = _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")
    sources = ["".join(c["source"]) for c in nb["cells"]]
    banner = next(s for s in sources if s.startswith("## Nodo A"))
    assert "Nodo degradado" in banner
    assert "`undefined_name`" in banner
    assert "**Confianza del traductor:** medium · **Verificador:** revise" in banner
    assert "asumí que bd_ctsi ya existe" in banner
    # el código traducido sigue estando, tal cual, en su celda
    codigo = "".join(nb["cells"][entry.cell_index]["source"])
    assert "total = bd_ctsi['DATO'].sum()" in codigo


def test_name_defined_by_an_earlier_node_is_visible(tmp_path: Path) -> None:
    translations = {
        "A": _nt("A", ["bd_ctsi = pd.DataFrame({'DATO': [1.0]})\n"]),
        "B": _nt("B", ["total = bd_ctsi['DATO'].sum()\n"]),
    }
    _, failures = assemble_notebooks(_plan("A", "B"), translations, tmp_path / "output")
    assert failures == []


def test_a_rejected_node_does_not_blame_the_next_one(tmp_path: Path) -> None:
    """Se reporta la causa raíz una vez, no una cascada por todo el notebook."""
    translations = {
        "A": _nt("A", ["bd = origen.copy()\n"]),          # 'origen' no existe
        "B": _nt("B", ["total = bd['DATO'].sum()\n"]),    # 'bd' lo definía A
    }
    _, failures = assemble_notebooks(_plan("A", "B"), translations, tmp_path / "output")
    assert [f.node_id for f in failures] == ["A"]


def test_imports_params_and_engine_count_as_defined(tmp_path: Path) -> None:
    plan = {"targets": [{
        "node_id": "A", "node_label": "Nodo A", "strategy": "pandas",
        "notebook_path": "output/NB-01_demo.ipynb", "macro_params": ["ANIO"],
    }]}
    translations = {"A": _nt("A", [
        "q = text('SELECT 1')\n"
        "with engine.begin() as conn:\n"
        "    conn.execute(q)\n"
        "periodo = Path('data') / str(ANIO)\n",
    ], imports=["from sqlalchemy import text", "from pathlib import Path"])}
    _, failures = assemble_notebooks(
        plan, translations, tmp_path / "output", db_bootstrap=True
    )
    assert failures == []


def test_comprehensions_lambdas_and_functions_do_not_trip_the_check(tmp_path: Path) -> None:
    """Ante la duda el chequeo calla: un falso positivo manda a needs_human un
    nodo que estaba bien, que es el error caro."""
    translations = {"A": _nt("A", [
        "pesos = [(321, 0.2), (511, 0.33)]\n"
        "partes = [sector for sector, peso in pesos if peso > 0.1]\n"
        "por_clave = {s: p for s, p in pesos}\n"
        "def ajustar(df, factor=1.0):\n"
        "    return df.assign(DATO=lambda x: x['DATO'] * factor * escala)\n"
        "escala = 2.0\n"
        "salida = ajustar(pd.DataFrame({'DATO': [1.0]}))\n"
    ])}
    _, failures = assemble_notebooks(_plan("A"), translations, tmp_path / "output")
    assert failures == []


def test_fstring_that_merely_mentions_a_sql_verb_is_fine() -> None:
    """Un print de avance no es SQL dinámico: se exige forma de sentencia."""
    assert check_node_translation(_nt("A", [
        'n = 3\nprint(f"insert completado: {n} filas escritas")\n'
    ])) is None


def test_fstring_that_builds_a_real_statement_still_fails() -> None:
    for src in (
        'q = f"SELECT * FROM {tabla}"\n',
        'q = f"INSERT INTO {tabla} VALUES (1)"\n',
        'q = f"DELETE FROM {tabla}"\n',
        'q = f"UPDATE {tabla} SET x = 1"\n',
    ):
        f = check_node_translation(_nt("A", [src]))
        assert f is not None and f.reason == "forbidden_pattern", src


# ── Logging por celda (decisión B5b) ────────────────────────────────────────

def test_cell_logging_on_define_helper_y_acepta_las_llamadas(tmp_path: Path) -> None:
    translations = {"A": _nt("A", ['x = pd.DataFrame()\n_log("x", x)\n'])}
    mapping, failures = assemble_notebooks(
        _plan("A"), translations, tmp_path / "output", cell_logging=True
    )
    assert failures == [], "con el feature ON, _log es un nombre conocido"

    nb = _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")
    sources = ["".join(c["source"]) for c in nb["cells"]]
    config = sources[1]
    assert "def _log(label, value=None):" in config
    assert '_LOG_PATH = Path("log") / "NB-01_demo.log"' in config
    assert "=== corrida" in config, "la marca de corrida se escribe al configurar"
    assert "import datetime" in config and "from pathlib import Path" in config
    assert any('_log("x", x)' in s for s in sources[2:]), "la llamada del nodo queda"
    # stdlib del helper no contamina el contrato del entorno destino
    reqs = (tmp_path / "output" / "requirements.txt").read_text(encoding="utf-8")
    assert "datetime" not in reqs and "pathlib" not in reqs


def test_cell_logging_off_stripea_llamadas_de_traducciones_cacheadas(tmp_path: Path) -> None:
    """Resume real: state/translations/ traducido con ON, ensamblado con OFF."""
    translations = {
        "A": _nt("A", ['x = pd.DataFrame()\n_log("x", x)\n', '_log("solo log")\n'])
    }
    mapping, failures = assemble_notebooks(
        _plan("A"), translations, tmp_path / "output"  # default OFF
    )
    assert failures == [], "el strip elimina la causa del undefined_name"

    raw = (tmp_path / "output" / "NB-01_demo.ipynb").read_text(encoding="utf-8")
    assert "_log" not in raw
    nb = _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")
    sources = ["".join(c["source"]) for c in nb["cells"]]
    assert any("x = pd.DataFrame()" in s for s in sources), "la lógica se conserva"
    assert "def _log" not in sources[1] and "import datetime" not in sources[1]
    by_id = {m.node_id: m for m in mapping.mappings}
    assert by_id["A"].cell_count == 1, "la celda que era puro _log se descarta"


def test_cell_logging_off_sin_llamadas_no_cambia_nada(tmp_path: Path) -> None:
    translations = {"A": _nt("A", ["x = pd.DataFrame()\n"])}
    _, failures = assemble_notebooks(_plan("A"), translations, tmp_path / "output")
    assert failures == []
    raw = (tmp_path / "output" / "NB-01_demo.ipynb").read_text(encoding="utf-8")
    assert "_log" not in raw and "datetime" not in raw


def test_except_que_solo_loguea_sigue_siendo_swallowed_exception() -> None:
    f = check_node_translation(_nt("A", [
        'try:\n    x = pd.read_csv("a.csv")\nexcept Exception:\n    _log("fallo")\n'
    ]))
    assert f is not None and f.reason == "swallowed_exception"


# ── Degradación: el nodo se emite, no se pierde ─────────────────────────────

def test_import_que_no_resuelve_no_tumba_el_notebook_entero(tmp_path: Path) -> None:
    """Emitir el nodo no puede costar el notebook completo.

    Su import iría a la celda 1, que corre ANTES de todo: un solo nodo malo
    dejaría a los otros sin ejecutar. Se aísla en la celda del nodo que lo pidió.
    """
    translations = {
        "A": _nt("A", ["x = fantasma_zz.load()\n"], imports=["import fantasma_zz"]),
        "B": _nt("B", ["y = 2\n"]),
    }
    mapping, failures = assemble_notebooks(_plan("A", "B"), translations, tmp_path / "output")

    assert [f.reason for f in failures] == ["unresolvable_import"]
    nb = _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")
    config = "".join(nb["cells"][1]["source"])
    assert "import fantasma_zz" not in config, "la celda de configuración queda sana"

    by_id = {m.node_id: m for m in mapping.mappings}
    codigo = "".join(nb["cells"][by_id["A"].cell_index]["source"])
    assert "import fantasma_zz" in codigo
    # y sigue documentado como contrato del entorno destino
    req = (tmp_path / "output" / "requirements.txt").read_text(encoding="utf-8")
    assert "fantasma_zz" in req


def test_engine_cae_al_default_del_proyecto_si_nadie_exporta_la_env(tmp_path: Path) -> None:
    """Sin ``SASMIG_DB_URL`` la celda 1 hacía ``KeyError`` y moría el notebook."""
    plan = _plan("A")
    translations = {"A": _nt("A", ["df = pd.read_sql('SELECT 1', engine)\n"])}
    url = "mssql+pyodbc:///?odbc_connect=DRIVER={x};SERVER=tcp:PLATDAT,1433"
    _, failures = assemble_notebooks(
        plan, translations, tmp_path / "output",
        db_bootstrap=True, db_url_default=url,
    )
    assert failures == []
    config = "".join(_read_nb(tmp_path / "output" / "NB-01_demo.ipynb")["cells"][1]["source"])
    assert 'os.environ.get("SASMIG_DB_URL"' in config
    assert url in config
    assert "SUPUESTO" in config, "la conexión asumida queda anotada, no silenciosa"


def test_un_plan_con_pushdown_define_engine_aunque_no_haya_conexiones() -> None:
    """Red de seguridad independiente de la entrevista (§2b).

    Si el plan aprobado manda a un nodo a empujar SQL, el notebook TIENE que
    definir ``engine`` venga de donde venga la decisión de conectar.
    """
    from sas_migrator.llm.phases import _plan_needs_db

    assert _plan_needs_db({"targets": [{"strategy": "hybrid"}]})
    assert _plan_needs_db({"targets": [{"strategy": "pandas"}, {"strategy": "sql_pushdown"}]})
    assert not _plan_needs_db({"targets": [{"strategy": "pandas"}]})
    assert not _plan_needs_db({"targets": []})


def test_la_confianza_del_traductor_llega_al_notebook(tmp_path: Path) -> None:
    """`confidence`/`traceability` se persistían y NUNCA llegaban al .ipynb."""
    from sas_migrator.core.models.translation import Traceability

    translations = {"A": _nt(
        "A", ["x = 1\n"], confidence="high",
        traceability=Traceability(sas_construct="PROC IML — Cholette-Dagum"),
    )}
    assemble_notebooks(
        _plan("A"), translations, tmp_path / "output", verdicts={"A": "approve"},
    )
    sources = ["".join(c["source"])
               for c in _read_nb(tmp_path / "output" / "NB-01_demo.ipynb")["cells"]]
    md = next(s for s in sources if s.startswith("## Nodo A"))
    assert md == "## Nodo A\n\n*confianza: high · verificador: approve · SAS: PROC IML — Cholette-Dagum*"
