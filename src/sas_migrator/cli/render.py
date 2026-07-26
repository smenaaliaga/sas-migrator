"""Render lean de tarjetas de entrevista para terminal — funciones puras.

El estilo lean vive aquí y solo aquí (y en los builders): sin recaps, opciones
numeradas con "(Recomendado)", evidencia como líneas cortas, el default por
Enter. Testeado por snapshot para proteger el formato por regresión.
"""

from __future__ import annotations

from typing import Any

CardDict = dict[str, Any]


def render_card(card: CardDict) -> str:
    """Texto de una tarjeta para la terminal."""
    lines: list[str] = []
    if card.get("validation_error"):
        lines.append(f"⚠ Respuesta no válida: {card['validation_error']}")
        lines.append("")
    if card.get("transition"):
        lines.append(card["transition"])
    progress = card.get("progress")
    title = card.get("title", "")
    if progress and progress.get("total"):
        title += f"  [{progress['index']}/{progress['total']}]"
    lines.append(f"── {title} " + "─" * max(0, 60 - len(title)))
    for q in card.get("questions", []):
        lines.append("")
        lines.append(q["text"])
        for ev in q.get("evidence", []):
            lines.append(f"    · {ev}")
        options = q.get("options", [])
        recommended = q.get("recommended_default")
        for i, opt in enumerate(options, start=1):
            marker = "  (Recomendado)" if opt == recommended else ""
            lines.append(f"  {i}. {opt}{marker}")
        if not options and recommended:
            lines.append(f"  [Enter = {recommended}]")
        elif recommended and recommended not in options:
            lines.append(f"  [Enter = {recommended}]")
        elif recommended:
            lines.append(f"  [Enter = opción recomendada: {recommended}]")
    if card.get("allow_free_text"):
        lines.append("")
        lines.append("  (texto libre también vale: se registra como comentario/contrapropuesta)")
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
