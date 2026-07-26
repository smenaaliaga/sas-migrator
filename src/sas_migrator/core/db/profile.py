#!/usr/bin/env python3
"""Perfila tablas MSSQL Server para integrar en profile_report.json.

Lee las conexiones de state/db_connections.yaml y para cada tabla:
- Extrae metadata (columnas, tipos, nulls, estadísticas)
- Genera un DataProfile compatible con profile_report.json

Sin acceso a la BD el script termina con exit code 3; el orquestador registra
el profiling de tablas como pendiente.

Usage:
    python db_profile.py [--state-dir state/]

Exit codes:
    0 = perfiles generados
    1 = error de configuración (sin db_connections.yaml o sin tablas)
    3 = sin acceso a la BD (profiling pendiente)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Asegurar que src/ sea importable

try:
    import yaml
except ImportError:
    sys.exit("Error: pyyaml no está instalado. Ejecutar: pip install pyyaml")


def load_connections(state_dir: Path) -> list[dict]:
    """Lee state/db_connections.yaml y retorna la lista de conexiones."""
    path = state_dir / "db_connections.yaml"
    if not path.exists():
        print(f"⚠ No se encontró {path} — nada que perfilar")
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("connections", [])


def profile_table_from_db(conn_cfg: dict, table_name: str, config=None) -> dict:
    """Perfila una tabla de la BD (dialect-agnóstico vía SQLAlchemy inspector).

    Lanza ConnectionError si no hay acceso al servidor.
    """
    from sqlalchemy import inspect, text

    from sas_migrator.core.db.engine import build_engine, qualified_table, resolve_server

    schema = conn_cfg.get("schema_name", "dbo")
    database = conn_cfg.get("database", "")
    try:
        server = resolve_server(conn_cfg, config)
    except ValueError:
        server = "(connection_url)"  # BD de prueba vía db.connection_url

    try:
        engine = build_engine(conn_cfg, config)
        prep = engine.dialect.identifier_preparer
        qt = qualified_table(engine, conn_cfg, table_name)
        use_schema = schema if (schema and engine.dialect.name != "sqlite") else None
        columns_meta = inspect(engine).get_columns(table_name, schema=use_schema)

        with engine.connect() as conn:
            row_count = conn.execute(text(f"SELECT COUNT(*) FROM {qt}")).scalar()

            columns = []
            for col in columns_meta:
                col_name = str(col["name"])
                qcol = prep.quote(col_name)
                total, non_null, uniq = conn.execute(text(
                    f"SELECT COUNT(*), COUNT({qcol}), COUNT(DISTINCT {qcol}) FROM {qt}"
                )).fetchone()
                samples = [
                    str(r[0])
                    for r in conn.execute(text(
                        f"SELECT DISTINCT {qcol} FROM {qt} WHERE {qcol} IS NOT NULL"
                    )).fetchmany(5)
                ]

                null_count = total - non_null
                null_pct = round(null_count / total, 4) if total > 0 else 0.0

                columns.append({
                    "name": col_name,
                    "dtype": str(col.get("type", "")),
                    "null_count": null_count,
                    "null_pct": null_pct,
                    "unique_count": uniq,
                    "sample_values": samples[:5],
                    "min_val": None,
                    "max_val": None,
                    "mean_val": None,
                })
    except Exception as e:
        raise ConnectionError(f"Error conectando a {server}/{database}: {e}") from e

    alias = conn_cfg.get("alias", server.split(".")[0])
    return {
        "file_path": f"db:{alias}/{database}.{schema}.{table_name}",
        "file_type": "mssql_table",
        "row_count": row_count,
        "column_count": len(columns),
        "columns": columns,
        "sheets": [],
        "encoding": None,
        "artifacts": [],
        "metadata": {
            "source_type": "mssql",
            "server": server,
            "database": database,
            "schema": schema,
            "table": table_name,
            "profiled_via": "db_profile.py",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Perfila tablas MSSQL para migration pipeline")
    parser.add_argument("--state-dir", default="state/", help="Directorio de estado")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    connections = load_connections(state_dir)
    if not connections:
        sys.exit(1)

    from sas_migrator.core.config import load_project_config

    config = load_project_config(state_dir.parent)

    # Cargar profile_report existente si hay
    report_path = state_dir / "profile_report.json"
    if report_path.exists():
        with open(report_path, encoding="utf-8") as f:
            existing_profiles = json.load(f)
        if isinstance(existing_profiles, dict):
            existing_profiles = [existing_profiles]
    else:
        existing_profiles = []

    # Filtrar perfiles DB previos (para re-generar sin duplicar)
    existing_profiles = [
        p for p in existing_profiles
        if not p.get("file_path", "").startswith("db:")
    ]

    db_profiles = []
    for conn in connections:
        alias = conn.get("alias", "DB")
        database = conn.get("database", "?")
        tables = conn.get("tables", [])

        print(f"\n📦 Perfilando {alias}/{database} — {len(tables)} tablas")

        for table in tables:
            print(f"  → {table}...", end=" ")
            try:
                profile = profile_table_from_db(conn, table, config)
            except ConnectionError as e:
                print(f"\n✗ {e}")
                print("Sin acceso a la BD — el profiling de tablas queda PENDIENTE.")
                sys.exit(3)
            db_profiles.append(profile)
            print(f"✓ {profile['row_count']:,} filas, {profile['column_count']} cols")

    # Combinar con perfiles existentes
    all_profiles = existing_profiles + db_profiles
    report_path.write_text(
        json.dumps(all_profiles, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n✅ Profile report actualizado: {report_path}")
    print(f"   {len(existing_profiles)} perfiles de archivos + {len(db_profiles)} perfiles de tablas DB")


if __name__ == "__main__":
    main()
