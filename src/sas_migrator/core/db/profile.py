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


def profile_table_from_db(conn_cfg: dict, table_name: str) -> dict:
    """Perfila una tabla conectándose a la BD MSSQL.

    Lanza ConnectionError si no hay acceso al servidor.
    """
    from sqlalchemy import create_engine, text

    server = conn_cfg.get("server", "srvplatdat.bcch.local")
    port = conn_cfg.get("port", 1433)
    driver = conn_cfg.get("driver", "ODBC Driver 17 for SQL Server")
    schema = conn_cfg.get("schema_name", "dbo")
    database = conn_cfg.get("database", "")

    connection_string = (
        "mssql+pyodbc:///?"
        "odbc_connect="
        f"DRIVER={{{driver}}};"
        f"SERVER=tcp:{server},{port};"
        f"DATABASE={database};"
        "Authentication=ActiveDirectoryIntegrated;"
        "Encrypt=yes;"
        "TrustServerCertificate=yes"
    )

    try:
        engine = create_engine(connection_string, fast_executemany=True)
        with engine.connect() as conn:
            # Conteo de filas
            row_count = conn.execute(
                text(f"SELECT COUNT(*) FROM [{schema}].[{table_name}]")
            ).scalar()

            # Metadata de columnas
            col_query = text(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table "
                "ORDER BY ORDINAL_POSITION"
            )
            columns_meta = conn.execute(
                col_query, {"schema": schema, "table": table_name}
            ).fetchall()

            # Perfilar cada columna
            columns = []
            for col_name, dtype, nullable in columns_meta:
                stats_query = text(
                    f"SELECT "
                    f"COUNT(*) AS total, "
                    f"COUNT([{col_name}]) AS non_null, "
                    f"COUNT(DISTINCT [{col_name}]) AS uniq "
                    f"FROM [{schema}].[{table_name}]"
                )
                total, non_null, uniq = conn.execute(stats_query).fetchone()

                sample_query = text(
                    f"SELECT DISTINCT TOP 5 CAST([{col_name}] AS NVARCHAR(200)) "
                    f"FROM [{schema}].[{table_name}] "
                    f"WHERE [{col_name}] IS NOT NULL"
                )
                samples = [str(r[0]) for r in conn.execute(sample_query).fetchall()]

                null_count = total - non_null
                null_pct = round(null_count / total, 4) if total > 0 else 0.0

                columns.append({
                    "name": col_name,
                    "dtype": dtype,
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
                profile = profile_table_from_db(conn, table)
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
