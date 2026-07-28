"""Eval set de traducción SAS→Python.

Dos modos, mismos asserts:

- **recorded** (siempre, CI sin API key): cada caso corre con un payload
  NodeTranslation curado en recorded/ — valida honestamente el harness, el
  contrato, los chequeos estáticos del ensamblador y las propiedades
  esperadas. No finge ser el modelo.
- **live** (solo con ANTHROPIC_API_KEY): mismos asserts contra AnthropicCaller
  con el prompt builder real. Requiere el extra `llm` instalado.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from sas_migrator.core.assembly.notebook import (
    assemble_notebooks,
    check_node_translation,
)
from sas_migrator.core.models.translation import NodeTranslation
from sas_migrator.llm import prompt_builder
from sas_migrator.llm.fake import FakeCaller

CASES_DIR = Path(__file__).parent / "cases"
RECORDED_DIR = Path(__file__).parent / "recorded"
CASES = sorted(CASES_DIR.glob("*.yaml"))


def _load_case(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _target(case: dict) -> dict:
    return {
        "node_id": case["id"],
        "node_label": case.get("node_label", case["id"]),
        "strategy": case["strategy"],
        "placement": case.get("placement"),
        # Lo que el nodo consume. El chequeo de nombres sin definir lo necesita:
        # un caso del eval es UN nodo aislado, así que sus entradas no las
        # define ninguna celda anterior — las declara el plan.
        "input_datasets": case.get("input_datasets", []),
        "notebook_path": "output/NB-01_eval.ipynb",
    }


def _assert_expectations(case: dict, nt: NodeTranslation, tmp_path: Path) -> None:
    exp = case.get("expect", {})
    failure = check_node_translation(nt)
    assert failure is None, f"chequeo estático falló: {failure}"
    assert len(nt.cells) >= int(exp.get("min_cells", 1))

    joined = "\n".join(nt.imports) + "\n" + "\n".join(nt.cells)
    for pattern in exp.get("cells_regex_all", []):
        assert re.search(pattern, joined), f"esperaba /{pattern}/ en la traducción"
    for bad in exp.get("forbid", []):
        assert bad not in joined, f"patrón prohibido presente: {bad}"
    if exp.get("imports_any"):
        assert any(i in nt.imports for i in exp["imports_any"]), exp["imports_any"]

    # El ensamblador la acepta end-to-end (notebook + mapping).
    target = _target(case)
    mapping, failures = assemble_notebooks(
        {"targets": [target]}, {nt.node_id: nt}, tmp_path / "output",
        db_bootstrap=case.get("placement") != "pandas",
    )
    assert failures == []
    assert mapping.mappings[0].node_id == case["id"]


def _call(caller, case: dict) -> NodeTranslation:
    user = prompt_builder.build_translation_user(_target(case), case["sas_code"], ["GOB"])
    return caller.call(
        task="translation",
        system_blocks=prompt_builder.build_translation_system(),
        user_content=user,
        output_model=NodeTranslation,
    )


@pytest.mark.parametrize("case_path", CASES, ids=lambda p: p.stem)
def test_recorded_translation_meets_expectations(case_path: Path, tmp_path: Path) -> None:
    case = _load_case(case_path)
    recorded = json.loads(
        (RECORDED_DIR / f"{case['id']}.json").read_text(encoding="utf-8")
    )
    caller = FakeCaller({"translation": NodeTranslation.model_validate(recorded)})
    nt = _call(caller, case)
    _assert_expectations(case, nt, tmp_path)


@pytest.mark.skipif(
    "ANTHROPIC_API_KEY" not in os.environ,
    reason="eval live requiere ANTHROPIC_API_KEY (y el extra llm instalado)",
)
@pytest.mark.parametrize("case_path", CASES, ids=lambda p: p.stem)
def test_live_translation_meets_expectations(case_path: Path, tmp_path: Path) -> None:
    from sas_migrator.core.config import LlmConfig
    from sas_migrator.llm.client import AnthropicCaller

    case = _load_case(case_path)
    caller = AnthropicCaller(LlmConfig())
    nt = _call(caller, case)
    # identidad defensiva, igual que el runner de fase 6
    nt = nt.model_copy(update={"node_id": case["id"]})
    _assert_expectations(case, nt, tmp_path)
