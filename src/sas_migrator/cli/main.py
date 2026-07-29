"""CLI de referencia — `sas-migrator doctor|run|resume|rewind|reset|status|iterate|serve`.

Cliente de referencia: usa `MigrationSession` in-process (la misma capa que
envuelven las tools MCP) y renderiza los interrupts de entrevista en terminal
con el estilo lean. `serve` expone la sesión como servidor MCP por stdio para
VS Code / Claude Code.

Dos reglas de este archivo:

* El docstring de un comando ES su `--help`. Va el qué y el cuándo, en dos o
  tres líneas; el por qué de una decisión va en un comentario como este, que el
  usuario no tiene que leer para usar el comando.
* Los errores van a stderr con código de salida estable (ver `EXIT_CODES`), y
  ninguno termina sin decir qué hacer a continuación.
"""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path

import click
import typer
import yaml

from sas_migrator.cli.render import (
    FREE_TEXT_HINT,
    PHASE_NAMES,
    answers_from_script,
    parse_answer_and_note,
    render_card_header,
    render_phases,
    render_question,
)

# Códigos de salida, estables para scripts y CI.
EXIT_OK = 0
EXIT_BLOCKED = 1  # el pipeline corrió y algo bloquea (gate, chequeo, iteración)
EXIT_USAGE = 2  # el entorno o los argumentos no permiten ni empezar
EXIT_INTERRUPTED = 130  # Ctrl-C

# Grupos del `--help`. Ocho comandos sin agrupar se leen como una lista de
# supermercado; agrupados se ve de un saque cuál es el camino normal.
PANEL_RUN = "Corrida"
PANEL_RECOVER = "Recuperación"
PANEL_INSPECT = "Inspección"
PANEL_INTEGRATION = "Integración"

app = typer.Typer(
    help="Migrador de proyectos SAS Enterprise Guide (.egp) a Python.",
    epilog=(
        "Camino normal: [bold]doctor[/bold] → [bold]run[/bold] → contestar las "
        "entrevistas → [bold]status[/bold].\n\n"
        "El workspace por defecto es el directorio actual, así que lo habitual "
        "es pararse en la migración y omitir --workspace.\n\n"
        "Salida: 0 ok · 1 bloqueado · 2 error de uso o entorno · 130 interrumpido."
    ),
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if not value:
        return
    from importlib.metadata import PackageNotFoundError, version

    try:
        typer.echo(f"sas-migrator {version('sas-migrator')}")
    except PackageNotFoundError:  # corriendo desde el repo sin instalar
        typer.echo("sas-migrator (sin instalar)")
    raise typer.Exit(code=EXIT_OK)


@app.callback()
def _root(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Mostrar la versión y salir.",
    ),
) -> None:
    # Sin docstring a propósito: el help del grupo es el `help=` de Typer(), que
    # además lleva epilog.
    pass


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    """Entry point de la consola — reconfigura stdout ANTES de que Click hable.

    Hacerlo dentro de cada comando dejaba afuera lo primero que se tipea:
    `sas-migrator --help` reventaba con UnicodeEncodeError en una consola
    cp1252, porque la flecha del help del grupo se renderiza sin pasar por
    ningún comando.
    """
    _utf8_stdout()
    app()


# ── Opciones compartidas ─────────────────────────────────────────────────────
#
# `--workspace` se resuelve en el cuerpo del comando, no como default del
# parámetro: un `Path.cwd()` en la firma se congela al importar el módulo.


def _ws_option() -> Path | None:
    return typer.Option(
        None,
        "--workspace",
        "-w",
        show_default="directorio actual",
        help="Raíz del workspace (input/, state/, output/).",
    )


def _answers_option() -> Path | None:
    return typer.Option(
        None,
        "--answers-file",
        "-a",
        exists=True,
        dir_okay=False,
        help="Guion YAML de respuestas para correr sin interacción (CI, replay).",
    )


def _resolve_ws(workspace: Path | None) -> Path:
    return (workspace or Path.cwd()).resolve()


