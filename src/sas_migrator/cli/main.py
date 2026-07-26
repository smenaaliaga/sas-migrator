"""CLI de referencia — `sas-migrator run|resume|status|serve`.

Cliente de referencia de la Etapa 3: usa `MigrationSession` in-process (la
misma capa que envuelven las tools MCP) y renderiza los interrupts de
entrevista en terminal con el estilo lean. `serve` expone la sesión como
servidor MCP por stdio para VS Code / Claude Code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
import yaml

from sas_migrator.cli.render import answers_from_script, parse_answer, render_card

app = typer.Typer(help="Migrador SAS EG → Python (v2, LangGraph)")


def _utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _session(workspace: Path):
    from sas_migrator.service import MigrationSession

    return MigrationSession(workspace.resolve())


def _echo_messages(result) -> None:
    for msg in result.messages:
        typer.echo(msg)


def _prompt_card(card: dict) -> dict:
    """Pregunta una tarjeta en terminal; Enter = default recomendado."""
    typer.echo("")
    typer.echo(render_card(card))
    answers: list[dict] = []
    free_parts: list[str] = []
    for q in card.get("questions", []):
        raw = typer.prompt(f"  {q['id']} >", default="", show_default=False)
        value = parse_answer(q, raw)
        if value is not None:
            answers.append({"question_id": q["id"], "value": value})
        elif raw.strip():
            free_parts.append(raw.strip())
    return {"card_id": card["card_id"], "answers": answers, "free_text": "\n".join(free_parts)}


def _drive(session, result, script: dict | None):
    """Responde interrupts hasta terminar (interactivo o por guion)."""
    _echo_messages(result)
    while result.pending_card is not None:
        card = result.pending_card.model_dump(mode="json")
        if script is not None:
            payload = answers_from_script(card, script)
        else:
            payload = _prompt_card(card)
        result = session.answer(payload)
        _echo_messages(result)
    return result


def _report(result) -> None:
    from sas_migrator.service import SessionStatus

    if result.status == SessionStatus.COMPLETED:
        return  # el mensaje final ya salió por messages
    if result.status == SessionStatus.BLOCKED:
        typer.echo(f"⛔ Bloqueado (fase {result.phase}):")
        for err in result.gate_errors[:10]:
            typer.echo(f"   - {err}")
        raise typer.Exit(code=1)


def _load_script(answers_file: Path | None) -> dict | None:
    if answers_file is None:
        return None
    return yaml.safe_load(answers_file.read_text(encoding="utf-8")) or {}


@app.command()
def run(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace (input/, state/, output/)"),
    egp: Path = typer.Option(None, help="Ruta al .egp (default: único .egp en input/egp/)"),
    stub: bool = typer.Option(
        True, help="Stubs deterministas (CI). --no-stub = entrevistas reales."
    ),
    answers_file: Path = typer.Option(
        None, help="Guion YAML de respuestas para --no-stub (ver cli/render.py)"
    ),
) -> None:
    """Corre la migración completa desde la fase 0."""
    _utf8_stdout()
    session = _session(workspace)
    try:
        result = session.start(egp, stub_mode=stub)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=2) from exc
    result = _drive(session, result, _load_script(answers_file))
    _report(result)


@app.command()
def resume(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
    answers_file: Path = typer.Option(None, help="Guion YAML de respuestas"),
) -> None:
    """Reanuda una migración interrumpida desde el checkpoint (incluida una
    entrevista a mitad de camino)."""
    _utf8_stdout()
    session = _session(workspace)
    result = _drive(session, session.resume(), _load_script(answers_file))
    _report(result)


@app.command()
def status(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
) -> None:
    """Muestra fase actual, gates y entrevista pendiente."""
    _utf8_stdout()
    from sas_migrator.service import SessionStatus

    ws = workspace.resolve()
    ms_path = ws / "state" / "migration_state.json"
    if ms_path.exists():
        ms = json.loads(ms_path.read_text(encoding="utf-8"))
        typer.echo(f"Proyecto: {ms.get('project_name', '?')}")
        typer.echo(f"EGP: {ms.get('egp_file', '?')}")
        typer.echo(f"Fase actual: {ms.get('current_phase', '?')}")
    result = _session(ws).status()
    if result.status == SessionStatus.NOT_STARTED:
        typer.echo("Sin migración iniciada.")
        raise typer.Exit(code=0)
    typer.echo(f"Estado: {result.status.value}")
    if result.pending_card is not None:
        typer.echo(f"Entrevista pendiente: {result.pending_card.card_id}")


@app.command()
def serve(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
) -> None:
    """Levanta el servidor MCP (stdio) sobre este workspace."""
    _utf8_stdout()
    from sas_migrator.mcp_server.server import serve_workspace

    serve_workspace(workspace.resolve())


if __name__ == "__main__":
    app()
