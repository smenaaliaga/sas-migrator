"""planning: la strategy de cada target sale del placement efectivo
(clasificador + overrides de la entrevista B4b), no de un valor fijo."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from sas_migrator.core import planning


def test_strategy_map_covers_all_placements() -> None:
    from sas_migrator.core.parser.placement import PLACEMENTS

    for placement in PLACEMENTS:
        assert placement in planning.STRATEGY_BY_PLACEMENT, placement
    assert planning.choose_strategy("utility") == "python"
    assert planning.choose_strategy("sql_pushdown") == "sql_pushdown"
    assert planning.choose_strategy(None) == "pandas"
    assert planning.choose_strategy("ambiguous") == "pandas"


def _state(tmp_path: Path, *, placements: dict[str, str],
           decisions: list[dict] | None = None) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    node_ids = sorted(placements)
    (state / "migration_state.json").write_text(
        json.dumps({"project_name": "demo", "egp_file": "demo.egp",
                    "output_strategy": "notebook-flow"}),
        encoding="utf-8",
    )
    (state / "flow_summary.json").write_text(
        json.dumps({
            "flows": [{
                "pfd_id": "PFD-1", "pfd_label": "Flujo", "migratable_candidate": True,
                "node_count": len(node_ids), "node_ids": node_ids,
                "notebook_slug": "flujo",
            }]
        }),
        encoding="utf-8",
    )
    (state / "nodes_index.json").write_text(
        json.dumps({
            "nodes": [
                {"id": nid, "label": nid, "node_type": "PROC_SQL",
                 "topo_order": i, "placement": placements[nid]}
                for i, nid in enumerate(node_ids)
            ]
        }),
        encoding="utf-8",
    )
    if decisions is not None:
        (state / "placement_decisions.yaml").write_text(
            yaml.safe_dump({"decisions": decisions}), encoding="utf-8"
        )
    return state


def test_targets_get_strategy_from_placement(tmp_path: Path) -> None:
    state = _state(tmp_path, placements={
        "A": "sql_pushdown", "B": "pandas", "C": "utility", "D": "ambiguous",
    })
    plan = planning.build(state)
    by_id = {t["node_id"]: t for t in plan["targets"]}

    assert by_id["A"]["strategy"] == "sql_pushdown"
    assert by_id["B"]["strategy"] == "pandas"
    assert by_id["C"]["strategy"] == "python" and by_id["C"]["placement"] == "utility"
    assert by_id["D"]["strategy"] == "pandas"
    # ambiguous sin resolver → supuesto VISIBLE en el plan
    assert any("ambiguous sin resolver en D" in a for a in plan["assumptions"])


def test_b4b_override_wins_over_classifier(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        placements={"A": "ambiguous"},
        decisions=[{"node_id": "A", "placement": "hybrid", "reason": "SRC = BD"}],
    )
    plan = planning.build(state)
    target = plan["targets"][0]
    assert target["placement"] == "hybrid"
    assert target["strategy"] == "hybrid"
    assert not any("ambiguous sin resolver" in a for a in plan["assumptions"])


def test_notebook_paths_are_workspace_relative(tmp_path: Path) -> None:
    state = _state(tmp_path, placements={"A": "pandas"})
    plan = planning.build(state)
    assert plan["targets"][0]["notebook_path"] == "output/NB-01_flujo.ipynb"


def test_datasets_se_pueblan_desde_los_node_files(tmp_path: Path) -> None:
    """La info existía en state/nodes/*.json y el plan la dejaba [] — el
    traductor trabajaba a ciegas y _declared_input_names comparaba contra vacío."""
    state = _state(tmp_path, placements={"A": "pandas"})
    (state / "nodes").mkdir()
    (state / "nodes" / "A.json").write_text(
        json.dumps({
            "id": "A",
            "inputs": ["WORK.BASE", "GG.CLIENTES"],
            "outputs": ["WORK.RESULT", "GG.SALIDA"],
        }),
        encoding="utf-8",
    )
    plan = planning.build(state)
    target = plan["targets"][0]
    assert target["input_datasets"] == ["GG.CLIENTES", "WORK.BASE"]
    assert target["output_datasets"] == ["GG.SALIDA", "WORK.RESULT"]
    assert target["output_tables"] == ["GG.SALIDA"]  # solo no-WORK calificadas


def test_el_modelo_no_borra_claves_del_plan_construido(tmp_path: Path) -> None:
    """Anti-deriva plan↔modelo: validar el dict que build() produce y volver a
    dumpear tiene que conservar TODAS las claves. macro_params y
    macro_param_values eran campos fantasma: el dict los traía y el modelo los
    tiraba en silencio."""
    from sas_migrator.core.models.translation import TranslationPlan

    state = _state(tmp_path, placements={"A": "pandas"})
    plan = planning.build(state)
    round_tripped = TranslationPlan.model_validate(plan).model_dump(mode="json")

    assert set(plan) <= set(round_tripped), (
        f"claves perdidas al validar: {set(plan) - set(round_tripped)}"
    )
    target_keys = set(plan["targets"][0])
    model_target_keys = set(round_tripped["targets"][0])
    assert target_keys <= model_target_keys, (
        f"claves de target perdidas: {target_keys - model_target_keys}"
    )


def test_cell_logging_viaja_en_el_plan(tmp_path: Path) -> None:
    """La decisión B5b (cell_logging.yaml) sube al plan; sin artefacto cae al
    config del workspace, y sin nada es False."""
    state = _state(tmp_path, placements={"A": "pandas"})
    assert planning.build(state)["cell_logging"] is False

    (tmp_path / "project_config.yaml").write_text(
        "translation:\n  cell_logging: true\n", encoding="utf-8"
    )
    assert planning.build(state)["cell_logging"] is True, "fallback a config (legado)"

    (state / "cell_logging.yaml").write_text(
        yaml.safe_dump({"cell_logging": {"enabled": False}}), encoding="utf-8"
    )
    assert planning.build(state)["cell_logging"] is False, "la entrevista manda sobre config"