class RequestType(str, Enum):
    """Tipos de iteración (fase 9). Enum y no texto libre: así el CLI valida
    antes de arrancar el sub-grafo y el shell puede completar el valor."""

    bug_fix = "bug_fix"
    enhancement = "enhancement"
    postponed_improvement = "postponed_improvement"
    new_requirement = "new_requirement"
    data_change = "data_change"
    context_update = "context_update"


# ── Salida ───────────────────────────────────────────────────────────────────


def _err(message: str, *, hint: str = "") -> None:
    """Un error a stderr. Mezclarlo con stdout arruina `status --json | jq`."""
    typer.secho(message, err=True, fg=typer.colors.RED)
    if hint:
        typer.secho(f"  → {hint}", err=True, fg=typer.colors.YELLOW)


def _die(message: str, *, hint: str = "", code: int = EXIT_USAGE) -> typer.Exit:
    _err(message, hint=hint)
    return typer.Exit(code=code)


def _next_step(cmd: str, why: str = "") -> None:
    typer.echo("")
    typer.secho(f"Próximo paso: {cmd}", fg=typer.colors.CYAN, bold=True)
    if why:
        typer.echo(f"  {why}")


def _session(workspace: Path):
    from sas_migrator.service import MigrationSession

    return MigrationSession(workspace)


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
        value, note = parse_answer_and_note(q, raw)
        if value is not None:
            answers.append({"question_id": q["id"], "value": value})
            if note:
                free_parts.append(note)
        elif raw.strip():
            free_parts.append(raw.strip())
    return {"card_id": card["card_id"], "answers": answers, "free_text": "\n".join(free_parts)}


def _drive(session, result, script: dict | None):
    """Responde interrupts hasta terminar (interactivo o por guion).

    Ctrl-C a mitad de una entrevista no es un accidente que haya que castigar
    con un traceback: lo contestado vive en el checkpointer y `resume` retoma
    exactamente ahí. Decirlo es la diferencia entre una pausa y un susto.
    """
    _echo_messages(result)
    try:
        while result.pending_card is not None:
            card = result.pending_card.model_dump(mode="json")
            if script is not None:
                payload = answers_from_script(card, script)
            else:
                payload = _prompt_card(card)
            result = session.answer(payload)
            _echo_messages(result)
    except (KeyboardInterrupt, click.exceptions.Abort):
        typer.echo("")
        typer.secho("⏸ Interrumpido.", fg=typer.colors.YELLOW)
        typer.echo("   Lo contestado quedó en el checkpoint.")
        _next_step("sas-migrator resume", "retoma en la misma pregunta.")
        raise typer.Exit(code=EXIT_INTERRUPTED) from None
    except KeyError as exc:  # guion incompleto: falta una tarjeta
        raise _die(
            f"Guion de respuestas incompleto: {exc.args[0] if exc.args else exc}",
            hint="Agregá la tarjeta al YAML o poné `default: recommended`.",
        ) from exc
    return result


def _report(result) -> None:
    from sas_migrator.service import SessionStatus

    if result.status == SessionStatus.COMPLETED:
        return  # el mensaje final ya salió por messages
    if result.status == SessionStatus.BLOCKED:
        _err(f"⛔ Bloqueado en la fase {result.phase} ({PHASE_NAMES.get(result.phase, '?')}):")
        for err in result.gate_errors[:10]:
            _err(f"   - {err}")
        if len(result.gate_errors) > 10:
            _err(f"   … y {len(result.gate_errors) - 10} más")
        _next_step("sas-migrator status", "muestra el detalle y qué falta resolver.")
        raise typer.Exit(code=EXIT_BLOCKED)


