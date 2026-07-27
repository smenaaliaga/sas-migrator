"""Render lean de tarjetas de entrevista para terminal — funciones puras.

El estilo lean vive aquí y solo aquí (y en los builders): sin recaps, opciones
numeradas con "(Recomendado)", evidencia como líneas cortas, el default por
Enter. Testeado por snapshot para proteger el formato por regresión.
"""

from __future__ import annotations

import textwrap
from typing import Any

CardDict = dict[str, Any]


FREE_TEXT_HINT = "  (texto libre también vale: se registra como comentario/contrapropuesta)"

# Ancho de la regla y de los enunciados. Un enunciado de 200 caracteres en una
# sola línea es ilegible en cualquier terminal: se pliega, no se trunca.
WIDTH = 78
# El cuerpo de una pregunta (SQL, evidencia, opciones) cuelga a la derecha del
# enunciado; el prompt vuelve al margen para que la línea que se escribe no se
# confunda con lo que se está leyendo.
BODY = "      "


def _wrap(text: str, initial: str, subsequent: str) -> list[str]:
    """Pliega respetando los saltos de línea que el texto ya traía."""
    out: list[str] = []
    for i, para in enumerate(text.splitlines() or [""]):
        first = initial if i == 0 else subsequent
        if not para.strip():
            continue
        out.extend(
            textwrap.wrap(
                para,
                width=WIDTH,
                initial_indent=first,
                subsequent_indent=subsequent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [first.rstrip()]
        )
    return out


def _rule(title: str, tag: str) -> str:
    """Regla de título con el progreso anclado a la derecha."""
    left = f"── {title} "
    right = f" {tag} ──" if tag else "──"
    return left + "─" * max(2, WIDTH - len(left) - len(right)) + right


def render_card_header(card: CardDict) -> str:
    """Encabezado de la tarjeta: error de validación, transición y título."""
    lines: list[str] = []
    if card.get("validation_error"):
        lines.append(f"⚠ Respuesta no válida: {card['validation_error']}")
        lines.append("")
    progress = card.get("progress") or {}
    tag = f"[{progress['index']}/{progress['total']}]" if progress.get("total") else ""
    lines.append(_rule(card.get("title", ""), tag))
    if card.get("transition"):
        lines.extend(_wrap(card["transition"], "  ", "  "))
    return "\n".join(lines)


def render_question(
    question: CardDict, number: int | None = None, total: int | None = None
) -> str:
    """Una pregunta: enunciado, código, evidencia, opciones y el default.

    ``number``/``total`` numeran la pregunta dentro de una tarjeta que agrupa
    varias (las consultas de inspección): sin la posición a la vista, siete
    preguntas seguidas se leen como una sola pared de texto.
    """
    prefix = f"{number}/{total} · " if number and total and total > 1 else ""
    lines = _wrap(question["text"], "  " + prefix, "  " + " " * len(prefix))
    for code_line in (question.get("context") or "").splitlines():
        lines.append(f"{BODY}│ {code_line}".rstrip())
    for ev in question.get("evidence", []):
        lines.extend(_wrap(ev, f"{BODY}· ", f"{BODY}  "))
    options = question.get("options", [])
    recommended = question.get("recommended_default")
    for i, opt in enumerate(options, start=1):
        marker = "  (Recomendado)" if opt == recommended else ""
        lines.append(f"{BODY}{i}. {opt}{marker}")
    if recommended and recommended not in options:
        lines.append(f"{BODY}[Enter = {recommended}]")
    elif recommended:
        lines.append(f"{BODY}[Enter = opción recomendada: {recommended}]")
    return "\n".join(lines)


def render_card(card: CardDict) -> str:
    """Texto de una tarjeta completa para la terminal."""
    lines = [render_card_header(card)]
    questions = card.get("questions", [])
    for i, q in enumerate(questions, start=1):
        lines.append("")
        lines.append(render_question(q, i, len(questions)))
    if card.get("allow_free_text"):
        lines.append("")
        lines.append(FREE_TEXT_HINT)
    return "\n".join(lines)


def parse_answer(question: CardDict, raw: str) -> str | None:
    """Interpreta la entrada del usuario para UNA pregunta.

    Devuelve el valor de respuesta, o None si el texto no corresponde a una
    opción (el caller lo trata como texto libre).
    """
    text = raw.strip()
    options = question.get("options", [])
    if not text:
        return question.get("recommended_default") or None
    if options:
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1]
        for opt in options:
            if opt.lower() == text.lower():
                return opt
        if question.get("question_type") == "multi_choice":
            return text  # "todos" o lista "a; b" — la valida el grafo
        return None
    return text


def default_card_answers(card: CardDict) -> CardDict:
    """Respuestas por el camino recomendado (guion 'default: recommended')."""
    answers = []
    for q in card.get("questions", []):
        if q.get("question_type") == "multi_choice":
            value = "todos"
        elif q.get("options"):
            value = q.get("recommended_default") or q["options"][0]
        else:
            value = q.get("recommended_default") or "sin respuesta"
        answers.append({"question_id": q["id"], "value": value})
    return {"card_id": card["card_id"], "answers": answers, "free_text": ""}


def answers_from_script(card: CardDict, script: CardDict) -> CardDict:
    """Respuestas desde un guion YAML (``--answers-file``).

    Formato del guion::

        default: recommended        # tarjetas no listadas → camino recomendado
        answers:
          B1-initial:
            Q-001: "Flujo de ventas mensuales"
            Q-003: "no"
            free_text: "comentario opcional"

    Sin ``default: recommended``, una tarjeta no listada es un error explícito.
    """
    spec = (script.get("answers") or {}).get(card["card_id"])
    if spec is None:
        if str(script.get("default", "")).lower() == "recommended":
            return default_card_answers(card)
        raise KeyError(
            f"la tarjeta '{card['card_id']}' no está en el guion y no hay "
            "'default: recommended'"
        )
    free_text = str(spec.get("free_text", ""))
    answers = [
        {"question_id": qid, "value": str(value)}
        for qid, value in spec.items()
        if qid != "free_text"
    ]
    # Preguntas no cubiertas por el guion → default recomendado si existe.
    covered = {a["question_id"] for a in answers}
    for q in card.get("questions", []):
        if q["id"] not in covered and q.get("recommended_default"):
            answers.append({"question_id": q["id"], "value": q["recommended_default"]})
    return {"card_id": card["card_id"], "answers": answers, "free_text": free_text}
