"""Ensamblador determinista: template, cell_index por construcción y
chequeos estáticos que dejan al nodo fuera antes de escribir un notebook roto."""

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
    # anclas del audit
    assert "## Nodo A" in sources and "## Nodo B" in sources
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

def test_syntax_error_omits_node_and_reports_failure(tmp_path: Path) -> None:
    translations = {"A": _nt("A", ["def broken(:\n"]), "B": _nt("B", ["ok = 1\n"])}
    mapping, failures = assemble_notebooks(_plan("A", "B"), translations, tmp_path / "output")

    assert [f.node_id for f in failures] == ["A"]
    assert failures[0].reason == "syntax_error"
    assert [m.node_id for m in mapping.mappings] == ["B"]
    nb_text = (tmp_path / "output" / "NB-01_demo.ipynb").read_text(encoding="utf-8")
    assert "Nodo A" not in nb_text, "el nodo fallido no deja rastro en el notebook"


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

def test_undefined_name_omits_the_node(tmp_path: Path) -> None:
    """'asume que viene de un nodo anterior' cuando no viene de ninguno."""
    translations = {"A": _nt("A", ["total = bd_ctsi['DATO'].sum()\n"])}
    mapping, failures = assemble_notebooks(_plan("A"), translations, tmp_path / "output")

    assert mapping.mappings == []
    assert [f.reason for f in failures] == ["undefined_name"]
    assert "bd_ctsi" in failures[0].detail


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
