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
