from sas_migrator.core.models.artifacts import IntakeCatalog
from sas_migrator.core.models.data import AuthMethod, DatabaseConnection, DatabaseConnections
from sas_migrator.core.models.state import MigrationState
from sas_migrator.core.models.translation import OutputStrategy, TranslationPlan, TranslationTarget
from sas_migrator.core.models.graph import FlowGraph, FlowSummary, SASNode, Edge, NodeType
from sas_migrator.core.extractors.egp import build_flow_summary


def test_default_output_strategy_is_notebook_flow() -> None:
    state = MigrationState()
    plan = TranslationPlan()

    assert state.output_strategy == "notebook-flow"
    assert plan.output_strategy == OutputStrategy.NOTEBOOK_FLOW


def test_intake_catalog_accepts_generic_files() -> None:
    catalog = IntakeCatalog(
        egp_files=[{"path": "flujo.egp", "size_bytes": 12}],
        data_files=[{"path": "data/input.csv", "type": "csv", "size_bytes": 34}],
        doc_files=[{"path": "README.md"}],
    )

    assert catalog.egp_files[0].path == "flujo.egp"
    assert catalog.data_files[0].type == "csv"


def test_database_connection_defaults_to_active_directory_integrated() -> None:
    connections = DatabaseConnections(
        connections=[DatabaseConnection(alias="PLATDAT", database="PYCNSIGOB")]
    )

    assert connections.connections[0].auth_method == AuthMethod.ACTIVE_DIRECTORY_INTEGRATED


def test_database_connection_has_no_dry_run_fields() -> None:
    fields = DatabaseConnection.model_fields
    assert "dry_run" not in fields
    assert "csv_fallback_dir" not in fields


def test_translation_plan_nodes_are_generic() -> None:
    plan = TranslationPlan(
        project_name="Migracion Generica",
        egp_file="flujo.egp",
        user_approved=True,
        targets=[
            TranslationTarget(
                node_id="CodeTask-1",
                notebook_path="output/NB-001.ipynb",
                output_tables=["RESULTADO"],
            )
        ],
    )

    assert plan.targets[0].node_id == "CodeTask-1"
    assert plan.targets[0].output_tables == ["RESULTADO"]


def test_build_flow_summary_groups_nodes_by_pfd() -> None:
    node_a = SASNode(id="CodeTask-A", pfd_id="PFD-1", pfd_label="Flujo Uno", node_type=NodeType.PROC_SQL)
    node_b = SASNode(id="CodeTask-B", pfd_id="PFD-1", pfd_label="Flujo Uno", node_type=NodeType.DATA_STEP)
    node_c = SASNode(id="CodeTask-C", pfd_id="PFD-2", pfd_label="Flujo Dos", node_type=NodeType.PROC_SORT)
    node_orphan = SASNode(id="CodeTask-D")  # no pfd_id

    graph = FlowGraph(
        project_name="Test",
        nodes=[node_a, node_b, node_c, node_orphan],
        pfds={"PFD-1": "Flujo Uno", "PFD-2": "Flujo Dos"},
    )

    summary = build_flow_summary(graph)

    assert isinstance(summary, FlowSummary)
    assert summary.total_flows == 2
    assert summary.migratable_flows == 2
    assert summary.total_nodes == 4
    assert summary.unassigned_nodes == ["CodeTask-D"]

    flow1 = next(f for f in summary.flows if f.pfd_id == "PFD-1")
    assert flow1.node_count == 2
    assert flow1.notebook_slug == "flujo_uno"
    assert "PROC_SQL" in flow1.node_types
    assert "DATA_STEP" in flow1.node_types

    flow2 = next(f for f in summary.flows if f.pfd_id == "PFD-2")
    assert flow2.node_count == 1
    assert flow2.notebook_slug == "flujo_dos"


def test_ordered_list_container_not_migratable() -> None:
    graph = FlowGraph(
        project_name="Test",
        nodes=[],
        pfds={"OrderedListContainer-X": "Listas ordenadas"},
    )
    summary = build_flow_summary(graph)
    assert summary.migratable_flows == 0
    assert summary.flows[0].migratable_candidate is False