"""Entrevistas reales sobre ``interrupt()`` de LangGraph (Etapa 3).

Reglas de re-ejecución (ADR-0002): un nodo con ``interrupt()`` se re-ejecuta
completo al reanudar, así que aquí NO se escribe ninguna decisión hasta el
último interrupt del nodo — las respuestas se acumulan en local y ``apply``
escribe todo al final. Los builders (core/interview) solo leen ``state/``:
el replay produce exactamente las mismas tarjetas.

Una respuesta inválida nunca es un crash: se re-presenta la MISMA tarjeta con
``validation_error`` poblado (nuevo interrupt); la revalidación del valor
inválido en el replay falla de forma determinista y el flujo converge.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.types import interrupt
from pydantic import ValidationError

from sas_migrator.core.interview import apply, initial, plan_approval, post_analysis
from sas_migrator.core.interview.validate import MULTI_ALL, AnswerError, validate_answers
from sas_migrator.core.models.interview import CardAnswers, InterviewCard

Collected = list[tuple[InterviewCard, CardAnswers]]


def _error_summary(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0]
    return text[:200]


def ask(card: InterviewCard) -> CardAnswers:
    """Presenta una tarjeta y devuelve respuestas VALIDADAS.

    Inválida ⇒ re-interrupt con la misma tarjeta y ``validation_error``.
    """
    current = card
    while True:
        raw = interrupt(current.model_dump(mode="json"))
        try:
            ans = CardAnswers.model_validate(raw)
            validate_answers(current, ans)
            return ans
        except (ValidationError, AnswerError) as exc:
            current = card.model_copy(update={"validation_error": _error_summary(exc)})


def _value_of(ans: CardAnswers, question_id: str) -> str:
    for a in ans.answers:
        if a.question_id == question_id:
            return str(a.value)
    return ""


# ── Fase 1 ──────────────────────────────────────────────────────────────────

def run_initial_interview(state_dir: Path) -> None:
    card = initial.build_initial_card(state_dir)
    ans = ask(card)
    apply.apply_initial(state_dir, [(card, ans)])


# ── Fase 4 ──────────────────────────────────────────────────────────────────

def _ask_scope_flows(state_dir: Path, collected: Collected) -> None:
    """Selección de flujos con confirmación de exclusión (loop hasta confirmar)."""
    card = post_analysis.build_scope_flows_card(state_dir)
    if card is None:
        return
    while True:
        ans = ask(card)
        selection = _value_of(ans, "Q-B2-1")
        excluded: list[str] = []
        if selection and selection.strip().lower() != MULTI_ALL:
            chosen = {p.strip() for p in selection.split(";") if p.strip()}
            flows = (
                post_analysis.load_json(Path(state_dir) / "flow_summary.json") or {}
            ).get("flows", [])
            excluded = [
                str(f.get("pfd_id"))
                for f in flows
                if f.get("migratable_candidate") and post_analysis.flow_option(f) not in chosen
            ]
        if not excluded:
            collected.append((card, ans))
            return
        confirm = post_analysis.build_scope_exclusion_confirm_card(state_dir, excluded)
        confirm_ans = ask(confirm)
        if _value_of(confirm_ans, "Q-B2-1b") == "Confirmar exclusión":
            collected.append((card, ans))
            collected.append((confirm, confirm_ans))
            return
        # "No, volver a elegir" → re-presentar la selección de flujos.


def _ask_improvements(state_dir: Path, collected: Collected) -> None:
    for card in post_analysis.build_improvement_cards(state_dir):
        imp_id = card.card_id.split(":", 1)[1]
        current = card
        while True:
            ans = ask(current)
            option = _value_of(ans, current.questions[0].id)
            if option == "Explicar más":
                collected.append((current, ans))
                current = post_analysis.build_improvement_detail_card(state_dir, imp_id)
                continue
            if not option and ans.free_text.strip() and not apply._POSTPONE.search(ans.free_text):
                # Contrapropuesta: se re-presenta la ficha modificada y se pide
                # Aprobar/Rechazar sobre esa versión (nunca se aplica en silencio).
                detail = post_analysis.build_improvement_detail_card(state_dir, imp_id)
                current = detail.model_copy(
                    update={
                        "card_id": f"B5:{imp_id}:contrapropuesta",
                        "title": f"Mejora {imp_id} (contrapropuesta del usuario)",
                        "transition": f"Registrado tu ajuste: {ans.free_text.strip()[:80]}",
                    }
                )
                continue
            collected.append((current, ans))
            break


def _ask_db_block(state_dir: Path, collected: Collected) -> None:
    step1 = post_analysis.build_db_step1_card(state_dir)
    if step1 is None:
        return  # sin evidencia de BD: B4b auto-completado, no se pregunta
    ans1 = ask(step1)
    collected.append((step1, ans1))

    confirmed: list[str] = []
    for card in post_analysis.build_placement_resolution_cards(state_dir):
        rans = ask(card)
        collected.append((card, rans))
        if _value_of(rans, card.questions[0].id) == "Es una base de datos":
            confirmed.append(card.card_id.split("B4b:resolve:", 1)[1])

    if _value_of(ans1, "Q-B4b-1") != "Sí, conectar a la base de datos":
        return  # sin conexión: se registra el supuesto y no se piden detalles
    step2 = post_analysis.build_db_connection_card(state_dir)
    collected.append((step2, ask(step2)))
    step3 = post_analysis.build_db_mapping_card(state_dir, confirmed)
    if step3 is not None:
        collected.append((step3, ask(step3)))


def _ask_api_block(state_dir: Path, collected: Collected) -> None:
    """B4c: una tarjeta por host HTTP. Elegir SDK sin nombrar el paquete
    re-presenta la MISMA tarjeta con validation_error (converge en replay)."""
    for card in post_analysis.build_api_connection_cards(state_dir):
        current = card
        while True:
            ans = ask(current)
            choice = _value_of(ans, card.questions[0].id)
            if (
                choice == "Usar una librería/SDK oficial (indico el paquete en texto libre)"
                and not apply.parse_sdk_package(ans.free_text)
            ):
                current = card.model_copy(
                    update={
                        "validation_error": (
                            "elegiste SDK: indica el nombre de import del paquete "
                            "en texto libre (ej: bcchapi)"
                        )
                    }
                )
                continue
            collected.append((card, ans))
            break


def run_post_analysis_interview(state_dir: Path) -> dict:
    state_dir = Path(state_dir)
    collected: Collected = []

    mapping = post_analysis.build_mapping_card(state_dir)
    if mapping is not None:
        collected.append((mapping, ask(mapping)))

    _ask_scope_flows(state_dir, collected)
    for card in post_analysis.build_native_node_cards(state_dir):
        collected.append((card, ask(card)))

    prep = post_analysis.build_preprocessing_card(state_dir)
    if prep is not None:
        collected.append((prep, ask(prep)))

    amb = post_analysis.build_ambiguities_card(state_dir)
    if amb is not None:
        collected.append((amb, ask(amb)))

    _ask_db_block(state_dir, collected)
    _ask_api_block(state_dir, collected)
    _ask_improvements(state_dir, collected)

    logcard = post_analysis.build_cell_logging_card(state_dir)
    collected.append((logcard, ask(logcard)))

    counts = apply.summarize_counts(state_dir, collected)
    closure = post_analysis.build_closure_card(state_dir, counts)
    closure_ans = ask(closure)
    collected.append((closure, closure_ans))
    # "No, detener aquí" no aborta el grafo: las decisiones ya tomadas se
    # persisten igual y el punto de freno real es la aprobación del plan
    # (Fase 5) — rechazar allí deja el pipeline bloqueado por gate.

    return apply.apply_post_analysis(state_dir, collected)


# ── Fase 5 ──────────────────────────────────────────────────────────────────

def run_plan_approval(state_dir: Path) -> bool:
    card = plan_approval.build_plan_card(state_dir)
    ans = ask(card)
    return apply.apply_plan_approval(state_dir, ans)
