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

from sas_migrator.cli.render import (
    FREE_TEXT_HINT,
    answers_from_script,
    parse_answer,
    render_card_header,
    render_question,
)

app = typer.Typer(help="Migrador SAS EG → Python (v2, LangGraph)")


def _utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    """Entry point de la consola — reconfigura stdout ANTES de que Click hable.

    Hacerlo dentro de cada comando dejaba afuera lo primero que se tipea:
    `sas-migrator --help` reventaba con UnicodeEncodeError en una consola
    cp1252, porque la flecha del help del grupo se renderiza sin pasar por
    ningún comando.
    """
    _utf8_stdout()
    app()


def _session(workspace: Path):
    from sas_migrator.service import MigrationSession

    return MigrationSession(workspace.resolve())


def _echo_messages(result) -> None:
    for msg in result.messages:
        typer.echo(msg)


def _prompt_card(card: dict) -> dict:
    """Pregunta una tarjeta en terminal; Enter = default recomendado.

    De a una: cada pregunta se muestra pegada a su prompt. Volcar la tarjeta
    entera y después encadenar los prompts deja al usuario emparejando IDs
    (`Q-B2-1 >`) con enunciados que quedaron pantallas más arriba.
    """
    typer.echo("")
    typer.echo(render_card_header(card))
    if card.get("allow_free_text"):
        typer.echo(FREE_TEXT_HINT)
    answers: list[dict] = []
    free_parts: list[str] = []
    questions = card.get("questions", [])
    for i, q in enumerate(questions, start=1):
        typer.echo("")
        typer.echo(render_question(q, i, len(questions)))
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
        False, help="Stubs deterministas sin LLM ni entrevistas (CI, smoke test)."
    ),
    answers_file: Path = typer.Option(
        None, help="Guion YAML de respuestas (ver cli/render.py)"
    ),
) -> None:
    """Corre la migración completa desde la fase 0.

    Por defecto es una corrida REAL: entrevistas y LLM. `--stub` es el modo
    determinista de CI. El default era el inverso, y contradecía a los otros
    dos frentes de la misma sesión (`MigrationSession.start` y la tool MCP
    `start_migration` ya arrancaban en real): migrar es lo que hace esta
    herramienta, y era lo único que exigía un flag.
    """
    session = _session(workspace)
    typer.echo(
        "▶ modo STUB: sin LLM ni entrevistas (determinista)"
        if stub
        else "▶ modo REAL: entrevistas y LLM — se detiene en la primera pregunta"
    )
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
    session = _session(workspace)
    result = _drive(session, session.resume(), _load_script(answers_file))
    _report(result)


@app.command()
def rewind(
    phase: int = typer.Option(..., help="Fase a rehacer desde cero (0-8)"),
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
    answers_file: Path = typer.Option(None, help="Guion YAML de respuestas"),
    backup: bool = typer.Option(True, help="Copia el checkpoint antes de rebobinar"),
) -> None:
    """Rebobina al inicio de una fase y la vuelve a correr desde cero.

    Para rehacer una entrevista ya contestada a medias: `resume` la continúa
    donde iba (las respuestas viven en el checkpointer), `rewind` la reinicia.
    Las fases anteriores no se recalculan — sus artefactos en `state/` quedan.
    """
    ws = workspace.resolve()
    if backup:
        import shutil

        from sas_migrator.service.session import CHECKPOINT_FILE

        src = ws / "state" / CHECKPOINT_FILE
        if src.exists():
            dst = src.with_suffix(src.suffix + ".bak")
            shutil.copy2(src, dst)
            typer.echo(f"↩ checkpoint respaldado en {dst.name}")

    session = _session(ws)
    try:
        result = session.rewind_to_phase(phase)
    except (ValueError, LookupError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=2) from exc
    result = _drive(session, result, _load_script(answers_file))
    _report(result)


@app.command()
def reset(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
    keep_output: bool = typer.Option(
        False, help="Conservar output/ (borra solo state/ y el checkpoint)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar (scripts, CI)"),
) -> None:
    """Borra los artefactos derivados y deja el workspace listo para empezar de cero.

    `rewind` rehace UNA fase; esto tira todo: `state/` (artefactos y
    checkpoint) y `output/`. `input/` y `project_config.yaml` no se tocan.
    """
    from sas_migrator.core.utils.workspace_reset import (
        PRESERVED,
        apply_reset,
        human_size,
        plan_reset,
    )

    try:
        plan = plan_reset(workspace, keep_output=keep_output)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=2) from exc

    if plan.is_empty:
        typer.echo(f"Nada que borrar en {plan.workspace} — el workspace ya está limpio.")
        raise typer.Exit(code=0)

    typer.echo(f"Workspace: {plan.workspace}")
    if plan.phase is not None:
        typer.echo(f"Fase alcanzada: {plan.phase}")
    typer.echo("Se van a BORRAR:")
    for target in plan.targets:
        typer.echo(f"  {target.name + '/':10} {target.files:4} archivos, {human_size(target.size)}")
    typer.echo(f"Se conservan: {', '.join(PRESERVED)}")
    # El conteo de archivos no mide lo que duele: lo caro de un reset es volver
    # a contestar, no volver a calcular.
    if plan.decisions:
        typer.echo(
            f"⚠ Incluye {len(plan.decisions)} artefacto(s) de decisiones tuyas que "
            "habrá que volver a contestar:"
        )
        for name in plan.decisions:
            typer.echo(f"    · {name}")

    if not yes:
        typer.confirm("¿Borrar y empezar de cero?", default=False, abort=True)

    done = apply_reset(plan)
    typer.echo(f"🗑 Borrado: {', '.join(f'{n}/' for n in done)} — listo para `run`.")


@app.command()
def status(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
) -> None:
    """Muestra fase actual, gates y entrevista pendiente."""
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
def iterate(
    describe: str = typer.Option(..., help="Qué ajustar (instrucción de la iteración)"),
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
    nodes: str = typer.Option("", help="node_ids afectados, separados por coma"),
    request_type: str = typer.Option(
        "enhancement",
        help="bug_fix | enhancement | postponed_improvement | new_requirement | data_change | context_update",
    ),
) -> None:
    """Itera sobre una migración completada (Fase 9, sub-grafo con gate)."""
    session = _session(workspace)
    node_ids = [n.strip() for n in nodes.split(",") if n.strip()]
    result = session.iterate(describe, request_type=request_type, affected_nodes=node_ids)
    for note in result.get("notes", []):
        typer.echo(note)
    if result.get("done"):
        typer.echo(f"✅ Iteración {result['entry_id']} cerrada (ciclo {result['cycle']}).")
    else:
        typer.echo(f"⛔ Iteración {result['entry_id']} bloqueada:")
        for err in result.get("errors", [])[:10]:
            typer.echo(f"   - {err}")
        raise typer.Exit(code=1)


@app.command()
def serve(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
) -> None:
    """Levanta el servidor MCP (stdio) sobre este workspace."""
    from sas_migrator.mcp_server.server import serve_workspace

    serve_workspace(workspace.resolve())


if __name__ == "__main__":
    main()
