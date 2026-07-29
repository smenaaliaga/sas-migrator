"""needs_human: nunca silencio — un item sin resolver bloquea el gate de su fase."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from sas_migrator.core.utils import needs_human
from sas_migrator.core.utils.schema_validation import check_gate


def _minimal_phase6_state(tmp_path: Path) -> Path:
    """Estado mínimo que pasa el gate 6 sin needs_human."""
    state = tmp_path / "state"
    state.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (state / "nodes").mkdir()
    (state / "nodes_index.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (state / "sas_python_mapping.json").write_text(
        json.dumps({"mappings": []}), encoding="utf-8"
    )
    nb = {
        "cells": [
            {"cell_type": "code", "source": ["df = df.merge(df)\n"]},
        ]
    }
    (output / "NB-01_x.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    (output / "run_all.py").write_text("NOTEBOOKS = ['NB-01_x.ipynb']\n", encoding="utf-8")
    # El audit lo produce la FASE 6; el gate solo lo lee (gates puros).
    from sas_migrator.core.audit import run_audit

    run_audit(state, output)
    return state


def test_unresolved_item_blocks_its_phase_gate(tmp_path: Path) -> None:
    state = _minimal_phase6_state(tmp_path)
    passed_before, _ = check_gate(6, state)
    assert passed_before

    item = needs_human.record(
        state, phase=6, task="translation", node_id="CodeTask-1",
        reason="validation_retries_exhausted", attempts=3,
    )
    assert item.id == "NH-001"

    passed, errors = check_gate(6, state)
    assert not passed
    assert any("NH-001" in e and "translation/CodeTask-1" in e for e in errors)


def test_resolved_item_unblocks(tmp_path: Path) -> None:
    state = _minimal_phase6_state(tmp_path)
    needs_human.record(state, phase=6, task="translation", reason="refusal")

    doc = yaml.safe_load((state / "needs_human.yaml").read_text(encoding="utf-8"))
    doc["items"][0]["resolved"] = True
    doc["items"][0]["resolution"] = "traducido a mano"
    (state / "needs_human.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")

    passed, errors = check_gate(6, state)
    assert passed, errors


def test_items_only_block_their_own_phase(tmp_path: Path) -> None:
    state = _minimal_phase6_state(tmp_path)
    needs_human.record(state, phase=2, task="analysis_reviews", reason="refusal")

    passed, _ = check_gate(6, state)
    assert passed, "un item de fase 2 no bloquea el gate 6"
    assert [i.id for i in needs_human.unresolved(state, phase=2)] == ["NH-001"]


def test_ids_are_sequential(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    a = needs_human.record(state, phase=2, task="a", reason="refusal")
    b = needs_human.record(state, phase=3, task="b", reason="refusal")
    assert (a.id, b.id) == ("NH-001", "NH-002")


def test_record_es_upsert_no_duplica_al_reejecutar_la_fase(tmp_path: Path) -> None:
    """Re-ejecutar la fase con el mismo nodo caído actualiza el item, no lo
    duplica: el gate cuenta items, y 40 duplicados leen como 40 problemas."""
    state = tmp_path / "state"
    state.mkdir()
    a = needs_human.record(
        state, phase=6, task="assembly", node_id="CodeTask-1",
        reason="static_check_failed", detail="primer intento", attempts=1,
    )
    b = needs_human.record(
        state, phase=6, task="assembly", node_id="CodeTask-1",
        reason="static_check_failed", detail="segundo intento", attempts=3,
    )
    items = needs_human.load_queue(state).items
    assert len(items) == 1
    assert a.id == b.id
    assert items[0].detail == "segundo intento"
    assert items[0].attempts == 3


def test_fallo_reaparecido_tras_resolver_es_item_nuevo(tmp_path: Path) -> None:
    """La historia resuelta se conserva: si la misma clave vuelve a fallar
    después de marcada resolved, eso ES un problema nuevo."""
    state = tmp_path / "state"
    state.mkdir()
    needs_human.record(
        state, phase=6, task="assembly", node_id="CodeTask-1", reason="static_check_failed"
    )
    doc = yaml.safe_load((state / "needs_human.yaml").read_text(encoding="utf-8"))
    doc["items"][0]["resolved"] = True
    (state / "needs_human.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")

    nuevo = needs_human.record(
        state, phase=6, task="assembly", node_id="CodeTask-1", reason="static_check_failed"
    )
    items = needs_human.load_queue(state).items
    assert len(items) == 2
    assert nuevo.id == "NH-002"
    assert not items[1].resolved


def test_ids_no_colisionan_tras_borrado_manual(tmp_path: Path) -> None:
    """`len+1` colisionaba si alguien borró un item del medio; max+1 no."""
    state = tmp_path / "state"
    state.mkdir()
    needs_human.record(state, phase=2, task="a", reason="refusal")
    needs_human.record(state, phase=3, task="b", reason="refusal")
    doc = yaml.safe_load((state / "needs_human.yaml").read_text(encoding="utf-8"))
    del doc["items"][0]  # el humano borró NH-001 a mano
    (state / "needs_human.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")

    c = needs_human.record(state, phase=4, task="c", reason="refusal")
    assert c.id == "NH-003", "no re-usa NH-002 (existe) ni pisa ids"


def test_lo_que_dejo_de_fallar_sale_de_la_cola(tmp_path: Path) -> None:
    """`record` es upsert: nada sacaba de la cola lo que se arregló.

    Al recuperar una corrida real, 55 items de un ensamblado viejo sobrevivieron
    a la corrida que los solucionó y siguieron bloqueando el gate — el único
    camino era editar el YAML a mano, que es justo el trabajo que la cola existe
    para evitar.
    """
    state = tmp_path / "state"
    state.mkdir()
    for nid in ("CodeTask-1", "CodeTask-2", "CodeTask-3"):
        needs_human.record(
            state, phase=6, task="assembly", node_id=nid,
            reason="static_check_failed", detail="undefined_name: engine",
        )

    cerrados = needs_human.resolve_fixed(
        state, phase=6, task="assembly",
        fixed={"CodeTask-1", "CodeTask-2"},
        resolution="el ensamblado de esta corrida ya no reporta el fallo",
    )

    assert sorted(i.node_id for i in cerrados) == ["CodeTask-1", "CodeTask-2"]
    items = {i.node_id: i for i in needs_human.load_queue(state).items}
    assert len(items) == 3, "no se borra nada: la historia queda"
    assert items["CodeTask-1"].resolved and items["CodeTask-1"].resolution
    assert not items["CodeTask-3"].resolved
    assert [i.node_id for i in needs_human.unresolved(state)] == ["CodeTask-3"]


def test_resolve_fixed_no_toca_otra_fase_ni_otro_task(tmp_path: Path) -> None:
    """La lista completa que justifica cerrar vale para UNA (fase, task).

    El ensamblado sabe qué nodos fallaron al ensamblar; no sabe nada de lo que
    la fase 2 dejó pendiente ni de un item de traducción.
    """
    state = tmp_path / "state"
    state.mkdir()
    needs_human.record(state, phase=6, task="translation", node_id="CodeTask-1",
                       reason="validation_retries_exhausted")
    needs_human.record(state, phase=2, task="assembly", node_id="CodeTask-1",
                       reason="static_check_failed")
    needs_human.record(state, phase=6, task="assembly", node_id="CodeTask-1",
                       reason="static_check_failed")

    cerrados = needs_human.resolve_fixed(
        state, phase=6, task="assembly", fixed={"CodeTask-1"}, resolution="ok",
    )
    assert len(cerrados) == 1
    assert len(needs_human.unresolved(state)) == 2


def test_resolve_fixed_ignora_items_sin_nodo(tmp_path: Path) -> None:
    """Un item de fase (sin node_id) no lo cubre ninguna lista de nodos."""
    state = tmp_path / "state"
    state.mkdir()
    needs_human.record(state, phase=6, task="assembly", reason="refusal")
    assert needs_human.resolve_fixed(
        state, phase=6, task="assembly", fixed={"CodeTask-1"}, resolution="ok",
    ) == []
    assert len(needs_human.unresolved(state)) == 1
