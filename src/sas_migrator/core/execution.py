"""Ejecución in-process de notebooks vía nbclient (Fase 7, Etapa 5).

SOLO se invoca detrás del interrupt de autorización (los notebooks pueden
escribir en la BD — esa pausa es sagrada). El resultado por notebook queda en
``state/execution_report.json``; un FAIL bloquea el gate 7.

La URL de BD para los notebooks viaja por la variable de entorno
``SASMIG_DB_URL`` (la celda de configuración del ensamblador construye el
engine desde ahí cuando el proyecto tiene conexiones declaradas).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

DB_URL_ENV = "SASMIG_DB_URL"


def resolve_db_url(state_dir: Path, config) -> str | None:
    """URL SQLAlchemy para los notebooks: config.db.connection_url o la
    construcción mssql desde la primera conexión de db_connections.yaml."""
    if config is not None and config.db.connection_url:
        return config.db.connection_url
    import yaml

    conn_path = Path(state_dir) / "db_connections.yaml"
    if not conn_path.exists():
        return None
    connections = (
        yaml.safe_load(conn_path.read_text(encoding="utf-8")) or {}
    ).get("connections", [])
    if not connections:
        return None
    from sas_migrator.core.db.engine import connection_string

    try:
        return connection_string(connections[0], config)
    except ValueError:
        return None  # sin server configurado: la ejecución fallará visible


def _notebook_source_sha(nb) -> str:
    """Hash del SOURCE de las celdas code (los outputs no cuentan: ejecutar
    reescribe el notebook con outputs y el hash debe sobrevivir a eso)."""
    import hashlib

    h = hashlib.sha256()
    for cell in nb.cells:
        if cell.get("cell_type") == "code":
            h.update("".join(cell.get("source", "")).encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()


def execute_notebooks(
    output_dir: Path,
    notebooks: list[str] | None = None,
    *,
    db_url: str | None = None,
    timeout: int = 600,
    progress_path: Path | None = None,
) -> dict:
    """Ejecuta los notebooks en orden y devuelve el reporte por notebook.

    Los notebooks ejecutados se reescriben con sus outputs (evidencia). Un
    fallo no corta la corrida: cada notebook reporta PASS/FAIL con su error.

    Con ``progress_path``, la ejecución es reanudable e idempotente: tras CADA
    notebook se persiste (atómico) su sha de celdas + resultado; un notebook
    ya PASS con el mismo sha se saltea (``SKIPPED_CACHED``) en la siguiente
    corrida. Un crash a mitad de la fase 7 no re-escribe en la BD lo que ya
    pasó — re-ejecutar notebooks con escrituras a medias era el riesgo del
    replay (ADR-0009). Si el sha cambió (re-traducción, edición), se
    re-ejecuta.
    """
    import nbformat
    from nbclient import NotebookClient

    from sas_migrator.core.utils import fsio

    output_dir = Path(output_dir)
    if notebooks is None:
        from sas_migrator.core.gen_run_all import discover_notebooks

        notebooks = discover_notebooks(output_dir)

    progress: dict[str, dict] = {}
    if progress_path is not None:
        progress = fsio.load_json(progress_path) or {}

    previous = os.environ.get(DB_URL_ENV)
    if db_url:
        os.environ[DB_URL_ENV] = db_url
    try:
        results: list[dict] = []
        for name in notebooks:
            path = output_dir / name
            if not path.exists():
                results.append({"notebook": name, "status": "SKIPPED",
                                "error": "no existe"})
                continue
            nb = nbformat.read(str(path), as_version=4)
            sha = _notebook_source_sha(nb)
            prev = progress.get(name)
            if prev and prev.get("sha") == sha and prev.get("status") == "PASS":
                results.append({"notebook": name, "status": "SKIPPED_CACHED",
                                "error": ""})
                continue
            client = NotebookClient(
                nb, timeout=timeout, kernel_name="python3",
                resources={"metadata": {"path": str(output_dir)}},
            )
            try:
                client.execute()
                nbformat.write(nb, str(path))
                results.append({"notebook": name, "status": "PASS", "error": ""})
                status = "PASS"
            except Exception as exc:
                results.append({
                    "notebook": name,
                    "status": "FAIL",
                    "error": str(exc)[-1500:],
                })
                status = "FAIL"
            if progress_path is not None:
                progress[name] = {"sha": sha, "status": status,
                                  "at": datetime.now(UTC).isoformat()}
                fsio.dump_json(progress_path, progress)
    finally:
        if db_url:
            if previous is None:
                os.environ.pop(DB_URL_ENV, None)
            else:
                os.environ[DB_URL_ENV] = previous

    passed = sum(1 for r in results if r["status"] in ("PASS", "SKIPPED_CACHED"))
    failed = sum(1 for r in results if r["status"] == "FAIL")
    return {
        "executed_at": datetime.now(UTC).isoformat(),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "results": results,
    }
