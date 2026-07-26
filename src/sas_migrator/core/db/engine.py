"""Construcción canónica del engine SQL Server — única en todo el sistema.

Antes vivía triplicada (verify_tables, db_profile, run_validation) con el
servidor de un cliente como default silencioso. Ahora el servidor se resuelve
conexión → project_config → error explícito.
"""

from __future__ import annotations

from sas_migrator.core.config import ProjectConfig


def resolve_server(conn_cfg: dict, config: ProjectConfig | None = None) -> str:
    """Servidor de la conexión, con fallback al default del proyecto."""
    server = (conn_cfg.get("server") or "").strip()
    if not server and config is not None:
        server = config.db.default_server.strip()
    if not server:
        raise ValueError(
            f"Conexión '{conn_cfg.get('alias', '?')}' sin 'server' y sin "
            "db.default_server en project_config.yaml — no se asume ninguno."
        )
    return server


def connection_string(conn_cfg: dict, config: ProjectConfig | None = None) -> str:
    cfg = config or ProjectConfig()
    server = resolve_server(conn_cfg, cfg)
    port = conn_cfg.get("port", cfg.db.default_port)
    driver = conn_cfg.get("driver", cfg.db.default_driver)
    database = conn_cfg.get("database", "")
    trust = "yes" if cfg.db.trust_server_certificate else "no"
    return (
        "mssql+pyodbc:///?"
        "odbc_connect="
        f"DRIVER={{{driver}}};"
        f"SERVER=tcp:{server},{port};"
        f"DATABASE={database};"
        "Authentication=ActiveDirectoryIntegrated;"
        "Encrypt=yes;"
        f"TrustServerCertificate={trust}"
    )


def build_engine(conn_cfg: dict, config: ProjectConfig | None = None):
    from sqlalchemy import create_engine

    cfg = config or ProjectConfig()
    # db.connection_url reemplaza la construcción mssql+pyodbc por completo
    # (BD de prueba para CI/DoD, otros dialectos). No requiere server.
    if cfg.db.connection_url:
        return create_engine(cfg.db.connection_url)
    return create_engine(connection_string(conn_cfg, cfg), fast_executemany=True)


def qualified_table(engine, conn_cfg: dict, table: str) -> str:
    """Nombre calificado y citado según el dialecto del engine.

    En SQL Server: [schema].[tabla]; en sqlite (sin schemas): "tabla".
    """
    prep = engine.dialect.identifier_preparer
    schema = str(conn_cfg.get("schema_name") or "").strip()
    if schema and engine.dialect.name != "sqlite":
        return f"{prep.quote_schema(schema)}.{prep.quote(table)}"
    return prep.quote(table)
