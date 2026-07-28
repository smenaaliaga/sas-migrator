"""Runners LLM de fases 2/3/6 con FakeCaller: producen artefactos que pasan
los gates REALES; NeedsHuman queda registrado y el gate bloquea (nunca
silencio)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from sas_migrator.core.analysis import ledger
from sas_migrator.core.models.translation import NodeTranslation
from sas_migrator.core.utils.needs_human import unresolved
from sas_migrator.core.utils.schema_validation import check_gate
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.llm import phases, runtime
from sas_migrator.llm.contracts import FileMappingBatch
from sas_migrator.llm.errors import NeedsHuman
from sas_migrator.llm.fake import FakeCaller
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import _header
from sas_migrator.testing.fake_llm import fake_improvements as _improvements_fake
from sas_migrator.testing.fake_llm import fake_reviews as _reviews_fake
from sas_migrator.testing.fake_llm import fake_translation as _translation_fake


@pytest.fixture(autouse=True)
def _reset_caller():
    yield
    runtime.set_caller(None)


@pytest.fixture(scope="module")
def stub_ws(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("llm_base")
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    result = build_graph().invoke(initial_state(ws, egp))
    assert result["done"] is True
    return ws


@pytest.fixture()
def ws(stub_ws: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "ws"
    shutil.copytree(stub_ws, dst)
    return dst




# ── Fase 2 ──────────────────────────────────────────────────────────────────

def test_run_analysis_passes_real_gate2(ws: Path) -> None:
    state = ws / "state"
    # partir de cero: sin reviews previas, descripciones vacías, ledger pending
    shutil.rmtree(state / "analysis_reviews")
    (state / "analysis_progress.json").unlink()
    summary = json.loads((state / "flow_summary.json").read_text(encoding="utf-8"))
    for flow in summary.get("flows", []):
        flow["description"] = ""
    (state / "flow_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    ledger.cmd_init(state)
    passed, _ = check_gate(2, state)
    assert not passed, "sin reviews el gate 2 debe estar rojo"

    runtime.set_caller(FakeCaller({
        "analysis_reviews": _reviews_fake, "improvements": _improvements_fake,
    }))
    counts = phases.run_analysis(state, ws)
    ledger.cmd_sync(state)

    assert counts["pfds_ok"] == counts["pfds_total"]
    passed, errors = check_gate(2, state)
    assert passed, errors
    summary = json.loads((state / "flow_summary.json").read_text(encoding="utf-8"))
    assert any("ventas regionales" in str(f.get("description")) for f in summary["flows"])
    proposed = yaml.safe_load(
        (state / "improvements_proposed.yaml").read_text(encoding="utf-8")
    )
    assert proposed["improvements"][0]["status"] == "proposed"


def test_run_analysis_needs_human_blocks_gate2(ws: Path) -> None:
    state = ws / "state"
    (state / "analysis_progress.json").unlink()
    shutil.rmtree(state / "analysis_reviews")
    ledger.cmd_init(state)

    runtime.set_caller(FakeCaller({
        "analysis_reviews": NeedsHuman(
            task="analysis_reviews", reason="validation_retries_exhausted", attempts=3
        ),
        "improvements": _improvements_fake,
    }))
    phases.run_analysis(state, ws)
    ledger.cmd_sync(state)

    items = unresolved(state, phase=2)
    assert items and items[0].task == "analysis_reviews"
    passed, errors = check_gate(2, state)
    assert not passed
    assert any("needs_human" in e for e in errors)


# ── Fase 3 ──────────────────────────────────────────────────────────────────

def _with_profile(state: Path) -> None:
    (state / "profile_report.json").write_text(
        json.dumps([{"file_path": "input/data/clientes.csv", "file_type": "csv",
                     "row_count": 10, "column_count": 2}]),
        encoding="utf-8",
    )


def test_run_matching_with_llm(ws: Path) -> None:
    state = ws / "state"
    _with_profile(state)
    runtime.set_caller(FakeCaller({
        "matching": FileMappingBatch(mappings=[{
            "file_path": "input/data/clientes.csv", "node_id": "Query-1",
            "role": "input", "confidence": 0.9,
            "reasons": ["nombre coincide con src.clientes"],
            "needs_confirmation": False,
        }]),
    }))
    counts = phases.run_matching(state, ws)
    assert counts == {"mappings": 1, "llm": True}
    doc = json.loads((state / "file_mapping.json").read_text(encoding="utf-8"))
    assert doc["mappings"][0]["node_id"] == "Query-1"
    passed, errors = check_gate(3, state)
    assert passed, errors


def test_run_matching_needs_human_fallback(ws: Path) -> None:
    state = ws / "state"
    _with_profile(state)
    runtime.set_caller(FakeCaller({
        "matching": NeedsHuman(task="matching", reason="refusal", attempts=1),
    }))
    phases.run_matching(state, ws)

    doc = json.loads((state / "file_mapping.json").read_text(encoding="utf-8"))
    assert doc["mappings"][0]["needs_confirmation"] is True
    passed, errors = check_gate(3, state)
    assert not passed and any("needs_human" in e for e in errors)


def test_run_matching_without_profiles_skips_llm(ws: Path) -> None:
    state = ws / "state"
    runtime.set_caller(FakeCaller({}))  # cualquier llamada explotaría con KeyError
    counts = phases.run_matching(state, ws)
    assert counts == {"mappings": 0, "llm": False}


# ── Fase 6 ──────────────────────────────────────────────────────────────────

def test_run_translation_passes_real_gate6(ws: Path) -> None:
    state = ws / "state"
    # partir de cero: sin traducciones stub previas (run_translation ahora
    # retoma desde lo que ya exista en disco, no debe confundirlas con
    # traducciones reales).
    shutil.rmtree(state / "translations", ignore_errors=True)
    runtime.set_caller(FakeCaller({"translation": _translation_fake}))
    counts = phases.run_translation(state, ws / "output", ws)

    assert counts["assembly_failures"] == 0
    assert counts["translated"] == counts["targets"]
    from sas_migrator.core.gen_run_all import write_run_all

    write_run_all(ws / "output")
    passed, errors = check_gate(6, state)
    assert passed, errors
    mapping = json.loads((state / "sas_python_mapping.json").read_text(encoding="utf-8"))
    assert all(m["confidence"] == "medium" for m in mapping["mappings"])


def test_run_translation_failed_node_is_needs_human_and_gate_blocks(ws: Path) -> None:
    state = ws / "state"
    shutil.rmtree(state / "translations", ignore_errors=True)

    def flaky(user: str) -> NodeTranslation:
        head = _header(user)
        if head["node_id"] == "CodeTask-1":
            raise NeedsHuman(task="translation", reason="validation_retries_exhausted",
                             attempts=3)
        return _translation_fake(user)

    runtime.set_caller(FakeCaller({"translation": flaky}))
    counts = phases.run_translation(state, ws / "output", ws)
    assert counts["translated"] == counts["targets"] - 1

    items = unresolved(state, phase=6)
    assert [i.node_id for i in items] == ["CodeTask-1"]
    passed, errors = check_gate(6, state)
    assert not passed
    assert any("needs_human" in e for e in errors)
    assert any("without SAS->Python mapping" in e for e in errors), (
        "la auditoría también reporta el nodo sin mapping (doble señal)"
    )


def test_run_translation_static_failure_is_recorded(ws: Path) -> None:
    """Un nodo que nunca pasa el chequeo agota los reintentos y va a la cola —
    y NO queda persistido en state/translations/ como si estuviera hecho."""
    state = ws / "state"
    shutil.rmtree(state / "translations", ignore_errors=True)

    def bad_code(user: str) -> NodeTranslation:
        nt = _translation_fake(user)
        if _header(user)["node_id"] == "CodeTask-2":
            return nt.model_copy(update={"cells": ["df.to_parquet('x')\n"]})
        return nt

    caller = FakeCaller({"translation": bad_code})
    runtime.set_caller(caller)
    counts = phases.run_translation(state, ws / "output", ws)

    assert counts["translated"] == counts["targets"] - 1
    assert counts["assembly_failures"] == 0, (
        "el fallo estático se ataja al traducir, no al ensamblar"
    )
    items = unresolved(state, phase=6)
    assert [i.node_id for i in items] == ["CodeTask-2"]
    assert items[0].task == "translation" and items[0].reason == "static_check_failed"
    assert "to_parquet" in items[0].detail, "el motivo exacto queda en la cola"
    assert items[0].attempts == phases.MAX_TRANSLATION_RETRIES + 1

    assert not (state / "translations" / "CodeTask-2.json").exists(), (
        "una traducción rechazada no se persiste: si se persistiera, el "
        "retomar-desde-disco la daría por hecha y no se reintentaría nunca"
    )
    malas = [
        c for c in caller.calls
        if c["task"] == "translation" and '"node_id": "CodeTask-2"' in c["user_content"]
    ]
    assert len(malas) == phases.MAX_TRANSLATION_RETRIES + 1
    assert "to_parquet" in malas[-1]["user_content"], (
        "el reintento le dice al modelo por qué se lo rechazó"
    )


def test_run_translation_retries_until_the_node_is_clean(ws: Path) -> None:
    """El reintento no es ceremonia: si el segundo intento sale bien, el nodo
    entra al notebook y nadie queda en la cola."""
    state = ws / "state"
    shutil.rmtree(state / "translations", ignore_errors=True)
    intentos: dict[str, int] = {}

    def flaky_once(user: str) -> NodeTranslation:
        nt = _translation_fake(user)
        nid = _header(user)["node_id"]
        intentos[nid] = intentos.get(nid, 0) + 1
        if nid == "CodeTask-2" and intentos[nid] == 1:
            return nt.model_copy(update={"cells": ["x = x\n"]})  # self_assignment
        return nt

    runtime.set_caller(FakeCaller({"translation": flaky_once}))
    counts = phases.run_translation(state, ws / "output", ws)

    assert counts["translated"] == counts["targets"]
    assert counts["assembly_failures"] == 0
    assert unresolved(state, phase=6) == []
    assert intentos["CodeTask-2"] == 2


def test_stale_bad_translation_on_disk_is_retranslated(ws: Path) -> None:
    """Una traducción inválida de una corrida vieja no se hereda como hecha."""
    state = ws / "state"
    trans_dir = state / "translations"
    shutil.rmtree(trans_dir, ignore_errors=True)
    trans_dir.mkdir(parents=True)
    (trans_dir / "CodeTask-2.json").write_text(
        NodeTranslation(node_id="CodeTask-2", node_label="viejo", cells=[]).model_dump_json(),
        encoding="utf-8",
    )

    runtime.set_caller(FakeCaller({"translation": _translation_fake}))
    counts = phases.run_translation(state, ws / "output", ws)

    assert counts["translated"] == counts["targets"]
    assert unresolved(state, phase=6) == []
    regenerada = json.loads((trans_dir / "CodeTask-2.json").read_text(encoding="utf-8"))
    assert regenerada["cells"], "el .json vacío se reemplazó por una traducción real"


def test_corrupt_translation_json_is_retranslated(ws: Path, capsys) -> None:
    """Un .json ilegible en translations/ (kill a mitad de escritura, edición a
    mano) no crashea la fase: se saltea con aviso y el nodo se re-traduce."""
    state = ws / "state"
    trans_dir = state / "translations"
    shutil.rmtree(trans_dir, ignore_errors=True)
    trans_dir.mkdir(parents=True)
    (trans_dir / "CodeTask-2.json").write_text(
        '{"node_id": "CodeTask-2", "cells": [', encoding="utf-8"
    )

    runtime.set_caller(FakeCaller({"translation": _translation_fake}))
    counts = phases.run_translation(state, ws / "output", ws)

    assert counts["translated"] == counts["targets"]
    assert unresolved(state, phase=6) == []
    regenerada = json.loads((trans_dir / "CodeTask-2.json").read_text(encoding="utf-8"))
    assert regenerada["cells"], "el .json corrupto se reemplazó por una traducción real"
    assert "se re-traduce" in capsys.readouterr().err


# ── Pipeline completo con LLM fake + entrevistas ────────────────────────────

def test_full_pipeline_llm_fake_and_interviews(tmp_path: Path) -> None:
    """DoD Etapa 4: sobre el .egp sintético, con caller fake y entrevistas
    reales, el pipeline completa las 9 fases y los notebooks pasan gate 6."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")

    from sas_migrator.testing.fake_llm import default_fake_caller

    runtime.set_caller(default_fake_caller())

    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "llm-e2e"}}
    result = graph.invoke(initial_state(ws, egp, stub_mode=False), config)
    steps = 0
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        answers = []
        for q in payload["questions"]:
            if q["question_type"] == "multi_choice":
                value = "todos"
            elif q["options"]:
                value = q.get("recommended_default") or q["options"][0]
            else:
                value = q.get("recommended_default") or "respuesta sintética"
            answers.append({"question_id": q["id"], "value": value})
        result = graph.invoke(
            Command(resume={"card_id": payload["card_id"], "answers": answers,
                            "free_text": ""}),
            config,
        )
        steps += 1
        assert steps < 40

    assert result["done"] is True
    gates = [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert gates == [(p, True) for p in range(9)]
    mapping = json.loads(
        (ws / "state" / "sas_python_mapping.json").read_text(encoding="utf-8")
    )
    assert {m["confidence"] for m in mapping["mappings"]} == {"medium"}, (
        "la traducción vino del caller fake, no del stub"
    )


# ── semillas M-xxx (fichas que el LLM no puede derivar de la evidencia) ──────

def test_seeded_improvements_absent_file_is_inert(tmp_path):
    from sas_migrator.llm.phases import _seeded_improvements

    assert _seeded_improvements(tmp_path) == []


def test_seeded_improvement_is_validated_and_forced_to_proposed(tmp_path):
    from sas_migrator.llm.phases import _seeded_improvements

    (tmp_path / "improvements_seed.yaml").write_text(
        "improvements:\n"
        "  - id: M-901\n"
        "    category: modernization\n"
        "    title: Usar el SDK oficial\n"
        "    description: d\n"
        "    justification: j\n"
        "    impact: medium\n"
        "    effort: low\n"
        "    risk: medium\n"
        "    recommendation: Rechazar por ahora\n"
        "    status: approved\n"          # sembrar no aprueba
        "    affected_nodes: [CT-1]\n",
        encoding="utf-8",
    )
    seeded = _seeded_improvements(tmp_path)
    assert [i["id"] for i in seeded] == ["M-901"]
    assert seeded[0]["status"] == "proposed"


def test_seeded_improvement_with_bad_category_raises(tmp_path):
    import pytest
    from pydantic import ValidationError

    from sas_migrator.llm.phases import _seeded_improvements

    (tmp_path / "improvements_seed.yaml").write_text(
        "improvements:\n  - id: M-902\n    category: inventada\n    title: t\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        _seeded_improvements(tmp_path)


# ── Nodos grandes: truncar en silencio es peor que fallar ───────────────────

def _state_con_nodo(tmp_path: Path, code: str) -> Path:
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    (state / "nodes" / "N-1.json").write_text(
        json.dumps({"code": code}), encoding="utf-8"
    )
    return state


def test_node_code_no_trunca_nunca(tmp_path: Path) -> None:
    """El recorte lo decide quien arma el prompt, no el lector del artefacto."""
    from sas_migrator.llm import phases

    state = _state_con_nodo(tmp_path, "x" * 500_000)
    assert len(phases._node_code(state, "N-1")) == 500_000


def test_extracto_de_analisis_declara_lo_que_omite(tmp_path: Path) -> None:
    """Un modelo que no sabe que ve un extracto cree que el nodo termina ahí."""
    from sas_migrator.llm import phases

    state = _state_con_nodo(tmp_path, "y" * 1000)
    extracto = phases._node_excerpt(state, "N-1", 400)
    assert "EXTRACTO" in extracto and "600" in extracto and "1000" in extracto
    # Bajo el límite no se anuncia nada.
    assert "EXTRACTO" not in phases._node_excerpt(state, "N-1", 5000)


def test_split_corta_entre_bloques_nunca_dentro_de_uno() -> None:
    from sas_migrator.llm.phases import split_sas_blocks

    code = (
        "PROC SQL;\n  CREATE TABLE a AS SELECT * FROM x;\nQUIT;\n"
        "DATA b;\n  SET a;\nRUN;\n"
        "PROC SORT DATA=b;\n  BY z;\nRUN;\n"
    )
    trozos = split_sas_blocks(code, 60)
    assert len(trozos) > 1
    assert "".join(trozos) == code, "no se pierde ni se duplica una línea"
    for t in trozos:
        assert t.lstrip().upper().startswith(("PROC", "DATA")), t


def test_bloque_indivisible_mas_grande_que_el_techo_no_se_parte() -> None:
    """Sin corte honesto, el nodo va a needs_human — no se traduce a medias."""
    from sas_migrator.llm.phases import split_sas_blocks

    gigante = "PROC SQL;\n" + "  SELECT 1;\n" * 5000 + "QUIT;\n"
    assert split_sas_blocks(gigante, 1000) == []


def test_nodo_sin_corte_posible_bloquea_el_gate() -> None:
    from sas_migrator.llm import phases
    from sas_migrator.llm.errors import NeedsHuman

    gigante = "PROC SQL;\n" + "  SELECT 1;\n" * 200_000 + "QUIT;\n"
    with pytest.raises(NeedsHuman) as exc:
        phases._translate_node(
            None, system=[], target={"node_id": "N-1"}, code=gigante, db_aliases=[]
        )
    assert exc.value.reason == "node_code_too_large"


def test_merge_conserva_orden_dedupea_imports_y_baja_la_confianza() -> None:
    from sas_migrator.core.models.translation import Confidence, NodeTranslation
    from sas_migrator.llm.phases import merge_translations

    p1 = NodeTranslation(node_id="N", imports=["import pandas as pd"], cells=["a = 1\n"],
                         confidence=Confidence.HIGH, warnings=["ojo con a"])
    p2 = NodeTranslation(node_id="N", imports=["import pandas as pd", "import numpy as np"],
                         cells=["b = a + 1\n"], confidence=Confidence.LOW)
    merged = merge_translations([p1, p2])

    assert merged.cells == ["a = 1\n", "b = a + 1\n"], "orden de ejecución del SAS"
    assert merged.imports == ["import pandas as pd", "import numpy as np"]
    assert merged.confidence == Confidence.LOW, "no más confiable que su peor tramo"
    assert any("NODO PARTIDO" in w for w in merged.warnings)
    assert any("[parte 1/2] ojo con a" == w for w in merged.warnings)
