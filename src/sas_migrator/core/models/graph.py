"""Graph models — FlowGraph, SASNode, Edge for the SAS EG DAG."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class NodeType(str, Enum):
    DATA_STEP = "DATA_STEP"
    PROC_SQL = "PROC_SQL"
    PROC_SORT = "PROC_SORT"
    PROC_FREQ = "PROC_FREQ"
    PROC_MEANS = "PROC_MEANS"
    PROC_TRANSPOSE = "PROC_TRANSPOSE"
    PROC_OTHER = "PROC_OTHER"
    MACRO = "MACRO"
    QUERY = "QUERY"
    IMPORT = "IMPORT"
    EXPORT = "EXPORT"
    OTHER = "OTHER"


class SASNode(BaseModel):
    """A single node in the SAS EG process flow."""

    id: str
    label: str = ""
    node_type: NodeType = NodeType.OTHER
    code: str = ""
    pfd_id: Optional[str] = None  # Process Flow Diagram it belongs to
    pfd_label: Optional[str] = None
    inputs: list[str] = Field(default_factory=list)   # dataset names consumed
    outputs: list[str] = Field(default_factory=list)  # dataset names produced
    libraries: list[str] = Field(default_factory=list)  # LIBNAME references
    metadata: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    """A directed edge in the DAG."""

    source: str  # node id
    target: str  # node id
    edge_type: str = "data_flow"  # data_flow | control_flow


class FlowGraph(BaseModel):
    """Complete DAG of a SAS EG project."""

    project_name: str = ""
    egp_path: str = ""
    nodes: list[SASNode] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    pfds: dict[str, str] = Field(default_factory=dict)  # pfd_id → label
    metadata: dict[str, Any] = Field(default_factory=dict)

    def node_by_id(self, node_id: str) -> Optional[SASNode]:
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}

    @property
    def orphan_nodes(self) -> list[SASNode]:
        """Nodes not connected to any edge."""
        connected = {e.source for e in self.edges} | {e.target for e in self.edges}
        return [n for n in self.nodes if n.id not in connected]


class ResidueElement(BaseModel):
    """Elemento del project.xml que no se materializó como nodo del DAG."""

    id: str
    type: str = ""
    label: str = ""
    container: str = ""


class DroppedEdge(BaseModel):
    """Arista del project.xml descartada al construir el DAG."""

    source: str
    target: str
    reason: str  # endpoint_unknown | endpoint_not_node


class ExtractionResidue(BaseModel):
    """Cuadratura .egp ↔ extracción: qué quedó fuera del DAG y por qué.

    Persistido como ``state/extraction_residue.json`` en Fase 2. El campo
    ``unexplained_count`` es el criterio del gate: cuenta solo lo que implica
    posible código SAS o topología perdidos (code.sas huérfanos, aristas hacia
    IDs desconocidos, errores de lectura, TASK sin materializar). Lo demás
    (contenedores, TASK sin código materializados con ``requires_manual_review``)
    se reporta pero no bloquea.
    """

    egp_path: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    total_elements: int = 0
    materialized_nodes: int = 0
    element_type_counts: dict[str, int] = Field(default_factory=dict)
    expected_non_nodes: list[ResidueElement] = Field(default_factory=list)
    unmaterialized_tasks: list[ResidueElement] = Field(default_factory=list)
    orphan_code_entries: list[str] = Field(default_factory=list)
    # code.sas bajo un prefijo de snapshot cuyo owner ya no está en project.xml:
    # versiones históricas superadas por el proyecto actual. Se informan para
    # dejar trazabilidad, pero NO cuentan como inexplicados.
    snapshot_code_entries: list[str] = Field(default_factory=list)
    tasks_without_code: list[str] = Field(default_factory=list)
    queries_without_payload: list[str] = Field(default_factory=list)
    dropped_edges: list[DroppedEdge] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unexplained_count: int = 0


class FlowInfo(BaseModel):
    """Resumen determinista de un Process Flow (PFD) del proyecto SAS EG.

    Un Process Flow es la unidad de agrupación natural de SAS Enterprise Guide y es
    el candidato por defecto a convertirse en UN notebook bajo la estrategia
    ``notebook-flow``. La sección estructural (conteos, tipos, entradas/salidas) la
    genera el extractor de forma determinista; el campo ``description`` lo completa
    el agente code-analyst en lenguaje de negocio.
    """

    pfd_id: str
    pfd_label: str = ""
    container_type: str = "PFD"  # PFD | ProcessFlowContainer | OrderedListContainer | other
    migratable_candidate: bool = True  # heurística: flujo de proceso real vs contenedor estructural
    node_count: int = 0
    node_ids: list[str] = Field(default_factory=list)
    node_types: dict[str, int] = Field(default_factory=dict)  # NodeType → conteo
    input_datasets: list[str] = Field(default_factory=list)
    output_datasets: list[str] = Field(default_factory=list)
    libraries: list[str] = Field(default_factory=list)
    notebook_slug: str = ""  # slug sugerido para el notebook (NB-NN_<slug>)
    description: str = ""  # resumen en lenguaje de negocio (lo completa code-analyst)


class FlowSummary(BaseModel):
    """Inventario de Process Flows detectados en el .egp.

    Persistido como ``state/flow_summary.json`` en Fase 2. Es la base para:
    - presentar al usuario los flujos detectados tras analizar el .egp,
    - seleccionar qué flujos migrar en la entrevista de Fase 4,
    - mapear un notebook por flujo en la estrategia ``notebook-flow``.
    """

    project_name: str = ""
    egp_path: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    total_flows: int = 0
    migratable_flows: int = 0
    total_nodes: int = 0
    flows: list[FlowInfo] = Field(default_factory=list)
    unassigned_nodes: list[str] = Field(default_factory=list)  # nodos sin Process Flow asignado
