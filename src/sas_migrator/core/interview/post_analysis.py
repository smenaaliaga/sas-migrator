"""Builders de la entrevista post-análisis (Fase 4) — bloques B1..B6.

Cada builder es una función determinista de ``state/`` (y, para las tarjetas
condicionales, de respuestas previas). Devuelven ``InterviewCard`` o ``None``
cuando la evidencia no amerita preguntar — el UX lean jamás pregunta sin
evidencia. Reglas heredadas del protocolo v1:

- B2 paso 3: los nodos nativos de EG nunca se omiten en silencio (una tarjeta
  por nodo, con recomendación).
- B4: las ambigüedades no son tarea del usuario; el default es continuar con
  supuestos visibles en el plan.
- B4b: solo se pregunta lo que la evidencia no resolvió. Una tarjeta por causa
  raíz (prefijo sin confirmar), no por nodo. Los nodos ``hybrid`` no generan
  tarjeta (su placement está resuelto por evidencia) ni los ``utility``.
- B5: una ficha M-xxx a la vez; `Explicar más` re-presenta con detalle;
  `postponed` solo existe vía texto libre.
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.interview._io import load_json, load_yaml
from sas_migrator.core.models.interview import (
    CardProgress,
    InterviewCard,
    Question,
    QuestionType,
)

INTERVIEW = "post_analysis"
PHASE = 4

# Opciones canónicas (los tests de snapshot las protegen por regresión).
MAPPING_CONFIRM_OPTIONS = ["Confirmar todos", "Revisar en texto libre"]
ORPHAN_OPTIONS = ["Excluirlos", "Mantenerlos"]
NATIVE_OPTIONS = ["Traducir a mano", "Excluir de la migración"]
PREPROCESS_OPTIONS = ["Sí, automatizar en Python", "No, lo preparo a mano"]
AMBIGUITY_OPTIONS = ["Dejar como supuestos", "Quiero aclarar alguna"]
DB_CONNECT_OPTIONS = ["Sí, conectar a la base de datos", "No, usar otra fuente (solo archivos)"]
PREFIX_OPTIONS = ["Es una base de datos", "Es una ruta de archivos (no BD)", "No sé"]
DB_CONNECTION_OPTIONS = ["Usar la conexión por defecto del proyecto", "Especificar otra conexión"]
DB_ROLE_OPTIONS = ["Solo lectura (fuente)", "Solo escritura (destino)", "Lectura y escritura"]
DB_MAPPING_OPTIONS = ["Mantener todos los nombres", "Renombrar alguna base"]
IMPROVEMENT_OPTIONS = ["Aprobar", "Rechazar", "Explicar más"]
IMPROVEMENT_DETAIL_OPTIONS = ["Aprobar", "Rechazar"]
CLOSURE_OPTIONS = ["Sí, proceder al plan de traducción", "No, detener aquí"]


def _card(card_id: str, block_id: str, title: str, **kwargs) -> InterviewCard:
    return InterviewCard(
        card_id=card_id,
        interview_type=INTERVIEW,
        phase=PHASE,
        block_id=block_id,
        title=title,
        **kwargs,
    )


# ── B1: confirmación de mapping ─────────────────────────────────────────────

def build_mapping_card(state_dir: Path) -> InterviewCard | None:
    doc = load_json(Path(state_dir) / "file_mapping.json") or {}
    mappings = [m for m in doc.get("mappings", []) if isinstance(m, dict)]
    if not mappings:
        return None

    high = [m for m in mappings if float(m.get("confidence") or 0) >= 0.5]
    low = [m for m in mappings if float(m.get("confidence") or 0) < 0.5]

    def _line(m: dict) -> str:
        name = Path(str(m.get("file_path", ""))).name
        node = m.get("node_id") or "sin nodo"
        return f"file_mapping.json: {name} → {node} (confianza {m.get('confidence', 0)})"

    questions: list[Question] = []
    if high:
        questions.append(
            Question(
                id="Q-B1M-1",
                text=(
                    f"Detecté {len(high)} match(es) de datos con confianza alta/media. "
                    "¿Confirmamos?"
                ),
                question_type=QuestionType.APPROVAL,
                options=list(MAPPING_CONFIRM_OPTIONS),
                recommended_default="Confirmar todos",
                evidence=[_line(m) for m in high],
            )
        )
    if low:
        questions.append(
            Question(
                id="Q-B1M-2",
                text=(
                    f"{len(low)} match(es) quedaron con baja confianza. ¿Puedes confirmar "
                    "alguno o los dejamos pendientes?"
                ),
                required=False,
                recommended_default="dejarlos pendientes",
                evidence=[_line(m) for m in low],
            )
        )
    questions.append(
        Question(
            id="Q-B1M-3",
            text="¿Hay archivos de entrada importantes que no aparezcan en la lista?",
            required=False,
            recommended_default="no",
        )
    )
    return _card(
        "B1-mapping",
        "B1-mapping-confirm",
        "Confirmación de mapping de datos",
        transition="Terminé el análisis, vamos con las decisiones:",
        questions=questions,
    )


# ── B2: alcance ─────────────────────────────────────────────────────────────

def flow_option(flow: dict) -> str:
    """Formato canónico de opción de flujo — apply lo parsea por el ':'."""
    label = flow.get("pfd_label") or "(sin etiqueta)"
    return f"{flow.get('pfd_id')}: {label} ({flow.get('node_count', 0)} nodos)"


def _orphan_nodes(state_dir: Path) -> list[str]:
    """Nodos sin ninguna arista en el DAG (candidatos a obsoletos)."""
    fg = load_json(Path(state_dir) / "flow_graph.json") or {}
    connected: set[str] = set()
    for e in fg.get("edges", []):
        connected.add(str(e.get("source")))
        connected.add(str(e.get("target")))
    index = load_json(Path(state_dir) / "nodes_index.json") or {}
    return sorted(
        n["id"]
        for n in index.get("nodes", [])
        if n["id"] not in connected and not n.get("native_task")
    )


def build_scope_flows_card(state_dir: Path) -> InterviewCard | None:
    summary = load_json(Path(state_dir) / "flow_summary.json") or {}
    flows = [f for f in summary.get("flows", []) if f.get("migratable_candidate")]
    if not flows:
        return None

    options = [flow_option(f) for f in flows]
    total_nodes = sum(int(f.get("node_count", 0)) for f in flows)
    questions = [
        Question(
            id="Q-B2-1",
            text=(
                f"El .egp contiene {len(flows)} Process Flow(s) migrable(s) "
                f"({total_nodes} nodos en total). Cada flujo seleccionado será un notebook "
                "independiente. ¿Cuáles migramos?"
            ),
            question_type=QuestionType.MULTI_CHOICE,
            options=options,
            recommended_default="todos",
            evidence=[
                f"flow_summary.json: {f.get('pfd_label') or f.get('pfd_id')} — "
                f"{f.get('node_count', 0)} nodos"
                for f in flows
            ],
        )
    ]
    orphans = _orphan_nodes(state_dir)
    if orphans:
        questions.append(
            Question(
                id="Q-B2-2",
                text=(
                    f"Estos {len(orphans)} nodo(s) no tienen aristas en el DAG (posibles "
                    "obsoletos). ¿Los excluimos de la migración inicial?"
                ),
                question_type=QuestionType.APPROVAL,
                options=list(ORPHAN_OPTIONS),
                recommended_default="Excluirlos",
                evidence=[f"flow_graph.json: {nid} sin aristas" for nid in orphans],
            )
        )
    questions.append(
        Question(
            id="Q-B2-3",
            text="¿Hay algún nodo que sepas que no se usa aunque aparezca conectado?",
            required=False,
            recommended_default="no",
        )
    )
    return _card(
        "B2-scope:flows",
        "B2-scope",
        "Alcance de la migración",
        questions=questions,
    )


def build_scope_exclusion_confirm_card(
    state_dir: Path, excluded_pfd_ids: list[str]
) -> InterviewCard | None:
    """Tarjeta condicional: confirmar la exclusión de flujos completos."""
    if not excluded_pfd_ids:
        return None
    summary = load_json(Path(state_dir) / "flow_summary.json") or {}
    by_id = {f.get("pfd_id"): f for f in summary.get("flows", [])}
    affected = sum(int(by_id.get(p, {}).get("node_count", 0)) for p in excluded_pfd_ids)
    return _card(
        "B2-scope:confirm-exclusion",
        "B2-scope",
        "Confirmar exclusión de flujos",
        questions=[
            Question(
                id="Q-B2-1b",
                text=(
                    f"Excluyendo {len(excluded_pfd_ids)} flujo(s): {affected} nodo(s) "
                    "quedarán fuera del plan. ¿Confirmas?"
                ),
                question_type=QuestionType.APPROVAL,
                options=["Confirmar exclusión", "No, volver a elegir"],
                recommended_default="Confirmar exclusión",
                evidence=[
                    f"flow_summary.json: {p} — "
                    f"{by_id.get(p, {}).get('node_count', 0)} nodos"
                    for p in excluded_pfd_ids
                ],
            )
        ],
    )


def build_native_node_cards(state_dir: Path) -> list[InterviewCard]:
    """Una tarjeta por nodo nativo de EG sin código SAS — nunca en silencio."""
    index = load_json(Path(state_dir) / "nodes_index.json") or {}
    natives = [
        n
        for n in index.get("nodes", [])
        if n.get("native_task") or n.get("requires_manual_review")
    ]
    cards: list[InterviewCard] = []
    for i, n in enumerate(sorted(natives, key=lambda x: x["id"]), start=1):
        label = n.get("label") or n["id"]
        cards.append(
            _card(
                f"B2-scope:native:{n['id']}",
                "B2-scope",
                f"Tarea nativa de EG: {label}",
                questions=[
                    Question(
                        id=f"Q-B2-4-{n['id']}",
                        text=(
                            f"El nodo '{label}' ({n['id']}) es una tarea de Enterprise "
                            "Guide sin código SAS extraíble. ¿Lo excluimos, o lo "
                            "traducimos a mano con tu descripción de lo que hace?"
                        ),
                        question_type=QuestionType.SINGLE_CHOICE,
                        options=list(NATIVE_OPTIONS),
                        recommended_default="Traducir a mano",
                        evidence=[
                            f"nodes_index.json: {n['id']} — tipo {n.get('node_type', '?')}, "
                            f"flujo {n.get('pfd_label') or n.get('pfd_id') or '?'}",
                            "el extractor no encontró código; excluirlo pierde el paso",
                        ],
                    )
                ],
                progress=CardProgress(index=i, total=len(natives)),
            )
        )
    return cards


# ── B3: pre-procesamiento ───────────────────────────────────────────────────

def build_preprocessing_card(state_dir: Path) -> InterviewCard | None:
    doc = load_json(Path(state_dir) / "profile_report.json")
    profiles = doc if isinstance(doc, list) else (doc or {}).get("profiles", [])
    problematic = [p for p in profiles if isinstance(p, dict) and p.get("error")]
    if not problematic:
        return None
    return _card(
        "B3-preprocessing",
        "B3-preprocessing",
        "Pre-procesamiento manual",
        questions=[
            Question(
                id="Q-B3-1",
                text=(
                    f"Detecté problemas de formato en {len(problematic)} archivo(s) de "
                    "entrada. ¿Automatizamos su preparación en Python?"
                ),
                question_type=QuestionType.APPROVAL,
                options=list(PREPROCESS_OPTIONS),
                recommended_default="Sí, automatizar en Python",
                evidence=[
                    f"profile_report.json: {Path(str(p.get('file_path', ''))).name} — "
                    f"{p.get('error')}"
                    for p in problematic
                ],
            ),
            Question(
                id="Q-B3-3",
                text="¿Hay algún ajuste manual previo al SAS que no esté en los archivos?",
                required=False,
                recommended_default="no",
            ),
        ],
    )


# ── B4: ambigüedades del código ─────────────────────────────────────────────

def build_ambiguities_card(state_dir: Path) -> InterviewCard | None:
    ev = load_json(Path(state_dir) / "analysis_evidence.json") or {}
    macro_vars = ev.get("macro_variables", [])
    if not macro_vars:
        return None
    top = macro_vars[:5]
    return _card(
        "B4-ambiguities",
        "B4-sas-ambiguities",
        "Ambigüedades del código SAS",
        questions=[
            Question(
                id="Q-B4-1",
                text=(
                    "Encontré reglas SAS que puedo traducir de más de una forma "
                    "(variables macro dinámicas). No necesitas resolverlas: si conoces el "
                    "contexto de alguna, cuéntame; si no, las dejo como supuestos "
                    "visibles en el plan."
                ),
                question_type=QuestionType.SINGLE_CHOICE,
                required=False,
                options=list(AMBIGUITY_OPTIONS),
                recommended_default="Dejar como supuestos",
                evidence=[
                    f"analysis_evidence.json: &{m.get('variable')} en "
                    f"{m.get('node_count', 0)} nodo(s)"
                    for m in top
                ],
            )
        ],
    )


# ── B4b: conexiones a BD + resolución de placement ──────────────────────────

def _db_evidence(state_dir: Path) -> dict:
    return load_json(Path(state_dir) / "db_evidence.json") or {}


def build_db_step1_card(state_dir: Path) -> InterviewCard | None:
    """¿El resultado se conectará a la BD? Solo si hay evidencia de BD."""
    ev = _db_evidence(state_dir)
    librefs = ev.get("librefs", [])
    unverified = ev.get("unverified_prefixes", [])
    connects = ev.get("connect_to_statements", [])
    if not librefs and not unverified and not connects:
        return None

    evidence = [
        f"db_evidence.json: {lr.get('libref')} — {lr.get('table_count', 0)} tabla(s) en "
        f"{lr.get('node_count', 0)} nodo(s) ({lr.get('source')})"
        for lr in librefs
    ]
    if unverified:
        evidence.append(
            f"db_evidence.json: {len(unverified)} prefijo(s) sin confirmar: "
            + ", ".join(u.get("prefix", "?") for u in unverified)
        )
    if connects:
        evidence.append(
            f"db_evidence.json: {len(connects)} CONNECT TO explícito(s) (passthrough)"
        )
    return _card(
        "B4b:step1",
        "B4b-db-connections",
        "Conexión a base de datos",
        questions=[
            Question(
                id="Q-B4b-1",
                text=(
                    "El flujo SAS depende de bases de datos. ¿El resultado (notebooks) se "
                    "conectará a la base que reemplaza a la que usaba SAS?"
                ),
                question_type=QuestionType.SINGLE_CHOICE,
                options=list(DB_CONNECT_OPTIONS),
                recommended_default="Sí, conectar a la base de datos",
                evidence=evidence,
            )
        ],
    )


def unconfirmed_prefixes(state_dir: Path) -> list[dict]:
    """Causas raíz de placement ambiguo: prefijos sin LIBNAME que los confirme.

    Un prefijo confirmado (source=libname_statement) NO genera tarjeta — solo
    se pregunta lo que la evidencia no resolvió.
    """
    ev = _db_evidence(state_dir)
    causes: list[dict] = []
    for lr in ev.get("librefs", []):
        if lr.get("source") != "libname_statement":
            causes.append(
                {
                    "prefix": lr.get("libref"),
                    "tables": [t.get("table") for t in lr.get("tables", [])],
                    "node_ids": sorted(
                        {n for t in lr.get("tables", []) for n in t.get("node_ids", [])}
                    ),
                    "writes": any(
                        t.get("access") in ("write", "both") for t in lr.get("tables", [])
                    ),
                    "source": lr.get("source"),
                }
            )
    for u in ev.get("unverified_prefixes", []):
        causes.append(
            {
                "prefix": u.get("prefix"),
                "tables": u.get("tables", []),
                "node_ids": u.get("node_ids", []),
                "writes": False,
                "source": "unverified",
            }
        )
    return sorted(causes, key=lambda c: str(c["prefix"]))


def build_placement_resolution_cards(state_dir: Path) -> list[InterviewCard]:
    """Una tarjeta por causa raíz (prefijo sin confirmar), no por nodo."""
    causes = unconfirmed_prefixes(state_dir)
    index = load_json(Path(state_dir) / "nodes_index.json") or {}
    ambiguous = [n for n in index.get("nodes", []) if n.get("placement") == "ambiguous"]

    cards: list[InterviewCard] = []
    for i, cause in enumerate(causes, start=1):
        prefix = str(cause["prefix"])
        affected = sorted(
            n["id"]
            for n in ambiguous
            if any(prefix in str(r) for r in n.get("placement_reasons", []))
        ) or cause["node_ids"]
        recommended = "Es una base de datos" if (cause["writes"] or len(cause["tables"]) > 2) \
            else "No sé"
        evidence = [
            f"db_evidence.json: prefijo {prefix} ({cause['source']}) — "
            f"tablas: {', '.join(map(str, cause['tables'][:6])) or '—'}",
            f"nodos afectados: {', '.join(affected[:8]) or '—'}",
        ]
        cards.append(
            _card(
                f"B4b:resolve:{prefix}",
                "B4b-db-connections",
                f"Prefijo sin confirmar: {prefix}",
                questions=[
                    Question(
                        id=f"Q-B4b-R-{prefix}",
                        text=(
                            f"'{prefix}' se usa como LIBREF.TABLA pero ningún LIBNAME lo "
                            "declara. ¿Es una base de datos, o una ruta de archivos?"
                        ),
                        question_type=QuestionType.SINGLE_CHOICE,
                        options=list(PREFIX_OPTIONS),
                        recommended_default=recommended,
                        evidence=evidence,
                    )
                ],
                progress=CardProgress(index=i, total=len(causes)),
            )
        )
    return cards


def build_db_connection_card(state_dir: Path) -> InterviewCard:
    """Paso 2 de B4b (solo si step1 = sí): qué conexión usar y con qué rol."""
    from sas_migrator.core.config import load_project_config

    cfg = load_project_config(Path(state_dir).parent)
    default_server = cfg.db.default_server
    ev = _db_evidence(state_dir)
    any_write = any(
        t.get("access") in ("write", "both")
        for lr in ev.get("librefs", [])
        for t in lr.get("tables", [])
    )
    server_evidence = (
        [f"project_config.yaml: default_server = {default_server}"]
        if default_server
        else ["project_config.yaml: sin default_server configurado"]
    )
    return _card(
        "B4b:step2",
        "B4b-db-connections",
        "Conexión a usar",
        questions=[
            Question(
                id="Q-B4b-2",
                text="¿Qué conexión de base de datos usará el resultado?",
                question_type=QuestionType.SINGLE_CHOICE,
                options=list(DB_CONNECTION_OPTIONS),
                recommended_default=(
                    "Usar la conexión por defecto del proyecto"
                    if default_server
                    else "Especificar otra conexión"
                ),
                evidence=server_evidence,
            ),
            Question(
                id="Q-B4b-4",
                text="¿La conexión se usa como fuente, destino o ambos?",
                question_type=QuestionType.SINGLE_CHOICE,
                options=list(DB_ROLE_OPTIONS),
                recommended_default=(
                    "Lectura y escritura" if any_write else "Solo lectura (fuente)"
                ),
                evidence=[
                    "db_evidence.json: "
                    + ("hay tablas con escritura detectada" if any_write else "solo lecturas")
                ],
            ),
        ],
    )


def build_db_mapping_card(state_dir: Path) -> InterviewCard | None:
    """Paso 3 de B4b: mapeo libref → base. Los nombres de tabla nunca cambian."""
    ev = _db_evidence(state_dir)
    librefs = ev.get("librefs", [])
    if not librefs:
        return None
    return _card(
        "B4b:step3",
        "B4b-db-connections",
        "Mapeo de librerías SAS a bases",
        questions=[
            Question(
                id="Q-B4b-5",
                text=(
                    "Propongo mantener el nombre de cada librería SAS como nombre de base "
                    "(esquema dbo, tablas sin cambio). ¿Mantenemos todos los nombres?"
                ),
                question_type=QuestionType.SINGLE_CHOICE,
                options=list(DB_MAPPING_OPTIONS),
                recommended_default="Mantener todos los nombres",
                evidence=[
                    f"{lr.get('libref')}.TABLA → {lr.get('libref')}.dbo.TABLA "
                    f"({lr.get('table_count', 0)} tabla(s))"
                    for lr in librefs
                ],
            )
        ],
    )


# ── B5: mejoras M-xxx ───────────────────────────────────────────────────────

def _improvements(state_dir: Path) -> list[dict]:
    doc = load_yaml(Path(state_dir) / "improvements_proposed.yaml") or {}
    return [i for i in doc.get("improvements", []) if isinstance(i, dict)]


def _improvement_question(item: dict, *, detail: bool) -> Question:
    imp_id = str(item.get("id"))
    evidence = [
        f"impacto {item.get('impact', '?')} · esfuerzo {item.get('effort', '?')} · "
        f"riesgo {item.get('risk', '?')}",
        f"afecta {len(item.get('affected_nodes', []))} nodo(s)",
        f"justificación: {item.get('justification', '')}",
    ]
    if detail:
        evidence.append("nodos: " + (", ".join(item.get("affected_nodes", [])[:20]) or "—"))
        evidence.append(f"recomendación del análisis: {item.get('recommendation', '')}")
    rec = str(item.get("recommendation", "")).lower()
    recommended = "Aprobar" if rec.startswith(("approve", "aprobar")) else "Rechazar"
    return Question(
        id=f"Q-{imp_id}",
        text=f"{item.get('title', imp_id)} — {item.get('description', '')} ¿Qué decides?",
        question_type=QuestionType.APPROVAL,
        options=list(IMPROVEMENT_DETAIL_OPTIONS if detail else IMPROVEMENT_OPTIONS),
        recommended_default=recommended,
        evidence=evidence,
        context=str(item.get("description", "")),
    )


def build_improvement_cards(state_dir: Path) -> list[InterviewCard]:
    """Una tarjeta por ficha M-xxx, en orden de id. `postponed` no es opción:
    solo se registra si el usuario lo pide en texto libre."""
    items = sorted(_improvements(state_dir), key=lambda i: str(i.get("id")))
    cards = []
    for i, item in enumerate(items, start=1):
        imp_id = str(item.get("id"))
        cards.append(
            _card(
                f"B5:{imp_id}",
                "B5-improvements",
                f"Mejora {imp_id}: {item.get('title', '')}",
                questions=[_improvement_question(item, detail=False)],
                progress=CardProgress(index=i, total=len(items)),
            )
        )
    return cards


def build_improvement_detail_card(state_dir: Path, imp_id: str) -> InterviewCard | None:
    """Variante 'Explicar más': misma ficha con detalle ampliado, sin registrar
    decisión todavía."""
    item = next((i for i in _improvements(state_dir) if str(i.get("id")) == imp_id), None)
    if item is None:
        return None
    return _card(
        f"B5:{imp_id}:detalle",
        "B5-improvements",
        f"Mejora {imp_id} (detalle): {item.get('title', '')}",
        questions=[_improvement_question(item, detail=True)],
    )


# ── B6: cierre ──────────────────────────────────────────────────────────────

def build_closure_card(state_dir: Path, counts: dict) -> InterviewCard:
    """Q6.1 con conteos calculados desde las decisiones de los bloques previos.
    Q6.2 del protocolo v1 se eliminó: la revisión del plan es el interrupt de
    aprobación de la Fase 5 (una decisión a la vez)."""
    return _card(
        "B6-closure",
        "B6-closure",
        "Cierre de la entrevista",
        questions=[
            Question(
                id="Q-B6-1",
                text=(
                    f"Resumen: migraremos {counts.get('migrate', 0)} nodo(s), "
                    f"excluiremos {counts.get('excluded', 0)}, aplicaremos "
                    f"{counts.get('improvements', 0)} mejora(s) y quedan "
                    f"{counts.get('assumptions', 0)} supuesto(s) visibles. "
                    "¿Procedemos con el plan de traducción?"
                ),
                question_type=QuestionType.APPROVAL,
                options=list(CLOSURE_OPTIONS),
                recommended_default="Sí, proceder al plan de traducción",
            )
        ],
    )
