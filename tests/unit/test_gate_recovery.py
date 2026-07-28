"""Recuperación tras gate bloqueado — el flujo del error más común del sistema.

El ciclo real de una migración con needs_human es: la fase registra items →
el gate bloquea → un humano resuelve editando el YAML → `resume` re-evalúa el
gate y la corrida sigue. Hasta P0.4 ese ciclo no tenía NINGÚN test (y de hecho
estaba roto: `resume` era un no-op sobre el checkpoint terminal de
gate_blocked).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sas_migrator.core.utils import needs_human
from sas_migrator.service import MigrationSession, SessionStatus
from sas_migrator.testing.egp_builder import build_egp


def make_workspace(root: Path) -> Path:
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    build_egp(ws / "input" / "egp" / "demo.egp")
    return ws


def _blocked_at_gate6(tmp_path: Path) -> tuple[MigrationSession, Path]:
    """Corrida stub que bloquea en el gate 6 por un needs_human pre-sembrado."""
    ws = make_workspace(tmp_path)
    state = ws / "state"
    state.mkdir()
    needs_human.record(
        state, phase=6, task="assembly", node_id="CodeTask-1",
        reason="static_check_failed", detail="sembrado por el test",
    )
    session = MigrationSession(ws)
    result = session.start(stub_mode=True)
    assert result.status == SessionStatus.BLOCKED
    assert any("needs_human" in e for e in result.gate_errors)
    return session, state


def _resolve_all(state: Path) -> None:
    doc = yaml.safe_load((state / "needs_human.yaml").read_text(encoding="utf-8"))
    for item in doc["items"]:
        item["resolved"] = True
        item["resolution"] = "resuelto por el test"
    (state / "needs_human.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_resume_con_item_abierto_sigue_bloqueado(tmp_path: Path) -> None:
    """El retry no pasa por cansancio: mientras el item siga sin resolver,
    re-evaluar el gate vuelve a bloquear (y lo dice)."""
    session, _state = _blocked_at_gate6(tmp_path)

    result = session.resume()

    assert result.status == SessionStatus.BLOCKED
    assert any("needs_human" in e for e in result.gate_errors)


def test_resolver_y_resume_completa_la_corrida(tmp_path: Path) -> None:
    """El ciclo completo: bloqueado → resolver el YAML → resume → COMPLETED.
    El gate 6 queda registrado dos veces en la historia (fallido y pasado)."""
    session, state = _blocked_at_gate6(tmp_path)
    _resolve_all(state)

    result = session.resume()

    assert result.status == SessionStatus.COMPLETED
    assert result.completed_phases == list(range(9))
    snapshot = session.graph.get_state(session._config())
    gate6 = [g for g in snapshot.values["gate_history"] if g["phase"] == 6]
    assert [g["passed"] for g in gate6] == [False, True], (
        "la historia conserva el bloqueo Y el reintento que pasó"
    )


def test_retry_gate_no_reejecuta_la_fase(tmp_path: Path, monkeypatch) -> None:
    """Re-evaluar el gate es barato y puro: la fase 6 NO se re-corre.
    Para rehacer la fase existe rewind_to_phase.

    El espía va sobre stub_generate_notebooks (resuelto en tiempo de llamada
    dentro de phase6_generation): si la fase se re-ejecutara, explota.
    """
    session, state = _blocked_at_gate6(tmp_path)
    _resolve_all(state)

    from sas_migrator.graph import stubs

    def boom(*_args):
        raise AssertionError("retry_gate no debe re-ejecutar la fase 6")

    monkeypatch.setattr(stubs, "stub_generate_notebooks", boom)
    result = session.retry_gate()
    assert result.status == SessionStatus.COMPLETED


def test_retry_gate_sin_gate_bloqueado_es_error_claro(tmp_path: Path) -> None:
    session = MigrationSession(make_workspace(tmp_path))
    session.start(stub_mode=True)  # completa sin bloqueos

    with pytest.raises(LookupError, match="no hay gate bloqueado"):
        session.retry_gate()


def test_resume_sin_bloqueo_conserva_su_semantica(tmp_path: Path) -> None:
    """resume() sobre una corrida completa sigue siendo un continue normal,
    no un retry — la delegación aplica solo al terminal gate_blocked."""
    session = MigrationSession(make_workspace(tmp_path))
    session.start(stub_mode=True)

    result = session.resume()

    assert result.status == SessionStatus.COMPLETED
