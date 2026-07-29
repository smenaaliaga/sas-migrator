"""B4c — conexiones externas (APIs HTTP): evidencia, tarjeta, apply, loader,
gate 4 y contexto del prompt. Cada regla con su falso positivo curado."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sas_migrator.core.api.connections import (
    load_api_connections,
    sdk_by_host,
    sdk_packages,
)
from sas_migrator.core.http_evidence import (
    build_http_evidence,
    extract_http_calls,
    extract_http_hosts,
)
from sas_migrator.core.interview import apply, post_analysis
from sas_migrator.core.models.data import ApiConnection, ApiConnectionMode
from sas_migrator.core.models.interview import Answer, CardAnswers
from sas_migrator.core.utils.schema_validation import _phase4_api_errors
from sas_migrator.llm import prompt_builder

_SAS_BDE = (
    'filename resp temp;\n'
    'proc http url="https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx?f=GetSeries"\n'
    '  method="get" out=resp;\nrun;'
)


# ── Evidencia (fase 2) ───────────────────────────────────────────────────────

def test_extract_http_calls_reads_host_method_and_source() -> None:
    parsed = extract_http_calls(_SAS_BDE)
    assert parsed["calls"][0]["host"] == "si3.bcentral.cl"
    assert parsed["methods"] == ["GET"]
    assert parsed["sources"] == ["proc_http"]
    assert parsed["has_dynamic_url"] is False


def test_macro_url_is_declared_not_inferred() -> None:
    """FP curado: URL por macro no aporta host — queda declarada, jamás adivinada."""
    parsed = extract_http_calls('proc http url="&base./ws?serie=X" method="get"; run;')
    assert parsed["calls"] == []
    assert parsed["has_dynamic_url"] is True
    assert extract_http_hosts('proc http url="&base./ws";') == []


def test_build_http_evidence_is_sorted_and_capped() -> None:
    nodes = [
        {"id": "CT-2", "code": 'proc http url="https://b.cl/x" method="get"; run;'},
        {"id": "CT-1", "code": _SAS_BDE},
        {"id": "CT-3", "code": 'proc http url="&base./x"; run;'},
    ]
    ev = build_http_evidence(nodes, generated_at="2026-01-01T00:00:00+00:00")
    assert [h["host"] for h in ev["hosts"]] == ["b.cl", "si3.bcentral.cl"]
    assert ev["hosts"][1]["node_ids"] == ["CT-1"]
    assert ev["nodes_with_dynamic_url"] == ["CT-3"]


def test_audit_reexports_extract_http_hosts() -> None:
    import sas_migrator.core.audit as audit

    assert audit.extract_http_hosts is extract_http_hosts


# ── Tarjeta B4c ──────────────────────────────────────────────────────────────

def _state_with_evidence(tmp_path: Path, hosts: list[dict]) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "http_evidence.json").write_text(
        json.dumps({
            "generated_at": "2026-01-01T00:00:00+00:00",
            "hosts": hosts,
            "nodes_with_dynamic_url": [],
            "detection_notes": [],
        }),
        encoding="utf-8",
    )
    return state


_HOST_BDE = {
    "host": "si3.bcentral.cl",
    "node_ids": ["CT-1"],
    "methods": ["GET"],
    "sample_urls": ["https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx"],
    "sources": ["proc_http"],
}


def test_no_evidence_no_cards(tmp_path) -> None:
    """UX lean: sin http_evidence.json (o sin hosts) no se pregunta nada."""
    state = tmp_path / "state"
    state.mkdir()
    assert post_analysis.build_api_connection_cards(state) == []
    assert post_analysis.build_api_connection_cards(_state_with_evidence(tmp_path, [])) == []


def test_one_card_per_host_with_recommended_default(tmp_path) -> None:
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    cards = post_analysis.build_api_connection_cards(state)
    assert [c.card_id for c in cards] == ["B4c:api:si3.bcentral.cl"]
    q = cards[0].questions[0]
    assert q.options == post_analysis.API_MODE_OPTIONS
    assert q.recommended_default == "Replicar la llamada HTTP con requests"
    assert any("si3.bcentral.cl" in e for e in q.evidence)


# ── Apply → api_connections.yaml ─────────────────────────────────────────────

def _collected_for(state: Path, choice: str, free_text: str = "") -> apply.Collected:
    card = post_analysis.build_api_connection_cards(state)[0]
    ans = CardAnswers(
        card_id=card.card_id,
        answers=[Answer(question_id=card.questions[0].id, value=choice)],
        free_text=free_text,
    )
    return [(card, ans)]


def test_apply_writes_http_mode(tmp_path) -> None:
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    apply._write_api_connections(
        state, _collected_for(state, "Replicar la llamada HTTP con requests")
    )
    conns = load_api_connections(state)
    assert conns[0]["host"] == "si3.bcentral.cl"
    assert conns[0]["mode"] == "http"
    assert conns[0]["node_ids"] == ["CT-1"]
    assert sdk_packages(state) == []


def test_apply_writes_sdk_mode_with_package_from_free_text(tmp_path) -> None:
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    apply._write_api_connections(
        state,
        _collected_for(
            state,
            "Usar una librería/SDK oficial (indico el paquete en texto libre)",
            free_text="bcchapi (la librería oficial del BC)",
        ),
    )
    conns = load_api_connections(state)
    assert conns[0]["mode"] == "sdk"
    assert conns[0]["package"] == "bcchapi"
    assert sdk_packages(state) == ["bcchapi"]
    assert sdk_by_host(state) == {"si3.bcentral.cl": "bcchapi"}


def test_apply_sdk_without_package_raises(tmp_path) -> None:
    """El modelo Pydantic es la última línea: mode=sdk sin package no se escribe."""
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    with pytest.raises(Exception, match="package"):
        apply._write_api_connections(
            state,
            _collected_for(
                state,
                "Usar una librería/SDK oficial (indico el paquete en texto libre)",
                free_text="",
            ),
        )


def test_ask_api_block_package_only_retry_keeps_sdk_choice(tmp_path, monkeypatch) -> None:
    """Tras el aviso «indica el paquete», responder solo «bcchapi» completa la
    elección SDK en vez de degradarla silenciosamente a mode=http."""
    from sas_migrator.graph import interviews

    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    sdk = "Usar una librería/SDK oficial (indico el paquete en texto libre)"
    presented: list = []
    replies = iter([
        lambda card: CardAnswers(
            card_id=card.card_id,
            answers=[Answer(question_id=card.questions[0].id, value=sdk)],
        ),
        lambda card: CardAnswers(card_id=card.card_id, free_text="bcchapi"),
    ])

    def fake_ask(card):
        presented.append(card)
        return next(replies)(card)

    monkeypatch.setattr(interviews, "ask", fake_ask)
    collected: interviews.Collected = []
    interviews._ask_api_block(state, collected)

    assert presented[1].validation_error, "la 2ª presentación lleva el aviso"
    ((card, ans),) = collected
    assert interviews._value_of(ans, card.questions[0].id) == sdk
    assert ans.free_text == "bcchapi"
    apply._write_api_connections(state, collected)
    assert sdk_by_host(state) == {"si3.bcentral.cl": "bcchapi"}


def test_ask_api_block_free_text_first_answer_stays_http(tmp_path, monkeypatch) -> None:
    """Sin aviso previo, texto libre puro sigue siendo contrapropuesta:
    no se infiere la elección SDK que el usuario nunca hizo."""
    from sas_migrator.graph import interviews

    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    monkeypatch.setattr(
        interviews,
        "ask",
        lambda card: CardAnswers(card_id=card.card_id, free_text="bcchapi"),
    )
    collected: interviews.Collected = []
    interviews._ask_api_block(state, collected)
    ((card, ans),) = collected
    assert interviews._value_of(ans, card.questions[0].id) == ""
    apply._write_api_connections(state, collected)
    conns = load_api_connections(state)
    assert conns[0]["mode"] == "http"


def test_apply_without_cards_writes_nothing(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    apply._write_api_connections(state, [])
    assert not (state / "api_connections.yaml").exists()


def test_parse_sdk_package() -> None:
    assert apply.parse_sdk_package("bcchapi") == "bcchapi"
    assert apply.parse_sdk_package("  usar bcchapi por favor") == "usar"  # primer token
    assert apply.parse_sdk_package("") == ""
    assert apply.parse_sdk_package("123") == ""


def test_api_connection_model_validates_sdk() -> None:
    with pytest.raises(ValueError, match="nombre"):
        ApiConnection(host="x.cl", mode=ApiConnectionMode.SDK, package="no válido!")
    ok = ApiConnection(host="x.cl", mode=ApiConnectionMode.SDK, package="bcchapi")
    assert ok.package == "bcchapi"


# ── Gate 4 ───────────────────────────────────────────────────────────────────

def test_gate4_without_evidence_demands_nothing(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    assert _phase4_api_errors(state) == []


def test_gate4_host_without_decision_blocks(tmp_path) -> None:
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    errors = _phase4_api_errors(state)
    assert len(errors) == 1
    assert "si3.bcentral.cl" in errors[0]


def test_gate4_sdk_without_package_blocks(tmp_path) -> None:
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    (state / "api_connections.yaml").write_text(
        "connections:\n  - host: si3.bcentral.cl\n    mode: sdk\n", encoding="utf-8"
    )
    errors = _phase4_api_errors(state)
    assert any("package" in e for e in errors)


def test_gate4_valid_decisions_pass(tmp_path) -> None:
    state = _state_with_evidence(tmp_path, [_HOST_BDE])
    apply._write_api_connections(
        state, _collected_for(state, "Replicar la llamada HTTP con requests")
    )
    assert _phase4_api_errors(state) == []


# ── Contexto del prompt ──────────────────────────────────────────────────────

def test_project_context_includes_api_section() -> None:
    ctx = prompt_builder.build_project_context(
        connections=[],
        improvements=[],
        macro_param_values={},
        api_connections=[
            {"host": "si3.bcentral.cl", "mode": "sdk", "package": "bcchapi"},
            {"host": "otra.api.cl", "mode": "http"},
        ],
    )
    assert "## Conexiones externas (APIs HTTP)" in ctx
    assert "`bcchapi`" in ctx
    assert "NO repliques la llamada HTTP cruda" in ctx
    assert "replica la llamada con requests al MISMO host" in ctx


def test_project_context_without_api_connections_unchanged() -> None:
    assert prompt_builder.build_project_context(
        connections=[], improvements=[], macro_param_values={}, api_connections=[]
    ) is None


# ── El SDK entra a la allowlist de la traducción ─────────────────────────────

def test_sdk_package_joins_allowed_imports(tmp_path) -> None:
    from sas_migrator.llm.phases import _translation_context

    ws = tmp_path / "ws"
    state = ws / "state"
    state.mkdir(parents=True)
    (state / "api_connections.yaml").write_text(
        yaml.safe_dump({"connections": [
            {"host": "si3.bcentral.cl", "mode": "sdk", "package": "bcchapi",
             "node_ids": [], "notes": ""}
        ]}),
        encoding="utf-8",
    )
    setup = _translation_context(state, {})
    assert "bcchapi" in setup.allowed
    assert "pandas" in setup.allowed