def _load_script(answers_file: Path | None) -> dict | None:
    if answers_file is None:
        return None
    try:
        return yaml.safe_load(answers_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise _die(f"El guion de respuestas no es YAML válido: {exc}") from exc


# ── Corrida ──────────────────────────────────────────────────────────────────


@app.command(rich_help_panel=PANEL_RUN)
def doctor(
    workspace: Path = _ws_option(),
    stub: bool = typer.Option(False, help="Verificar solo lo que el modo --stub necesita."),
) -> None:
    """Verifica que el workspace pueda correr, antes de gastar tiempo y tokens.

    Revisa estructura (input/egp/, referencias), project_config.yaml, la
    credencial del proveedor configurado —en los mismos .env donde la busca una
    corrida real— y los extras instalados. No toca la red.

    Sale 0 si nada bloquea (los ⚠ no bloquean: describen lo que faltará más
    tarde), 1 si hay algo que impide correr.
    """
    from sas_migrator.service.preflight import FAIL, OK, WARN, run_checks

    ws = _resolve_ws(workspace)
    report = run_checks(ws, stub=stub)
    icon = {OK: "✅", WARN: "⚠ ", FAIL: "⛔"}
    color = {OK: typer.colors.GREEN, WARN: typer.colors.YELLOW, FAIL: typer.colors.RED}

    typer.echo(f"Workspace: {ws}")
    typer.echo("")
    for check in report.checks:
        line = f"{icon[check.status]} {check.name:26} {check.detail}"
        typer.secho(line.rstrip(), fg=color[check.status])
        if check.hint and check.status != OK:
            for hint_line in check.hint.splitlines():
                typer.echo(f"     {hint_line}")
    typer.echo("")

    if not report.ok:
        _err(f"⛔ {len(report.failures)} chequeo(s) bloquean la corrida.")
        raise typer.Exit(code=EXIT_BLOCKED)
    if report.warnings:
        typer.secho(
            f"✅ Se puede correr, con {len(report.warnings)} advertencia(s).",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho("✅ Todo en orden.", fg=typer.colors.GREEN)
    # Con checkpoint, `run` arranca de cero sobre una migración que ya existe:
    # el próximo paso honesto es mirar dónde quedó.
    if any(c.name == "checkpoint" and "en curso" in c.detail for c in report.checks):
        _next_step("sas-migrator status", "hay una migración en curso.")
    else:
        _next_step("sas-migrator run")


@app.command(rich_help_panel=PANEL_RUN)
def run(
    workspace: Path = _ws_option(),
    egp: Path = typer.Option(
        None, exists=True, dir_okay=False,
        show_default="el único .egp en input/egp/",
        help="Ruta al .egp a migrar.",
    ),
    stub: bool = typer.Option(
        False, help="Stubs deterministas: sin LLM ni entrevistas (CI, smoke test)."
    ),
    answers_file: Path = _answers_option(),
    check: bool = typer.Option(
        True, "--check/--no-check", help="Verificar la credencial antes de arrancar."
    ),
) -> None:
    """Corre la migración completa desde la fase 0.

    Es una corrida REAL: entrevistas y LLM. Se detiene en la primera pregunta,
    así que una corrida arrancada por error no gasta más que las fases 0-1.

    Con --answers-file corre sin interacción. Con --stub corre el pipeline
    entero sin proveedor ni preguntas.
    """
    # El default era `--stub`, y contradecía a los otros dos frentes de la misma
    # sesión (`MigrationSession.start` y la tool MCP `start_migration` ya
    # arrancaban en real): migrar es lo que hace esta herramienta, y era lo único
    # que exigía un flag.
    ws = _resolve_ws(workspace)
    typer.echo(
        "▶ modo STUB: sin LLM ni entrevistas (determinista)"
        if stub
        else "▶ corrida de migración: entrevistas y LLM — se detiene en la primera pregunta"
    )
    if check and not stub:
        # Falta de credencial es el fallo más caro de descubrir tarde: pega en la
        # fase 2, después de la entrevista inicial ya contestada.
        from sas_migrator.service.preflight import credential_report

        report = credential_report(ws)
        if not report.ok:
            for failed in report.failures:
                _err(f"⛔ {failed.name}: {failed.detail}")
                for hint_line in failed.hint.splitlines():
                    _err(f"   {hint_line}")
            raise _die(
                "No se puede correr la migración sin credencial.",
                hint="`sas-migrator doctor` lista todo lo que falta; --no-check la saltea.",
            )

    session = _session(ws)
    try:
        result = session.start(egp, stub_mode=stub)
    except FileNotFoundError as exc:
        raise _die(str(exc), hint="`sas-migrator doctor` o `run --egp <ruta>`.") from exc
    result = _drive(session, result, _load_script(answers_file))
    _report(result)


@app.command(rich_help_panel=PANEL_RUN)
def iterate(
    description: str = typer.Argument(
        None, metavar="DESCRIPCION", help="Qué ajustar, en una frase."
    ),
    workspace: Path = _ws_option(),
    node: list[str] = typer.Option(
        [], "--node", "-n", help="node_id afectado. Repetible: -n A -n B."
    ),
    request_type: RequestType = typer.Option(
        RequestType.enhancement, "--request-type", "-t", help="Tipo de pedido."
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Retoma la iteración que quedó a medias (corte)."
    ),
    describe: str = typer.Option(None, "--describe", hidden=True),
    nodes: str = typer.Option("", "--nodes", hidden=True),
) -> None:
    """Itera sobre una migración ya completada (fase 9).

    Re-traduce solo los nodos afectados y vuelve a correr auditoría y
    validación: una iteración no cierra sin re-validar. Si un corte dejó un
    ciclo a medias, `iterate --resume` lo retoma; arrancar un ciclo nuevo lo
    marca deferred (visible en iteration_log.json).

        sas-migrator iterate "corregir el redondeo de montos" -n CodeTask-3
    """
    # `--describe` y `--nodes "a,b"` son las formas viejas: siguen andando sin
    # figurar en el help para no enseñar dos maneras de lo mismo.
    text = description or describe
    if not text and not resume:
        raise _die(
            "Falta la descripción de la iteración.",
            hint='sas-migrator iterate "corregir el redondeo de montos"',
        )
    node_ids = list(node) + [n.strip() for n in nodes.split(",") if n.strip()]
    session = _session(_resolve_ws(workspace))
    try:
        result = session.iterate(
            text or "", request_type=request_type.value,
            affected_nodes=node_ids, resume=resume,
        )
    except LookupError as exc:
        raise _die(str(exc), hint='sas-migrator iterate "<qué ajustar>"') from exc
    for note in result.get("notes", []):
        typer.echo(note)
    if result.get("done"):
        typer.secho(
            f"✅ Iteración {result['entry_id']} cerrada (ciclo {result['cycle']}).",
            fg=typer.colors.GREEN,
        )
        return
    _err(f"⛔ Iteración {result['entry_id']} bloqueada:")
    for err in result.get("errors", [])[:10]:
        _err(f"   - {err}")
    raise typer.Exit(code=EXIT_BLOCKED)


# ── Recuperación ─────────────────────────────────────────────────────────────


@app.command(rich_help_panel=PANEL_RECOVER)
def resume(
    workspace: Path = _ws_option(),
    answers_file: Path = _answers_option(),
) -> None:
    """Retoma la migración donde quedó, incluida una entrevista a medio contestar.

    Las respuestas viven en el checkpoint, no en state/: retomar no repite lo
    que ya contestaste. Para volver a preguntar desde cero, `rewind`.

    Sobre un gate bloqueado, `resume` RE-EVALÚA el gate (los needs_human que
    marcaste resolved, el artefacto que corregiste) sin re-ejecutar la fase;
    si el arreglo exige rehacer la fase (p. ej. re-traducir nodos), eso es
    `rewind --phase N`.
    """
    ws = _resolve_ws(workspace)
    session = _session(ws)
    result = _drive(session, session.resume(), _load_script(answers_file))
    _report(result)


@app.command(rich_help_panel=PANEL_RECOVER)
def rewind(
    phase: int = typer.Option(..., "--phase", "-p", min=0, max=9, help="Fase a rehacer (0-9)."),
    workspace: Path = _ws_option(),
    answers_file: Path = _answers_option(),
    backup: bool = typer.Option(
        True, "--backup/--no-backup", help="Copiar el checkpoint a .bak antes de rebobinar."
    ),
) -> None:
    """Rehace una fase desde cero, descartando lo hecho desde su inicio.

    `resume` continúa una entrevista donde iba; `rewind --phase N` la reinicia
    desde la primera tarjeta. Las fases anteriores no se recalculan: sus
    artefactos en state/ quedan como están.
    """
    ws = _resolve_ws(workspace)
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
        raise _die(
            str(exc),
            hint="`sas-migrator status` muestra hasta qué fase llegó la migración.",
        ) from exc
    result = _drive(session, result, _load_script(answers_file))
    _report(result)


@app.command(rich_help_panel=PANEL_RECOVER)
def reset(
    workspace: Path = _ws_option(),
    keep_output: bool = typer.Option(
        False, help="Conservar output/ (borra solo state/ y el checkpoint)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar (scripts, CI)."),
) -> None:
    """Borra lo derivado y deja el workspace listo para empezar de cero.

    `rewind` rehace UNA fase; esto tira todo: state/ (artefactos y checkpoint) y
    output/. input/ y project_config.yaml no se tocan. Pide confirmación y antes
    lista qué decisiones humanas habrá que volver a contestar.
    """
    from sas_migrator.core.utils.workspace_reset import (
        PRESERVED,
        apply_reset,
        human_size,
        plan_reset,
    )

    ws = _resolve_ws(workspace)
    try:
        plan = plan_reset(ws, keep_output=keep_output)
    except ValueError as exc:
        raise _die(str(exc), hint="Corré `reset` parado en el workspace, o pasá -w.") from exc

    if plan.is_empty:
        typer.echo(f"Nada que borrar en {plan.workspace} — el workspace ya está limpio.")
        raise typer.Exit(code=EXIT_OK)

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
        typer.secho(
            f"⚠ Incluye {len(plan.decisions)} artefacto(s) de decisiones tuyas que "
            "habrá que volver a contestar:",
            fg=typer.colors.YELLOW,
        )
        for name in plan.decisions:
            typer.echo(f"    · {name}")

    if not yes:
        typer.confirm("¿Borrar y empezar de cero?", default=False, abort=True)

    done = apply_reset(plan)
    typer.echo(f"🗑 Borrado: {', '.join(f'{n}/' for n in done)}")
    _next_step("sas-migrator run")


# ── Inspección ───────────────────────────────────────────────────────────────


def _needs_human_items(state_dir: Path) -> list:
    from sas_migrator.core.utils.needs_human import unresolved

    try:
        return unresolved(state_dir)
    except Exception:  # cola corrupta: no es motivo para no mostrar el resto
        return []


def _trace_summary(state_dir: Path) -> dict:
    try:
        from sas_migrator.llm.trace import summarize

        return summarize(state_dir)
    except Exception:
        return {"calls": 0, "by_task": {}}


def _cost_summary(state_dir: Path) -> dict:
    """Totales de costo/tokens/cache de la corrida (desde el trace)."""
    try:
        from sas_migrator.core.config import load_project_config
        from sas_migrator.llm.costs import run_totals

        overrides = load_project_config(state_dir.parent).llm.prices
        return run_totals(state_dir, overrides)
    except Exception:
        return {}


def _confidence_summary(state_dir: Path) -> dict:
    """Distribución de confianza de las traducciones + veredictos del
    verificador — antes se descartaban y "qué tan confiable quedó esto" no
    tenía respuesta sin abrir 75 archivos."""
    out: dict = {}
    try:
        mapping = json.loads(
            (state_dir / "sas_python_mapping.json").read_text(encoding="utf-8")
        )
        dist: dict[str, int] = {}
        for m in mapping.get("mappings", []):
            c = str(m.get("confidence", "?"))
            dist[c] = dist.get(c, 0) + 1
        if dist:
            out["confidence"] = dist
    except Exception:
        pass
    try:
        reviews = json.loads(
            (state_dir / "translation_review.json").read_text(encoding="utf-8")
        ).get("reviews", [])
        verdicts: dict[str, int] = {}
        for r in reviews:
            v = str(r.get("verdict", "?"))
            verdicts[v] = verdicts.get(v, 0) + 1
        if verdicts:
            out["verify"] = verdicts
    except Exception:
        pass
    return out


@app.command(rich_help_panel=PANEL_INSPECT)
def status(
    workspace: Path = _ws_option(),
    as_json: bool = typer.Option(False, "--json", help="Salida JSON para scripts."),
) -> None:
    """Dónde quedó la migración: fases, gate bloqueado, needs_human y qué sigue.

    Muestra las 10 fases con su estado, el detalle del gate que bloquea, los
    items de la cola needs_human (que bloquean el gate de su fase hasta
    resolverse) y un resumen de las llamadas LLM.
    """
    from sas_migrator.service import SessionStatus

    ws = _resolve_ws(workspace)
    state_dir = ws / "state"
    result = _session(ws).status()

    project: dict = {}
    ms_path = state_dir / "migration_state.json"
    if ms_path.exists():
        try:
            project = json.loads(ms_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            project = {}

    started = result.status != SessionStatus.NOT_STARTED
    items = _needs_human_items(state_dir) if started else []
    trace = _trace_summary(state_dir) if started else {"calls": 0, "by_task": {}}
    conf = _confidence_summary(state_dir) if started else {}
    cost = _cost_summary(state_dir) if started else {}

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "workspace": str(ws),
                    "project_name": project.get("project_name"),
                    "egp_file": project.get("egp_file"),
                    "status": result.status.value,
                    "phase": result.phase,
                    "completed_phases": result.completed_phases,
                    "gate_errors": result.gate_errors,
                    "pending_card": (
                        result.pending_card.model_dump(mode="json")
                        if result.pending_card
                        else None
                    ),
                    "needs_human": [i.model_dump(mode="json") for i in items],
                    "llm": trace,
                    "cost": cost,
                    "translation": conf,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if not started:
        typer.echo(f"Workspace: {ws}")
        typer.echo("Sin migración iniciada.")
        _next_step("sas-migrator doctor", "verifica el workspace antes de arrancar.")
        raise typer.Exit(code=EXIT_OK)

    typer.echo(f"Proyecto: {project.get('project_name', '?')}")
    typer.echo(f"EGP:      {project.get('egp_file', '?')}")
    typer.echo(f"Estado:   {result.status.value}")
    typer.echo("")
    typer.echo(render_phases(result.phase, result.completed_phases))

    if result.gate_errors:
        # Los errores de gate por needs_human se colapsan a un renglón: el
        # detalle está en el bloque de abajo, y volcarlos dos veces convierte un
        # tablero en una pared de texto (20 items = 40 líneas idénticas).
        others = [e for e in result.gate_errors if "needs_human sin resolver" not in e]
        from_queue = len(result.gate_errors) - len(others)
        typer.echo("")
        typer.secho(f"⛔ Gate {result.phase} bloqueado:", fg=typer.colors.RED)
        for err in others[:10]:
            typer.echo(f"   - {err}")
        if len(others) > 10:
            typer.echo(f"   … y {len(others) - 10} más")
        if from_queue:
            typer.echo(f"   - {from_queue} item(s) de needs_human sin resolver (detalle abajo)")

    if items:
        typer.echo("")
        typer.secho(
            f"⚠ needs_human sin resolver ({len(items)}) — bloquean el gate de su fase:",
            fg=typer.colors.YELLOW,
        )
        for item in items[:10]:
            node = f" · {item.node_id}" if item.node_id else ""
            # El `detail` es lo que distingue un item de otro cuando veinte
            # comparten el mismo `reason` (`static_check_failed`, siempre).
            detail = (item.detail or "").strip().splitlines()
            head = f" — {detail[0][:70]}" if detail else ""
            typer.echo(f"   {item.id} (fase {item.phase}{node}) {item.reason}{head}")
        if len(items) > 10:
            typer.echo(f"   … y {len(items) - 10} más")
        typer.echo(f"   Se resuelven en {state_dir / 'needs_human.yaml'} (resolved: true).")

    if trace.get("calls"):
        by_task = trace["by_task"].values()
        agg = {k: sum(t.get(k, 0) for t in by_task) for k in ("ok", "needs_human", "errors")}
        seconds = sum(t.get("duration_ms", 0) for t in by_task) / 1000
        typer.echo("")
        typer.echo(
            f"LLM: {trace['calls']} llamadas · {agg['ok']} ok · "
            f"{agg['needs_human']} needs_human · {agg['errors']} error(es) · "
            f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
        )

    if cost.get("priced_calls") or cost.get("unpriced_calls"):
        total_in = (
            cost["input_tokens"] + cost["cache_read_tokens"] + cost["cache_write_tokens"]
        )
        cache_pct = (100 * cost["cache_read_tokens"] // total_in) if total_in else 0
        linea = (
            f"Costo: ~${cost['cost_usd']:.2f} · "
            f"{cost['input_tokens'] + cost['cache_read_tokens'] + cost['cache_write_tokens']:,} in / "
            f"{cost['output_tokens']:,} out · {cache_pct}% del input leído de cache"
        )
        if cost.get("unpriced_calls"):
            linea += f" · {cost['unpriced_calls']} llamada(s) sin precio conocido"
        typer.echo(linea)

    if conf.get("confidence"):
        orden = {"high": 0, "medium": 1, "low": 2}
        dist = " · ".join(
            f"{k} {v}"
            for k, v in sorted(conf["confidence"].items(), key=lambda kv: orden.get(kv[0], 9))
        )
        linea = f"Confianza traducción: {dist}"
        if conf.get("verify"):
            veredictos = " · ".join(f"{k} {v}" for k, v in sorted(conf["verify"].items()))
            linea += f"  |  verificador: {veredictos}"
        typer.echo(linea)

    if result.pending_card is not None:
        card = result.pending_card
        progress = card.progress
        tag = f"  [{progress.index}/{progress.total or '?'}]" if progress else ""
        typer.echo("")
        typer.echo(f"Entrevista pendiente: {card.card_id} — {card.title}{tag}")

    if result.status == SessionStatus.COMPLETED:
        _next_step('sas-migrator iterate "<qué ajustar>"', "la migración base está completa.")
    elif result.status == SessionStatus.WAITING_USER:
        _next_step("sas-migrator resume", "retoma en la pregunta pendiente.")
    elif items:
        _next_step(
            "sas-migrator resume",
            "re-evalúa el gate después de marcar los needs_human como resolved.",
        )
    else:
        _next_step(
            "sas-migrator resume",
            "re-evalúa el gate bloqueado; si hay que rehacer la fase, "
            "`rewind --phase N`.",
        )


# ── Integración ──────────────────────────────────────────────────────────────


@app.command(rich_help_panel=PANEL_INTEGRATION)
def serve(
    workspace: Path = _ws_option(),
) -> None:
    """Levanta el servidor MCP (stdio) sobre este workspace.

    Un servidor por workspace. Es el frente cómodo para las entrevistas: las
    tarjetas llegan como JSON tipado y se contestan en lenguaje natural. Ver el
    README para registrarlo en un host MCP.
    """
    try:
        from sas_migrator.mcp_server.server import serve_workspace
    except ModuleNotFoundError as exc:
        raise _die(
            f"Falta el extra 'mcp': {exc}",
            hint='pip install -e ".[mcp]"',
        ) from exc

    serve_workspace(_resolve_ws(workspace))


if __name__ == "__main__":
    main()
