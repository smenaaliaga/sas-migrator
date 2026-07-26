"""CLI de referencia — `sas-migrator run|resume|status`.

Cliente mínimo del grafo para Etapa 1 (stub_mode). En Etapa 3 esta CLI se
convierte en el cliente de referencia del servidor MCP y renderiza los
interrupts de entrevista.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Migrador SAS EG → Python (v2, LangGraph)")

CHECKPOINT_FILE = "graph_checkpoint.sqlite"
THREAD_ID = "migration"  # una migración por workspace


def _utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _graph_with_checkpointer(workspace: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from sas_migrator.graph.builder import build_graph

    state_dir = workspace / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / CHECKPOINT_FILE), check_same_thread=False)
    return build_graph(checkpointer=SqliteSaver(conn))


def _config() -> dict:
    return {"configurable": {"thread_id": THREAD_ID}}


def _report(result: dict) -> None:
    if result.get("done"):
        typer.echo("✅ Migración base completa (fases 0-8, gates en verde).")
    else:
        gate = result.get("last_gate") or {}
        typer.echo(f"⛔ Bloqueado en gate {gate.get('phase', '?')}:")
        for err in gate.get("errors", [])[:10]:
            typer.echo(f"   - {err}")
        raise typer.Exit(code=1)


@app.command()
def run(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace (input/, state/, output/)"),
    egp: Path = typer.Option(None, help="Ruta al .egp (default: único .egp en input/egp/)"),
    stub: bool = typer.Option(True, help="Etapa 1: nodos LLM como stubs deterministas"),
) -> None:
    """Corre la migración completa desde la fase 0."""
    _utf8_stdout()
    from sas_migrator.graph.builder import initial_state

    workspace = workspace.resolve()
    if egp is None:
        candidates = sorted((workspace / "input" / "egp").glob("*.egp"))
        if len(candidates) != 1:
            typer.echo(f"Se esperaba exactamente un .egp en input/egp/, hay {len(candidates)}.")
            raise typer.Exit(code=2)
        egp = candidates[0]

    graph = _graph_with_checkpointer(workspace)
    result = graph.invoke(initial_state(workspace, egp, stub_mode=stub), _config())
    _report(result)


@app.command()
def resume(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
) -> None:
    """Reanuda una migración interrumpida desde el checkpoint."""
    _utf8_stdout()
    graph = _graph_with_checkpointer(workspace.resolve())
    result = graph.invoke(None, _config())
    _report(result)


@app.command()
def status(
    workspace: Path = typer.Option(Path.cwd(), help="Raíz del workspace"),
) -> None:
    """Muestra fase actual y estado de gates desde los artefactos en disco."""
    _utf8_stdout()
    ms_path = workspace.resolve() / "state" / "migration_state.json"
    if not ms_path.exists():
        typer.echo("Sin migración iniciada (no existe state/migration_state.json).")
        raise typer.Exit(code=0)
    ms = json.loads(ms_path.read_text(encoding="utf-8"))
    typer.echo(f"Proyecto: {ms.get('project_name', '?')}")
    typer.echo(f"EGP: {ms.get('egp_file', '?')}")
    typer.echo(f"Fase actual: {ms.get('current_phase', '?')}")


if __name__ == "__main__":
    app()
