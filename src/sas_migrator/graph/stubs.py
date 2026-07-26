"""Stubs deterministas de los nodos LLM — Etapa 1.

Cada función produce lo que en la arquitectura final producirá un nodo LLM
(reviews, descripciones, fichas, entrevistas, notebooks), pero de forma
determinista y satisfaciendo los gates reales. Sirven para:

- correr el pipeline completo sin API key (tests, golden runs, CI);
- fijar el CONTRATO de cada nodo LLM antes de implementarlo (Etapa 4);
- el stub de generación es el embrión del ensamblador determinista de
  notebooks (cell_index correcto por construcción).

Los artefactos se construyen instanciando los modelos Pydantic del core:
válidos contra schema por construcción, no por suerte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from sas_migrator.core.models.analysis import (
    Improvement,
    ImprovementCategory,
    ImprovementStatus,
)
from sas_migrator.core.models.interview import (
    Answer,
    InterviewQA,
    Question,
    QuestionBlock,
)


def _dump_yaml(path: Path, data: Any) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _dump_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _model_dump(model) -> dict:
    return json.loads(model.model_dump_json())


# ── Fase 1: entrevista inicial ───────────────────────────────────────────────

def stub_initial_interview(state_dir: Path) -> None:
    """Las 3 preguntas fijas de la Fase 1, respondidas con defaults de stub."""
    qa = InterviewQA(
        interview_type="initial",
        phase=1,
        blocks=[
            QuestionBlock(
                block_id="B1-initial",
                title="Entrevista inicial",
                questions=[
                    Question(id="Q-001", text="¿De qué trata este flujo?"),
                    Question(id="Q-002", text="¿Consideraciones especiales?"),
                    Question(
                        id="Q-003",
                        text="¿Existen archivos de referencia de entrada y salida?",
                        question_type="single_choice",
                        options=["sí", "no", "no sé"],
                    ),
                ],
                answers=[
                    Answer(question_id="Q-001", value="[stub] Flujo de demostración."),
                    Answer(question_id="Q-002", value="[stub] Ninguna."),
                    Answer(question_id="Q-003", value="no"),
                ],
            )
        ],
    )
    _dump_yaml(state_dir / "initial_interview.yaml", _model_dump(qa))


# ── Fase 2: criterio del code-analyst ────────────────────────────────────────

def stub_analysis_reviews(state_dir: Path) -> None:
    """Reviews por flujo + descripciones de negocio + fichas M-xxx.

    Debe pasar los gates de sustancia: nota DISTINTA por nodo (anti-boilerplate),
    description en cada flujo migrable, y category_scan_summary sin contradecir
    a code_smells.json.
    """
    index = json.loads((state_dir / "nodes_index.json").read_text(encoding="utf-8"))
    nodes = index.get("nodes", [])

    # Reviews por PFD con nota única por nodo (varía por id/label/tipo).
    reviews_dir = state_dir / "analysis_reviews"
    reviews_dir.mkdir(exist_ok=True)
    by_pfd: dict[str, list[dict]] = {}
    for n in nodes:
        pfd = str(n.get("pfd_id") or "sin_pfd")
        by_pfd.setdefault(pfd, []).append(
            {
                "node_id": n["id"],
                "note": (
                    f"[stub] {n.get('label', n['id'])}: nodo {n.get('node_type', '?')} "
                    f"con {n.get('n_inputs', 0)} entrada(s) y {n.get('n_outputs', 0)} "
                    f"salida(s); revisar en Etapa 4 con LLM real ({n['id']})."
                ),
            }
        )
    for pfd, items in by_pfd.items():
        _dump_json(reviews_dir / f"{pfd}.json", {"pfd_id": pfd, "reviews": items})

    # Descripciones de negocio por flujo migrable.
    summary_path = state_dir / "flow_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for flow in summary.get("flows", []):
        if flow.get("migratable_candidate") and not str(flow.get("description") or "").strip():
            flow["description"] = (
                f"[stub] Flujo '{flow.get('pfd_label', flow.get('pfd_id'))}' con "
                f"{flow.get('node_count', '?')} nodos; descripción pendiente de LLM real."
            )
    _dump_json(summary_path, summary)

    # Fichas M-xxx: una por categoría de smell con evidencia; scan summary
    # coherente (nunca "No improvements found" sobre categorías con smells).
    smells_doc = json.loads((state_dir / "code_smells.json").read_text(encoding="utf-8"))
    smell_cats = sorted(
        {str(s.get("category", "")).strip() for s in smells_doc.get("smells", []) if s.get("category")}
    )
    improvements = []
    for i, cat in enumerate(smell_cats, start=1):
        affected = sorted(
            {
                str(s.get("node_id", ""))
                for s in smells_doc.get("smells", [])
                if str(s.get("category", "")).strip() == cat and s.get("node_id")
            }
        )
        improvements.append(
            Improvement(
                id=f"M-{i:03d}",
                category=ImprovementCategory.QUALITY,
                title=f"[stub] Atender smells de categoría '{cat}'",
                description=f"[stub] {len(affected)} nodo(s) con smell '{cat}'.",
                justification="[stub] Evidencia en code_smells.json.",
                impact="low",
                effort="low",
                risk="low",
                recommendation="reject",
                affected_nodes=affected,
                status=ImprovementStatus.PROPOSED,
            )
        )
    _dump_yaml(
        state_dir / "improvements_proposed.yaml",
        {
            "improvements": [_model_dump(m) for m in improvements],
            "category_scan_summary": {
                cat: f"Ficha redactada desde evidencia (stub): {cat}" for cat in smell_cats
            },
        },
    )


# ── Fase 3: matching ─────────────────────────────────────────────────────────

def stub_file_mapping(state_dir: Path) -> None:
    """file_mapping vacío o 1:1 trivial contra los perfiles existentes."""
    profiles_path = state_dir / "profile_report.json"
    profiles = []
    if profiles_path.exists():
        doc = json.loads(profiles_path.read_text(encoding="utf-8"))
        profiles = doc if isinstance(doc, list) else doc.get("profiles", [])
    mappings = [
        {
            "file_path": p.get("file_path", ""),
            "node_id": None,
            "role": "unknown",
            "confidence": 0.0,
            "reasons": ["[stub] matching pendiente de LLM real"],
            "needs_confirmation": True,
        }
        for p in profiles
    ]
    _dump_json(state_dir / "file_mapping.json", {"mappings": mappings})
    if profiles:
        _dump_yaml(state_dir / "column_mapping.yaml", {"mappings": []})


# ── Fase 4: entrevista post-análisis ─────────────────────────────────────────

def stub_post_analysis_interview(state_dir: Path) -> None:
    """Decisiones default: sin exclusiones, toda mejora propuesta → rejected
    (el stub no aprueba cambios en nombre del usuario)."""
    qa = InterviewQA(
        interview_type="post_analysis",
        phase=4,
        blocks=[
            QuestionBlock(
                block_id="B2-scope",
                title="Alcance",
                questions=[
                    Question(
                        id="Q-B2-1",
                        text="¿Qué Process Flows se migran?",
                        question_type="multi_choice",
                    )
                ],
                answers=[Answer(question_id="Q-B2-1", value="[stub] todos")],
            )
        ],
    )
    _dump_yaml(state_dir / "post_analysis_interview.yaml", _model_dump(qa))
    _dump_yaml(state_dir / "ignored_nodes.yaml", {"ignored_nodes": []})

    proposed = yaml.safe_load(
        (state_dir / "improvements_proposed.yaml").read_text(encoding="utf-8")
    ) or {}
    decided = []
    for item in proposed.get("improvements", []):
        item = dict(item)
        item["status"] = "rejected"
        item["user_comment"] = "[stub] decisión default; revisar con usuario real"
        decided.append(item)
    _dump_yaml(state_dir / "approved_improvements.yaml", {"improvements": decided})


# ── Fase 5: aprobación del plan ──────────────────────────────────────────────

def stub_approve_plan(state_dir: Path) -> None:
    plan_path = state_dir / "translation_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["user_approved"] = True
    plan.setdefault("notes", []).append("[stub] aprobado automáticamente en stub_mode")
    _dump_json(plan_path, plan)


# ── Fase 6: generación (stub de traducción + ensamblador real) ───────────────

def stub_node_translations(plan: dict) -> dict:
    """NodeTranslation determinista por target — el 'traductor' del stub.

    Las celdas incluyen `.merge(`/`.groupby(` para satisfacer las señales de
    traducción del gate 6 sin fingir una traducción real.
    """
    from sas_migrator.core.models.translation import NodeTranslation, Traceability

    translations = {}
    for t in plan.get("targets", []):
        label = t.get("node_label") or t["node_id"]
        translations[t["node_id"]] = NodeTranslation(
            node_id=t["node_id"],
            node_label=label,
            strategy=t.get("strategy", "pandas"),
            cells=[
                f"# [stub] Traducción pendiente de LLM real ({t['node_id']}).\n"
                "df = pd.DataFrame()\n"
                "df = df.merge(df, how='left', left_index=True, right_index=True)\n"
                "resumen = df.groupby(level=0).size() if len(df) else None\n"
            ],
            traceability=Traceability(sas_construct="[stub]", business_rule="[stub]"),
            confidence="low",
        )
    return translations


def stub_generate_notebooks(state_dir: Path, output_dir: Path) -> None:
    """Traducción stub + ensamblador determinista REAL (core.assembly).

    El mapping SAS→Python sale de los índices de celda calculados al ensamblar
    — el contrato clave de la arquitectura v2 (cell_index correcto por
    construcción, nunca mantenido a mano). El camino stub ejercita el mismo
    ensamblador que usará la traducción LLM.
    """
    from sas_migrator.core.assembly.notebook import assemble_notebooks

    plan = json.loads((state_dir / "translation_plan.json").read_text(encoding="utf-8"))
    mapping, failures = assemble_notebooks(plan, stub_node_translations(plan), output_dir)
    if failures:  # el stub emite traducciones siempre válidas — esto es un bug
        raise RuntimeError(f"stub de traducción falló chequeos estáticos: {failures}")
    _dump_json(state_dir / "sas_python_mapping.json", _model_dump(mapping))


# ── Fase 7: reporte de validación (modo sin insumos) ─────────────────────────

def stub_validation_report(state_dir: Path) -> None:
    """Reporte honesto: sin referencias ni BD en stub → not_applicable."""
    _dump_json(
        state_dir / "validation_report.json",
        {
            "mode": "not_applicable",
            "overall_status": "PASS",
            "results": [],
            "notes": ["[stub] sin referencias ni conexión BD; validación pendiente"],
        },
    )


# ── Fase 8: documentación ────────────────────────────────────────────────────

def stub_docs(state_dir: Path, output_dir: Path) -> None:
    docs = output_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for name in ("README.md", "LINEAGE.md", "DECISIONS.md", "IMPROVEMENTS.md", "RUNBOOK.md"):
        (docs / name).write_text(
            f"# {name[:-3]}\n\n[stub] Documento generado por stub de Etapa 1; "
            "la prosa real la escribe el nodo LLM doc-writer (Etapa 4).\n",
            encoding="utf-8",
        )
