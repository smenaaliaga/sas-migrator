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


def test_cell_logging_card_recommendation_follows_config(tmp_path: Path) -> None:
    """B5b siempre se presenta; el config solo mueve la recomendación."""
    state = _state(tmp_path)

    card = post_analysis.build_cell_logging_card(state)  # sin config → False
    assert card.questions[0].recommended_default == "No, notebooks sin logging"
    assert card.questions[0].options == post_analysis.CELL_LOGGING_OPTIONS

    (tmp_path / "project_config.yaml").write_text(
        "translation:\n  cell_logging: true\n", encoding="utf-8"
    )
    card = post_analysis.build_cell_logging_card(state)
    assert card.questions[0].recommended_default == "Sí, agregar logging de resultados"
    assert card.questions[0].evidence == [
        "project_config.yaml: translation.cell_logging = true"
    ]


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


def _state_con_consultas(tmp_path: Path, n_queries: int = 2) -> Path:
    state = _state(
        tmp_path,
        nodes_index={
            "nodes": [
                *(
                    {
                        "id": f"Query-{i}",
                        "label": f"Query Builder {i}",
                        "node_type": "QUERY",
                        "pfd_label": "Salidas",
                        "requires_manual_review": True,
                        "query_preview": True,
                    }
                    for i in range(1, n_queries + 1)
                ),
                {
                    "id": "ImportTask-1",
                    "label": "Importar Excel",
                    "node_type": "IMPORT",
                    "native_task": True,
                },
            ]
        },
    )
    (state / "nodes").mkdir()
    for i in range(1, n_queries + 1):
        (state / "nodes" / f"Query-{i}.json").write_text(
            json.dumps(
                {
                    "code": "PROC SQL;\n  CREATE TABLE WORK.QUERY_FOR_X AS\n"
                    f"  SELECT DISTINCT t1.REGION FROM SRC.C{i} t1;\nQUIT;",
                    "metadata": {"code_from_last_run": True},
                }
            ),
            encoding="utf-8",
        )
    return state


def test_query_de_inspeccion_pregunta_con_el_sql_y_recomienda_excluir(
    tmp_path: Path,
) -> None:
    """Decidir sobre una consulta exige ver su SQL, no solo su ID."""
    state = _state_con_consultas(tmp_path, n_queries=1)

    cards = post_analysis.build_native_node_cards(state)
    consultas = next(c for c in cards if c.card_id == "B2-scope:queries")
    question = consultas.questions[0]
    assert question.id == "Q-B2-4-Query-1"
    assert question.recommended_default == "Excluir de la migración"
    assert "CREATE TABLE WORK.QUERY_FOR_X" in question.context
    assert any("ÚLTIMA ejecución" in e for e in question.evidence)

    # La tarea nativa sin código conserva su default opuesto: excluirla pierde
    # un paso que nadie puede reconstruir.
    nativa = next(c for c in cards if c.card_id.endswith("ImportTask-1"))
    assert nativa.questions[0].recommended_default == "Traducir a mano"


def test_las_consultas_de_inspeccion_van_todas_en_una_tarjeta(tmp_path: Path) -> None:
    """El Query Builder se explica una vez, no una vez por nodo."""
    state = _state_con_consultas(tmp_path, n_queries=4)

    cards = post_analysis.build_native_node_cards(state)
    consultas = [c for c in cards if c.card_id == "B2-scope:queries"]
    assert len(consultas) == 1, "las consultas no se preguntan de a una"
    card = consultas[0]
    assert [q.id for q in card.questions] == [f"Q-B2-4-Query-{i}" for i in range(1, 5)]
    # Cada pregunta trae SU propio SQL: agrupar no es fusionar.
    assert [q.context.count("SRC.C") for q in card.questions] == [1, 1, 1, 1]
    assert "Enterprise Guide" in card.transition, "el contexto compartido va en la tarjeta"
    assert "(4)" in card.title
    # La tarea nativa sigue teniendo tarjeta propia: pide una descripción.
    assert any(c.card_id == "B2-scope:native:ImportTask-1" for c in cards)
