"""Harness de integración contra un .egp REAL (Etapa 6).

No corre en CI normal: se activa con la variable de entorno SASMIG_REAL_EGP
apuntando al .egp productivo. Tres niveles, cada uno con más requisitos:

1. **Extracción** (solo el .egp): parser v2 sobre cada nodo real — nunca
   crashea, placement válido, contabilidad del residuo cierra; resumen al
   backlog JSON (SASMIG_REAL_EGP_REPORT o junto al .egp).
2. **Pipeline estructural** (solo el .egp): las 9 fases en stub_mode sobre la
   estructura real — gates, placement, plan, ensamblador y golden de
   determinismo aguantan un proyecto real, no solo el sintético.
3. **Traducción live** (+ ANTHROPIC_API_KEY): cada nodo real pasa por el LLM
   real; los fallos de chequeo estático van al backlog — el anti-drift dice
   que un desvío del LLM produce una verificación nueva, no más prompt.

Uso local:
    SASMIG_REAL_EGP=C:/ruta/proyecto.egp pytest tests/integration -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sas_migrator.core.extractors.egp import build_flow_summary, extract_egp
from sas_migrator.core.parser.placement import classify_placement
from sas_migrator.core.parser.statements import parse_sas_code

REAL_EGP = os.environ.get("SASMIG_REAL_EGP", "")

pytestmark = pytest.mark.skipif(
    not REAL_EGP, reason="harness .egp real: definir SASMIG_REAL_EGP"
)


def _egp_path() -> Path:
    path = Path(REAL_EGP)
    assert path.exists(), f"SASMIG_REAL_EGP no existe: {path}"
    return path


def _report_path() -> Path:
    custom = os.environ.get("SASMIG_REAL_EGP_REPORT", "")
    if custom:
        return Path(custom)
    return _egp_path().with_suffix(".backlog.json")


def _write_report(section: str, payload) -> Path:
    path = _report_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[section] = payload
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


# ── 1. Extracción e invariantes del parser ──────────────────────────────────

def test_extraction_and_parser_invariants(tmp_path: Path) -> None:
    graph = extract_egp(_egp_path(), tmp_path / "state")

    assert graph.nodes, "el .egp real no aportó ningún nodo"

    macro_dependent = []
    for node in graph.nodes:
        parse = parse_sas_code(node.code or "")  # nunca debe crashear (fuzz + real)
        decision = classify_placement(parse, db_librefs=set())
        assert decision.placement in {
            "sql_passthrough", "sql_pushdown", "pandas", "hybrid",
            "ambiguous", "utility",
        }
        if parse.macro_refs:
            macro_dependent.append({"node_id": node.id, "macro_refs": parse.macro_refs})

    summary = build_flow_summary(graph)
    residue = graph.metadata.get("extraction_residue_summary", {})
    report = _write_report("extraction", {
        "egp": str(_egp_path()),
        "nodes": len(graph.nodes),
        "flows": len(summary.flows),
        "residue": residue,
        "macro_dependent_nodes": macro_dependent,
    })
    assert residue.get("unexplained_count", 0) == 0, residue
    print(f"\nbacklog → {report}")


# ── 2. Pipeline estructural completo (stub_mode) ────────────────────────────

def test_structural_pipeline_stub_mode(tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from sas_migrator.graph.builder import build_graph, initial_state

    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = ws / "input" / "egp" / _egp_path().name
    egp.write_bytes(_egp_path().read_bytes())

    graph = build_graph(checkpointer=InMemorySaver())
    result = graph.invoke(
        initial_state(ws, egp, stub_mode=True),
        {"configurable": {"thread_id": "real-egp"}},
    )

    # La auditoría semántica del gate 6 puede bloquear legítimamente en stub:
    # las traducciones placeholder no satisfacen construcciones especiales del
    # .egp real (PROC HTTP → requests, etc.). Eso NO es un fallo estructural —
    # es trabajo para la corrida real con LLM; va al backlog.
    audit_block = []
    if result.get("done") is not True:
        last_gate = (result.get("gate_history") or [{}])[-1]
        errors = last_gate.get("errors", [])
        is_audit_only = last_gate.get("phase") == 6 and errors and all(
            "Semantic audit" in e or "High issue" in e for e in errors
        )
        assert is_audit_only, result.get("notes")
        audit_block = errors

    notebooks = sorted((ws / "output").glob("*.ipynb"))
    _write_report("structural_pipeline", {
        "done": result.get("done") is True,
        "audit_block_on_stub": audit_block,
        "notebooks": [nb.name for nb in notebooks],
        "needs_human": _needs_human_count(ws / "state"),
    })
    assert notebooks, "el pipeline estructural no ensambló ningún notebook"


def _needs_human_count(state_dir: Path) -> int:
    import yaml

    path = state_dir / "needs_human.yaml"
    if not path.exists():
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return len([i for i in data.get("items", []) if not i.get("resolved")])


# ── 3. Traducción live nodo a nodo (requiere ANTHROPIC_API_KEY) ─────────────

@pytest.mark.skipif(
    "ANTHROPIC_API_KEY" not in os.environ,
    reason="traducción live requiere ANTHROPIC_API_KEY",
)
def test_live_translation_backlog(tmp_path: Path) -> None:
    from sas_migrator.core.assembly.notebook import check_node_translation
    from sas_migrator.core.config import LlmConfig
    from sas_migrator.core.models.translation import NodeTranslation
    from sas_migrator.llm import prompt_builder
    from sas_migrator.llm.client import AnthropicCaller
    from sas_migrator.llm.errors import NeedsHuman

    graph = extract_egp(_egp_path(), tmp_path / "state")
    caller = AnthropicCaller(LlmConfig())

    backlog = []
    translated = 0
    for node in graph.nodes:
        if not (node.code or "").strip():
            continue
        target = {
            "node_id": node.id,
            "node_label": node.label,
            "strategy": "pandas",
            "notebook_path": "output/NB-01_live.ipynb",
        }
        user = prompt_builder.build_translation_user(target, node.code, [])
        try:
            nt = caller.call(
                task="translation",
                system_blocks=prompt_builder.build_translation_system(),
                user_content=user,
                output_model=NodeTranslation,
            )
        except NeedsHuman as exc:
            backlog.append({"node_id": node.id, "kind": "needs_human",
                            "reason": exc.reason, "detail": exc.detail[:300]})
            continue
        translated += 1
        failure = check_node_translation(nt.model_copy(update={"node_id": node.id}))
        if failure:
            backlog.append({"node_id": node.id, "kind": "static_check",
                            "reason": failure.reason, "detail": failure.detail})

    report = _write_report("live_translation", {
        "translated": translated,
        "backlog": backlog,
    })
    print(f"\nbacklog → {report}")
    # Los desvíos alimentan el backlog (anti-drift), no rompen el harness:
    # solo exigimos que el LLM haya podido traducir al menos un nodo.
    assert translated > 0
