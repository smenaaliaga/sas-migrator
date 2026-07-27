"""Ensamblador determinista: template, cell_index por construcción y
chequeos estáticos que dejan al nodo fuera antes de escribir un notebook roto."""

from __future__ import annotations

import json
from pathlib import Path

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
    # también dentro de las celdas
    failure2 = check_node_translation(_nt("A", ["import otro_fantasma_zz\nx = 1\n"]))
    assert failure2.reason == "unresolvable_import"


def test_empty_translation_fails() -> None:
    assert check_node_translation(_nt("A", ["   \n"])).reason == "empty_translation"


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
    ok = _nt("A", ["import os\npwd = os.environ.get('DB_PASSWORD')\nx = 1\n"])
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
        'from pathlib import Path\n'
        'salida = Path("salidas") / "resumen.csv"\n'
        'df.to_csv(salida, index=False)\n'
        'fecha = x.strftime("%d/%m/%Y")\n'
        'api = "https://api.ejemplo.cl/v1/datos"\n',
    ])
    assert check_node_translation(ok) is None


def test_drop_table_and_replace_write_fail() -> None:
    assert check_node_translation(
        _nt("A", ["engine.execute('DROP TABLE dbo.X')\n"])
    ).reason == "forbidden_pattern"
    failure = check_node_translation(
        _nt("A", ["df.to_sql('X', engine, if_exists='replace')\n"])
    )
    assert failure.reason == "forbidden_pattern" and "DDL" in failure.detail
