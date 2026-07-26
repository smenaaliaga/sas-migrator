#!/usr/bin/env python3
"""Verifica en la BD la existencia de las tablas mapeadas en
state/db_connections.yaml (paso 4 del bloque B4b de la entrevista).

Usa el inspector de SQLAlchemy (dialect-agnóstico: SQL Server, LocalDB,
sqlite de prueba), escribe state/table_verification.json y reporta
existentes/faltantes. Las tablas DESTINO faltantes son bloqueantes: el
sistema no crea tablas.

Uso CLI: python -m sas_migrator.core.db.verify_tables [--state-dir state/]

Exit codes:
    0 = todas las tablas existen (o solo faltan fuentes)
    2 = faltan tablas DESTINO (bloqueante)
    3 = sin acceso a la BD (verificación pendiente)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from sas_migrator.core.config import ProjectConfig
from sas_migrator.core.db.engine import build_engine


def verify(state_dir: Path, config: ProjectConfig | None = None) -> tuple[dict, int]:
    """Verificación in-process. Devuelve (reporte, exit_code semántico).

    Escribe state/table_verification.json. Sin db_connections.yaml devuelve
    status "not_applicable" (0) sin escribir nada que bloquee.
    """
    from sqlalchemy import inspect

    state_dir = Path(state_dir)
    conn_path = state_dir / "db_connections.yaml"
    if not conn_path.exists():
        return {"status": "not_applicable", "note": "sin db_connections.yaml"}, 0
    connections = (
        yaml.safe_load(conn_path.read_text(encoding="utf-8")) or {}
    ).get("connections", [])
    if not connections:
        return {"status": "not_applicable", "note": "db_connections.yaml sin conexiones"}, 0

    exists: list[dict] = []
    missing: list[dict] = []

    for conn_cfg in connections:
        database = conn_cfg.get("database", "")
        schema = str(conn_cfg.get("schema_name") or "").strip()
        role = conn_cfg.get("role", "source")
        tables = conn_cfg.get("tables", [])
        try:
            engine = build_engine(conn_cfg, config)
            inspector = inspect(engine)
            use_schema = schema if (schema and engine.dialect.name != "sqlite") else None
            for table in tables:
                found = inspector.has_table(table, schema=use_schema)
                record = {
                    "qualified_name": f"{database}.{schema}.{table}" if schema else table,
                    "alias": conn_cfg.get("alias", ""),
                    "role": role,
                }
                (exists if found else missing).append(record)
        except Exception as e:
            report = {
                "checked_at": datetime.now(UTC).isoformat(),
                "status": "blocked",
                "note": f"Sin acceso a la BD ({conn_cfg.get('alias', '?')}): {e}",
                "exists": [], "missing_targets": [], "missing_sources": [],
            }
            (state_dir / "table_verification.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return report, 3

    missing_targets = [m for m in missing if m["role"] in ("target", "both")]
    missing_sources = [m for m in missing if m["role"] == "source"]

    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": (
            "blocked_missing_targets" if missing_targets
            else ("ok" if not missing else "missing_sources")
        ),
        "exists": exists,
        "missing_targets": missing_targets,
        "missing_sources": missing_sources,
    }
    (state_dir / "table_verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report, (2 if missing_targets else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica existencia de tablas en la BD")
    parser.add_argument("--state-dir", default="state/", help="Directorio de estado")
    args = parser.parse_args()

    state = Path(args.state_dir)
    if not (state / "db_connections.yaml").exists():
        raise SystemExit(f"No existe {state / 'db_connections.yaml'} — nada que verificar")

    from sas_migrator.core.config import load_project_config

    report, code = verify(state, load_project_config(state.resolve().parent))
    if report["status"] == "not_applicable":
        raise SystemExit("db_connections.yaml no tiene conexiones")
    if report["status"] == "blocked":
        print(f"✗ {report['note']}")
        sys.exit(3)

    print(f"✅ Existen ({len(report['exists'])}):")
    for r in report["exists"]:
        print(f"   {r['qualified_name']}")
    missing = report["missing_targets"] + report["missing_sources"]
    if missing:
        print(f"❌ No existen ({len(missing)}):")
        for r in missing:
            kind = "DESTINO (bloqueante)" if r in report["missing_targets"] else "fuente"
            print(f"   {r['qualified_name']} — {kind}")
    print(f"\nReporte: {state / 'table_verification.json'}")
    sys.exit(code)


if __name__ == "__main__":
    main()
