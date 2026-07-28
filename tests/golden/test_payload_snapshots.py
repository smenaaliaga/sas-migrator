"""Snapshots de cada payload de entrevista (DoD Etapa 3).

Dos guiones sobre el .egp sintético: el camino default y un camino con ramas
(exclusión de flujo, detalle de mejora). El UX lean queda protegido por
regresión: cualquier cambio de texto/opciones/default rompe el snapshot.
"""

from __future__ import annotations

import json

from sas_migrator.core.interview import initial, plan_approval, post_analysis


def _dump(card) -> dict:
    return card.model_dump(mode="json")


# ── Guion default ───────────────────────────────────────────────────────────

def test_snapshot_phase1_initial(synthetic_state, snapshot) -> None:
    snapshot("phase1_initial", _dump(initial.build_initial_card(synthetic_state)))


def test_snapshot_phase4_scope_flows(synthetic_state, snapshot) -> None:
    snapshot(
        "phase4_scope_flows",
        _dump(post_analysis.build_scope_flows_card(synthetic_state)),
    )


def test_snapshot_phase4_native_nodes(synthetic_state, snapshot) -> None:
    cards = post_analysis.build_native_node_cards(synthetic_state)
    assert len(cards) == 1, "el sintético tiene exactamente un nodo nativo (ImportTask-1)"
    snapshot("phase4_native_nodes", [_dump(c) for c in cards])


def test_blocks_without_evidence_do_not_ask(synthetic_state) -> None:
    """UX lean: sin evidencia no hay tarjeta (mapping/preprocessing/ambigüedades
    no aplican al sintético sin archivos de datos ni macro vars)."""
    assert post_analysis.build_mapping_card(synthetic_state) is None
    assert post_analysis.build_preprocessing_card(synthetic_state) is None
    assert post_analysis.build_ambiguities_card(synthetic_state) is None
    assert post_analysis.build_db_mapping_card(synthetic_state) is None


def test_snapshot_phase4_db_step1(synthetic_state, snapshot) -> None:
    snapshot("phase4_db_step1", _dump(post_analysis.build_db_step1_card(synthetic_state)))


def test_snapshot_phase4_placement_resolution(synthetic_state, snapshot) -> None:
    cards = post_analysis.build_placement_resolution_cards(synthetic_state)
    assert [c.card_id for c in cards] == ["B4b:resolve:SRC"], (
        "una tarjeta por causa raíz; SRC es el único prefijo sin confirmar"
    )
    snapshot("phase4_placement_resolution", [_dump(c) for c in cards])


def test_snapshot_phase4_improvements(synthetic_state, snapshot) -> None:
    cards = post_analysis.build_improvement_cards(synthetic_state)
    assert len(cards) >= 1
    for card in cards:
        assert len(card.questions) == 1, "una ficha M-xxx a la vez, una decisión por tarjeta"
        q = card.questions[0]
        assert q.options == post_analysis.IMPROVEMENT_OPTIONS
        assert "postergar" not in [o.lower() for o in q.options], (
            "postponed solo existe vía texto libre"
        )
    snapshot("phase4_improvements", [_dump(c) for c in cards])


def test_snapshot_phase4_cell_logging(synthetic_state, snapshot) -> None:
    snapshot(
        "phase4_cell_logging",
        _dump(post_analysis.build_cell_logging_card(synthetic_state)),
    )


def test_snapshot_phase4_closure(synthetic_state, snapshot) -> None:
    counts = {"migrate": 4, "excluded": 0, "improvements": 0, "assumptions": 1}
    snapshot("phase4_closure", _dump(post_analysis.build_closure_card(synthetic_state, counts)))


def test_snapshot_phase5_plan_approval(synthetic_state, snapshot) -> None:
    snapshot("phase5_plan_approval", _dump(plan_approval.build_plan_card(synthetic_state)))


# ── Guion con ramas ─────────────────────────────────────────────────────────

def test_snapshot_phase4_scope_exclusion_confirm(synthetic_state, snapshot) -> None:
    card = post_analysis.build_scope_exclusion_confirm_card(synthetic_state, ["PFD-1"])
    snapshot("phase4_scope_exclusion_confirm", _dump(card))


def test_snapshot_phase4_improvement_detail(synthetic_state, snapshot) -> None:
    cards = post_analysis.build_improvement_cards(synthetic_state)
    imp_id = cards[0].card_id.split(":", 1)[1]
    detail = post_analysis.build_improvement_detail_card(synthetic_state, imp_id)
    assert detail is not None
    assert detail.questions[0].options == post_analysis.IMPROVEMENT_DETAIL_OPTIONS
    snapshot("phase4_improvement_detail", _dump(detail))


def test_snapshot_phase4_db_connection(synthetic_state, snapshot) -> None:
    card = post_analysis.build_db_connection_card(synthetic_state)
    snapshot("phase4_db_connection", _dump(card))


def test_api_block_without_evidence_does_not_ask(synthetic_state) -> None:
    """UX lean: el .egp sintético no tiene PROC HTTP → B4c no aparece."""
    assert post_analysis.build_api_connection_cards(synthetic_state) == []


def test_snapshot_phase4_api_connection(tmp_path, snapshot) -> None:
    """B4c con fixture propio: el builder lee SOLO http_evidence.json."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "http_evidence.json").write_text(
        json.dumps({
            "generated_at": "2026-01-01T00:00:00+00:00",
            "hosts": [{
                "host": "si3.bcentral.cl",
                "node_ids": ["CodeTask-uf"],
                "methods": ["GET"],
                "sample_urls": ["https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx"],
                "sources": ["proc_http"],
            }],
            "nodes_with_dynamic_url": [],
            "detection_notes": [],
        }),
        encoding="utf-8",
    )
    cards = post_analysis.build_api_connection_cards(state)
    assert [c.card_id for c in cards] == ["B4c:api:si3.bcentral.cl"]
    snapshot("phase4_api_connection", [_dump(c) for c in cards])


# ── Invariantes del UX lean sobre TODOS los payloads ────────────────────────

def _all_cards(state_dir):
    cards = [
        initial.build_initial_card(state_dir),
        post_analysis.build_scope_flows_card(state_dir),
        *post_analysis.build_native_node_cards(state_dir),
        post_analysis.build_db_step1_card(state_dir),
        *post_analysis.build_placement_resolution_cards(state_dir),
        *post_analysis.build_api_connection_cards(state_dir),
        *post_analysis.build_improvement_cards(state_dir),
        post_analysis.build_cell_logging_card(state_dir),
        post_analysis.build_closure_card(state_dir, {}),
        post_analysis.build_db_connection_card(state_dir),
        plan_approval.build_plan_card(state_dir),
    ]
    return [c for c in cards if c is not None]


def test_every_choice_question_has_valid_recommended_default(synthetic_state) -> None:
    """El default recomendado siempre existe y es una opción real — el camino
    fácil ('no sé' / recomendado) nunca requiere escribir."""
    for card in _all_cards(synthetic_state):
        for q in card.questions:
            if q.options:
                assert q.recommended_default, f"{card.card_id}/{q.id} sin default recomendado"
                assert q.recommended_default in q.options or q.recommended_default == "todos", (
                    f"{card.card_id}/{q.id}: default '{q.recommended_default}' no es una opción"
                )


def test_transitions_are_micro_lines_not_recaps(synthetic_state) -> None:
    for card in _all_cards(synthetic_state):
        assert card.transition.count("\n") == 0, f"{card.card_id}: transición multilínea"
        assert len(card.transition) <= 90, f"{card.card_id}: transición demasiado larga (recap)"
