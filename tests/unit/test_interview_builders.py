"""Lógica condicional de los builders de entrevista (core/interview)."""

from __future__ import annotations

import json
from pathlib import Path

from sas_migrator.core.interview import post_analysis


def _state(tmp_path: Path, **artifacts) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        (state / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    return state


def test_confirmed_prefix_does_not_generate_resolution_card(tmp_path: Path) -> None:
    """Un libref confirmado por LIBNAME está resuelto — no se pregunta."""
    state = _state(
        tmp_path,
        db_evidence={
            "librefs": [
                {
                    "libref": "GG",
                    "source": "libname_statement",
                    "engine_hint": "ODBC",
                    "table_count": 2,
                    "node_count": 2,
                    "tables": [
                        {"table": "IN", "access": "read", "node_ids": ["CodeTask-1"]},
                        {"table": "OUT", "access": "write", "node_ids": ["CodeTask-1"]},
                    ],
                }
            ],
            "unverified_prefixes": [],
            "connect_to_statements": [],
        },
        nodes_index={"nodes": []},
    )
    assert post_analysis.build_placement_resolution_cards(state) == []
    # pero el bloque B4b sí abre (hay BD involucrada)
    assert post_analysis.build_db_step1_card(state) is not None


def test_unverified_prefix_generates_one_card_per_cause(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        db_evidence={
            "librefs": [],
            "unverified_prefixes": [
                {"prefix": "SRC", "node_ids": ["CodeTask-1", "Query-1"], "tables": ["V"]},
                {"prefix": "XX", "node_ids": ["CodeTask-2"], "tables": ["T"]},
            ],
            "connect_to_statements": [],
        },
        nodes_index={
            "nodes": [
                {
                    "id": "CodeTask-1",
                    "placement": "ambiguous",
                    "placement_reasons": ["librefs sin confirmar como BD o ruta: ['SRC']"],
                }
            ]
        },
    )
    cards = post_analysis.build_placement_resolution_cards(state)
    assert [c.card_id for c in cards] == ["B4b:resolve:SRC", "B4b:resolve:XX"]
    src = cards[0]
    assert any("CodeTask-1" in line for q in src.questions for line in q.evidence), (
        "la tarjeta debe nombrar los nodos afectados como evidencia"
    )


def test_no_db_evidence_no_b4b_block(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        db_evidence={"librefs": [], "unverified_prefixes": [], "connect_to_statements": []},
    )
    assert post_analysis.build_db_step1_card(state) is None
    assert post_analysis.build_placement_resolution_cards(state) == []


def test_no_migratable_flows_no_scope_card(tmp_path: Path) -> None:
    state = _state(tmp_path, flow_summary={"flows": []})
    assert post_analysis.build_scope_flows_card(state) is None


def test_exclusion_confirm_only_when_something_excluded(tmp_path: Path) -> None:
    state = _state(tmp_path, flow_summary={"flows": []})
    assert post_analysis.build_scope_exclusion_confirm_card(state, []) is None


def test_low_confidence_mapping_becomes_pending_question(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        file_mapping={
            "mappings": [
                {"file_path": "a.xlsx", "node_id": None, "confidence": 0.0},
                {"file_path": "b.csv", "node_id": "CodeTask-1", "confidence": 0.9},
            ]
        },
    )
    card = post_analysis.build_mapping_card(state)
    assert card is not None
    ids = [q.id for q in card.questions]
    assert "Q-B1M-1" in ids and "Q-B1M-2" in ids
    low_q = next(q for q in card.questions if q.id == "Q-B1M-2")
    assert low_q.required is False, "los matches de baja confianza no bloquean"
