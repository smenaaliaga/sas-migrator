"""UX de la CLI — el contrato de lo que el usuario ve y de cómo sale el proceso.

No prueba el pipeline (eso es `test_session.py`): prueba que el preflight
bloquee antes de gastar, que los errores salgan por stderr con código estable,
que `status` diga dónde quedó y qué sigue, y que interrumpir una entrevista sea
una pausa y no un traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from sas_migrator.cli.main import EXIT_BLOCKED, EXIT_INTERRUPTED, EXIT_USAGE, app
from sas_migrator.cli.render import PHASE_NAMES, render_phases
from sas_migrator.service import MigrationSession
from sas_migrator.service.preflight import FAIL, OK, WARN, run_checks
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import default_fake_caller


@pytest.fixture(autouse=True)
def _fake_llm():
    from sas_migrator.llm import runtime

    runtime.set_caller(default_fake_caller())
    yield
    runtime.set_caller(None)


def make_workspace(root: Path) -> Path:
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    build_egp(ws / "input" / "egp" / "demo.egp")
    return ws


# ── Preflight (doctor) ───────────────────────────────────────────────────────


def test_doctor_falla_sin_egp_y_dice_donde_ponerlo(tmp_path: Path) -> None:
    """El fallo más común es el más barato de detectar: no hay .egp."""
    ws = tmp_path / "vacio"
    ws.mkdir()
    result = CliRunner().invoke(app, ["doctor", "-w", str(ws)])
    assert result.exit_code == EXIT_BLOCKED
    assert "input/egp/" in result.output
    # La CLI resuelve el workspace (casing real del disco en Windows); el test
    # compara contra la misma ruta resuelta o la comparación depende del casing
    # de %TEMP%/%USERNAME%.
    assert str(ws.resolve() / "input" / "egp") in result.output  # el hint dice la ruta exacta


def test_doctor_pasa_en_workspace_valido(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    result = CliRunner().invoke(app, ["doctor", "-w", str(ws)])
    assert result.exit_code == 0, result.output
    assert "Se puede correr" in result.output or "Todo en orden" in result.output


def test_doctor_no_confunde_advertencia_con_bloqueo(tmp_path: Path) -> None:
    """Un WARN describe lo que faltará más tarde; no puede impedir arrancar."""
    ws = make_workspace(tmp_path)
    report = run_checks(ws)
    assert report.ok
    assert any(c.status == WARN for c in report.checks), "sin config ni referencias hay warns"
    assert all(c.hint for c in report.checks if c.status != OK), (
        "un chequeo que falla sin decir cómo arreglarlo deja al usuario en la "
        "misma cacería que vino a evitar"
    )


def test_preflight_sin_directorio_no_sigue_chequeando(tmp_path: Path) -> None:
    report = run_checks(tmp_path / "no-existe")
    assert [c.status for c in report.checks] == [FAIL]


def test_doctor_sugiere_status_si_hay_migracion_en_curso(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    MigrationSession(ws).start(stub_mode=True)
    result = CliRunner().invoke(app, ["doctor", "-w", str(ws)])
    assert "sas-migrator status" in result.output
    assert "en curso" in result.output


def test_run_real_sin_credencial_no_arranca(tmp_path: Path, monkeypatch) -> None:
    """El fallo más caro de descubrir tarde: falta la key y pega en la fase 2,
    después de que el usuario ya contestó la entrevista inicial."""
    from sas_migrator.llm import runtime

    monkeypatch.setattr(runtime, "caller_injected", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sas_migrator.llm.env.load_env", lambda ws=None: None)

    ws = make_workspace(tmp_path)
    arrancó = []
    monkeypatch.setattr(
        MigrationSession, "start", lambda self, *a, **k: arrancó.append(True)
    )
    result = CliRunner().invoke(app, ["run", "-w", str(ws)])
    assert result.exit_code == EXIT_USAGE
    assert not arrancó, "no puede gastar una sola llamada sin credencial"
    assert "doctor" in result.output

    # …y --no-check es la salida de emergencia si el chequeo se equivoca
    CliRunner().invoke(app, ["run", "-w", str(ws), "--no-check"])
    assert arrancó


def test_run_stub_no_exige_credencial(tmp_path: Path, monkeypatch) -> None:
    from sas_migrator.llm import runtime

    monkeypatch.setattr(runtime, "caller_injected", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ws = make_workspace(tmp_path)
    result = CliRunner().invoke(app, ["run", "-w", str(ws), "--stub"])
    assert result.exit_code == 0, result.output


# ── status ───────────────────────────────────────────────────────────────────


def test_status_muestra_las_fases_y_el_proximo_paso(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    MigrationSession(ws).start(stub_mode=False)  # se detiene en la primera tarjeta
    result = CliRunner().invoke(app, ["status", "-w", str(ws)])
    assert result.exit_code == 0, result.output
    for name in PHASE_NAMES.values():
        assert name in result.output, "las 10 fases con su estado, no solo el número"
    assert "Entrevista pendiente: B1-initial" in result.output
    assert "Próximo paso: sas-migrator resume" in result.output


def test_status_json_es_parseable(tmp_path: Path) -> None:
    """Un tablero para leer y un JSON para scriptear; nunca los dos mezclados."""
    ws = make_workspace(tmp_path)
    MigrationSession(ws).start(stub_mode=True)
    result = CliRunner().invoke(app, ["status", "-w", str(ws), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert data["completed_phases"] == list(range(9))
    assert data["needs_human"] == []
    assert "llm" in data


def test_status_sin_migracion_sugiere_doctor(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    result = CliRunner().invoke(app, ["status", "-w", str(ws)])
    assert result.exit_code == 0
    assert "Sin migración iniciada" in result.output
    assert "sas-migrator doctor" in result.output


def test_status_no_repite_los_needs_human_como_errores_de_gate(tmp_path: Path) -> None:
    """20 items en la cola no pueden imprimirse dos veces: el tablero se vuelve
    una pared de texto y el error que sí importa queda enterrado."""
    from sas_migrator.core.utils import needs_human

    ws = make_workspace(tmp_path)
    MigrationSession(ws).start(stub_mode=True)
    needs_human.record(
        ws / "state", phase=6, task="assembly", reason="static_check_failed",
        node_id="CodeTask-1", detail="absolute_path: ruta absoluta literal",
    )
    result = CliRunner().invoke(app, ["status", "-w", str(ws)])
    assert result.output.count("NH-001") == 1
    assert "absolute_path" in result.output, "el detail distingue un item de otro"
    assert "needs_human.yaml" in result.output, "dice dónde se resuelve"


def test_render_phases_marca_donde_esta(tmp_path: Path) -> None:
    text = render_phases(3, [0, 1, 2])
    assert "✅ 0  Intake" in text
    assert "▶ 3  Profiling + matching   ← acá" in text
    assert " · 4  Entrevista post-análisis" in text


# ── Interrupción y errores ───────────────────────────────────────────────────


def test_ctrl_c_en_entrevista_es_una_pausa(tmp_path: Path, monkeypatch) -> None:
    """Lo contestado vive en el checkpoint: interrumpir no pierde nada, y el
    mensaje tiene que decirlo en vez de escupir un traceback."""
    ws = make_workspace(tmp_path)

    def _abort(*args, **kwargs):
        raise click.exceptions.Abort()

    monkeypatch.setattr("sas_migrator.cli.main.typer.prompt", _abort)
    result = CliRunner().invoke(app, ["run", "-w", str(ws)])
    assert result.exit_code == EXIT_INTERRUPTED
    assert "Interrumpido" in result.output
    assert "sas-migrator resume" in result.output


def test_guion_incompleto_dice_que_falta(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    script = tmp_path / "answers.yaml"
    script.write_text("answers: {}\n", encoding="utf-8")  # sin default: recommended
    result = CliRunner().invoke(app, ["run", "-w", str(ws), "-a", str(script)])
    assert result.exit_code == EXIT_USAGE
    assert "B1-initial" in result.output
    assert "default: recommended" in result.output


def test_rewind_a_una_fase_no_alcanzada_explica(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    result = CliRunner().invoke(app, ["rewind", "-w", str(ws), "-p", "6"])
    assert result.exit_code == EXIT_USAGE
    assert "sas-migrator status" in result.output


def test_phase_fuera_de_rango_se_rechaza_antes_de_tocar_el_checkpoint(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    result = CliRunner().invoke(app, ["rewind", "-w", str(ws), "-p", "42"])
    assert result.exit_code != 0
    assert "42" in result.output


# ── iterate ──────────────────────────────────────────────────────────────────


def test_iterate_toma_la_descripcion_como_argumento(tmp_path: Path, monkeypatch) -> None:
    ws = make_workspace(tmp_path)
    visto: dict = {}

    def _fake_iterate(self, description, request_type="enhancement", affected_nodes=None):
        visto.update(
            description=description, request_type=request_type, nodes=affected_nodes
        )
        return {"done": True, "entry_id": "IT-001", "cycle": 1, "notes": []}

    monkeypatch.setattr(MigrationSession, "iterate", _fake_iterate)
    result = CliRunner().invoke(
        app,
        ["iterate", "corregir el redondeo", "-w", str(ws), "-n", "A", "-n", "B",
         "-t", "bug_fix"],
    )
    assert result.exit_code == 0, result.output
    assert visto == {
        "description": "corregir el redondeo",
        "request_type": "bug_fix",
        "nodes": ["A", "B"],
    }

    # La forma vieja sigue andando (no figura en el help, pero no se rompe).
    visto.clear()
    CliRunner().invoke(
        app, ["iterate", "-w", str(ws), "--describe", "otra", "--nodes", "A,B"]
    )
    assert visto["description"] == "otra"
    assert visto["nodes"] == ["A", "B"]


def test_iterate_sin_descripcion_no_arranca(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["iterate", "-w", str(tmp_path)])
    assert result.exit_code == EXIT_USAGE
    assert "iterate" in result.output


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "sas-migrator" in result.output
