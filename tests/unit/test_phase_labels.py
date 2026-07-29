"""Fuente única de fases: el enum Phase etiqueta (PHASE_LABELS) y el grafo
(builder.PHASES) no pueden desalinearse en silencio."""

from __future__ import annotations

from sas_migrator.core.models.state import PHASE_LABELS, Phase


def test_phase_labels_es_biyectivo_con_el_enum() -> None:
    assert set(PHASE_LABELS) == set(Phase), "toda fase tiene etiqueta y viceversa"
    assert all(label.strip() for label in PHASE_LABELS.values())


def test_los_gates_del_grafo_son_fases_del_enum() -> None:
    from sas_migrator.graph.builder import PHASES

    gate_phases = {phase for _, _, phase in PHASES if phase is not None}
    assert gate_phases <= {int(p) for p in Phase}


def test_cli_deriva_sus_nombres_del_enum() -> None:
    from sas_migrator.cli.render import PHASE_NAMES

    assert PHASE_NAMES == {int(p): label for p, label in PHASE_LABELS.items()}


def test_cada_fase_anuncia_arranque_y_cierre_en_orden(tmp_path, capsys) -> None:
    """El log tiene que dejar ver dónde empieza y termina cada fase.

    `invoke()` encadena varias fases antes de devolver, así que el cierre lo
    emite el gate en el instante en que pasa: si lo imprimiera el cliente al
    recibir el resultado, el ✅ de la fase N saldría DESPUÉS del ▶ de la N+1.
    """
    import re

    from sas_migrator.graph.builder import build_graph, initial_state
    from sas_migrator.testing.egp_builder import build_egp

    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    build_graph().invoke(initial_state(ws, build_egp(ws / "input" / "egp" / "d.egp")))

    err = capsys.readouterr().err
    marcas = re.findall(r"(▶|✅) Fase (\d+)", err)
    # Cada fase abre antes de cerrar, y ninguna abre dos veces (la fase 7 son
    # tres sub-nodos y anuncia UN solo arranque).
    for fase in range(9):
        assert marcas.count(("▶", str(fase))) == 1, f"fase {fase} no abre una sola vez"
        assert marcas.index(("▶", str(fase))) < marcas.index(("✅", str(fase)))
    # El arranque lleva el nombre de la fase, no solo el número.
    assert "▶ Fase 2 · Análisis SAS" in err


def test_el_cliente_no_reimprime_el_cierre_que_ya_sali__por_stderr() -> None:
    """Si el CLI reimprimiera messages, el cierre saldría dos veces."""
    from sas_migrator.cli.main import _CIERRE_DE_FASE

    assert _CIERRE_DE_FASE.match("✅ Fase 3 completada")
    assert not _CIERRE_DE_FASE.match("✅ Migración base completa (fases 0-8, gates en verde).")
