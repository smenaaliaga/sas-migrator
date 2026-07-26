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

    return create_engine(connection_string(conn_cfg, config), fast_executemany=True)
