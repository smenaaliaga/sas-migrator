"""Reset del workspace — lo derivado se borra, lo humano no se toca."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sas_migrator.cli.main import app
from sas_migrator.core.utils.workspace_reset import apply_reset, human_size, plan_reset


def _workspace(tmp_path: Path, *, con_estado: bool = True) -> Path:
    ws = tmp_path / "mi_migracion"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "egp" / "demo.egp").write_bytes(b"PK\x03\x04 no importa")
    (ws / "input" / "data").mkdir()
    (ws / "input" / "data" / "referencia.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (ws / "project_config.yaml").write_text("db: {}\n", encoding="utf-8")
    if con_estado:
        (ws / "state").mkdir()
        (ws / "state" / "graph_checkpoint.sqlite").write_bytes(b"x" * 2048)
        (ws / "state" / "nodes_index.json").write_text("{}", encoding="utf-8")
        (ws / "state" / "post_analysis_interview.yaml").write_text("q: a\n", encoding="utf-8")
        (ws / "state" / "ignored_nodes.yaml").write_text("ignored_nodes: []\n", encoding="utf-8")
        (ws / "state" / "migration_state.json").write_text(
            json.dumps({"current_phase": 6}), encoding="utf-8"
        )
        (ws / "output").mkdir()
        (ws / "output" / "NB-01.ipynb").write_text("{}", encoding="utf-8")
    return ws


def test_el_plan_no_borra_nada_y_cuenta_lo_derivado(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    plan = plan_reset(ws)

    assert [t.name for t in plan.targets] == ["state", "output"]
    assert plan.files == 6
    assert plan.phase == 6
    # Construir el plan es una lectura: los archivos siguen ahí.
    assert (ws / "state" / "nodes_index.json").exists()


def test_el_plan_nombra_las_decisiones_humanas(tmp_path: Path) -> None:
    """El conteo de archivos no mide lo que duele: volver a contestar."""
    plan = plan_reset(_workspace(tmp_path))
    assert plan.decisions == ["post_analysis_interview.yaml", "ignored_nodes.yaml"]


def test_keep_output_deja_los_notebooks(tmp_path: Path) -> None:
    plan = plan_reset(_workspace(tmp_path), keep_output=True)
    assert [t.name for t in plan.targets] == ["state"]


def test_un_directorio_que_no_es_workspace_no_se_planifica(tmp_path: Path) -> None:
    """Única defensa contra correr el reset parado en la carpeta equivocada."""
    (tmp_path / "output").mkdir()
    with pytest.raises(ValueError, match="no parece un workspace"):
        plan_reset(tmp_path)


def test_apply_vacia_lo_derivado_y_conserva_lo_humano(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    apply_reset(plan_reset(ws))

    # Derivado: los directorios quedan, vacíos y listos para `run`.
    assert (ws / "state").is_dir() and not any((ws / "state").iterdir())
    assert (ws / "output").is_dir() and not any((ws / "output").iterdir())
    # Humano: intacto.
    assert (ws / "input" / "egp" / "demo.egp").exists()
    assert (ws / "input" / "data" / "referencia.csv").exists()
    assert (ws / "project_config.yaml").exists()


def test_workspace_ya_limpio_no_es_un_error(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, con_estado=False)
    plan = plan_reset(ws)
    assert plan.is_empty
    result = CliRunner().invoke(app, ["reset", "--workspace", str(ws)])
    assert result.exit_code == 0
    assert "ya está limpio" in result.output


def test_cli_sin_confirmar_no_borra(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    result = CliRunner().invoke(app, ["reset", "--workspace", str(ws)], input="n\n")

    assert result.exit_code != 0, "abortar debe ser un exit code distinto de 0"
    assert (ws / "state" / "nodes_index.json").exists(), "no se borró nada"
    # Lo que se va a perder se muestra ANTES de preguntar.
    assert "post_analysis_interview.yaml" in result.output
    assert "Se conservan: input/, project_config.yaml" in result.output


def test_cli_confirmando_borra(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    result = CliRunner().invoke(app, ["reset", "--workspace", str(ws)], input="y\n")

    assert result.exit_code == 0, result.output
    assert not any((ws / "state").iterdir())
    assert (ws / "input" / "egp" / "demo.egp").exists()


def test_cli_yes_no_pregunta(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    result = CliRunner().invoke(app, ["reset", "--workspace", str(ws), "--yes"])

    assert result.exit_code == 0, result.output
    assert "¿Borrar y empezar de cero?" not in result.output
    assert not any((ws / "state").iterdir())


def test_human_size() -> None:
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
