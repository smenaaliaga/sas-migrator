#!/usr/bin/env python3
"""Verifica en SQL Server la existencia de las tablas mapeadas en
state/db_connections.yaml (bloque B4b de la entrevista).

Consulta INFORMATION_SCHEMA.TABLES por cada tabla de cada conexión, escribe
state/table_verification.json e imprime las dos listas (existentes/faltantes).

Uso: python .github/skills/mssql-connectivity/scripts/verify_tables.py [--state-dir state/]

Exit codes:
    0 = todas las tablas existen
    2 = faltan tablas DESTINO (bloqueante: el sistema no crea tablas)
    3 = sin acceso de red a la BD (verificación pendiente)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml




from sas_migrator.core.db.engine import build_engine  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica existencia de tablas en SQL Server")
    parser.add_argument("--state-dir", default="state/", help="Directorio de estado")
    args = parser.parse_args()

    from sqlalchemy import text

    state = Path(args.state_dir)
    conn_path = state / "db_connections.yaml"
    if not conn_path.exists():
        raise SystemExit(f"No existe {conn_path} — nada que verificar")
    connections = (yaml.safe_load(conn_path.read_text(encoding="utf-8")) or {}).get("connections", [])
    if not connections:
        raise SystemExit("db_connections.yaml no tiene conexiones")

    exists: list[dict] = []
    missing: list[dict] = []

    query = text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
    )

    for conn_cfg in connections:
        database = conn_cfg.get("database", "")
        schema = conn_cfg.get("schema_name", "dbo")
        role = conn_cfg.get("role", "source")
        tables = conn_cfg.get("tables", [])
        try:
            engine = build_engine(conn_cfg)
            with engine.connect() as conn:
                for table in tables:
                    found = conn.execute(query, {"schema": schema, "table": table}).scalar() > 0
                    record = {
                        "qualified_name": f"{database}.{schema}.{table}",
                        "alias": conn_cfg.get("alias", ""),
                        "role": role,
                    }
                    (exists if found else missing).append(record)
        except Exception as e:
            print(f"✗ Sin acceso a {conn_cfg.get('server', '?')}/{database}: {e}")
            report = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "blocked",
                "note": f"Sin acceso de red: {e}",
                "exists": [], "missing_targets": [], "missing_sources": [],
            }
            (state / "table_verification.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            sys.exit(3)

    missing_targets = [m for m in missing if m["role"] in ("target", "both")]
    missing_sources = [m for m in missing if m["role"] == "source"]

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked_missing_targets" if missing_targets else ("ok" if not missing else "missing_sources"),
        "exists": exists,
        "missing_targets": missing_targets,
        "missing_sources": missing_sources,
    }
    (state / "table_verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"✅ Existen ({len(exists)}):")
    for r in exists:
        print(f"   {r['qualified_name']}")
    if missing:
        print(f"❌ No existen ({len(missing)}):")
        for r in missing:
            kind = "DESTINO (bloqueante)" if r in missing_targets else "fuente"
            print(f"   {r['qualified_name']} — {kind}")
    print(f"\nReporte: {state / 'table_verification.json'}")
    sys.exit(2 if missing_targets else 0)


if __name__ == "__main__":
    main()
