"""Validación semántica de respuestas a una tarjeta.

Pydantic valida la FORMA (``CardAnswers.model_validate``); aquí se valida el
CONTENIDO contra la tarjeta: ids conocidos, opciones reales, requeridas
cubiertas. Una respuesta inválida nunca es un crash: el nodo re-presenta la
tarjeta con ``validation_error`` poblado.

Determinista por diseño (sin reloj ni red): el replay de un nodo interrumpido
revalida lo mismo y converge al mismo resultado.
"""

from __future__ import annotations

from sas_migrator.core.models.interview import (
    CardAnswers,
    InterviewCard,
    Question,
    QuestionType,
)

# Valores aceptados para yes_no sin options explícitas.
_YES_NO = {"sí", "si", "no", "no sé", "no se"}

# La selección "todos" es el atajo canónico de multi_choice.
MULTI_ALL = "todos"


class AnswerError(ValueError):
    """Respuesta bien formada pero semánticamente inválida para la tarjeta."""


def _check_choice(q: Question, value: str) -> None:
    if q.question_type == QuestionType.MULTI_CHOICE:
        if value.strip().lower() == MULTI_ALL:
            return
        parts = [p.strip() for p in value.split(";") if p.strip()]
        if not parts:
            raise AnswerError(f"{q.id}: selección vacía")
        for part in parts:
            if part not in q.options:
                raise AnswerError(
                    f"{q.id}: '{part}' no es una opción válida "
                    f"(usar '{MULTI_ALL}' o opciones separadas por ';')"
                )
        return
    if q.question_type in (QuestionType.SINGLE_CHOICE, QuestionType.APPROVAL):
        if q.options and value not in q.options:
            raise AnswerError(f"{q.id}: '{value}' no es una opción válida de {q.options}")
        return
    if q.question_type == QuestionType.YES_NO:
        allowed = set(q.options) if q.options else _YES_NO
        if value.lower() not in {a.lower() for a in allowed}:
            raise AnswerError(f"{q.id}: '{value}' no es una respuesta sí/no válida")


def validate_answers(card: InterviewCard, ans: CardAnswers) -> None:
    """Lanza ``AnswerError`` si las respuestas no satisfacen la tarjeta."""
    if ans.card_id != card.card_id:
        raise AnswerError(
            f"respuestas para '{ans.card_id}' pero la tarjeta activa es '{card.card_id}'"
        )
    if ans.free_text and not card.allow_free_text:
        raise AnswerError(f"{card.card_id}: esta tarjeta no acepta texto libre")

    by_id = {q.id: q for q in card.questions}
    seen: set[str] = set()
    for a in ans.answers:
        q = by_id.get(a.question_id)
        if q is None:
            raise AnswerError(f"pregunta desconocida: {a.question_id}")
        if a.question_id in seen:
            raise AnswerError(f"respuesta duplicada: {a.question_id}")
        seen.add(a.question_id)
        if not str(a.value).strip():
            raise AnswerError(f"{a.question_id}: respuesta vacía")
        _check_choice(q, str(a.value))

    # Las requeridas deben estar respondidas; el texto libre (contrapropuesta)
    # es el único camino alternativo válido.
    missing = [q.id for q in card.questions if q.required and q.id not in seen]
    if missing and not ans.free_text.strip():
        raise AnswerError(
            f"faltan respuestas requeridas: {', '.join(missing)} "
            "(o escribe una contrapropuesta en texto libre)"
        )
