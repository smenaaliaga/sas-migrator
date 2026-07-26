"""Configuración de proyecto — todo lo específico de un cliente/entorno vive aquí.

El código genérico no puede traer defaults de un cliente (servidores, heurísticas
de dominio, marcadores de auditoría). Esos valores se cargan desde
``project_config.yaml`` en la raíz del workspace de migración; sin archivo,
aplican defaults neutros y los pasos que requieren un valor (p. ej. servidor de
BD) fallan con un error claro en vez de apuntar silenciosamente a infra ajena.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_FILENAME = "project_config.yaml"


class DBConfig(BaseModel):
    """Defaults de conexión SQL Server para el proyecto."""

    default_server: str = ""
    default_port: int = 1433
    default_driver: str = "ODBC Driver 17 for SQL Server"
    # Hoy la conexión usa siempre AD integrated + Encrypt. Se mantienen como
    # default pero el certificado ya no se confía a ciegas salvo opt-in.
    trust_server_certificate: bool = False


class AuditConfig(BaseModel):
    """Marcadores de dominio para la auditoría de traducción.

    Vacíos por default: los chequeos dependientes de dominio (URLs del cliente,
    nombres de engine, DataFrames esperados) solo aplican si el proyecto los
    declara. Los chequeos genéricos (PROC HTTP→requests, secretos, etc.) no
    dependen de esto.
    """

    domain_markers: list[str] = Field(default_factory=list)
    sql_engine_markers: list[str] = Field(default_factory=list)
    env_secret_markers: list[str] = Field(
        default_factory=lambda: ["os.environ", "dotenv"]
    )
    runtime_df_checks: list[str] = Field(default_factory=list)


class LlmConfig(BaseModel):
    """Configuración de los nodos LLM (Etapa 4).

    ``model`` va pineado (ID fijo, sin sufijo de fecha). Sin parámetros de
    sampling: los modelos actuales los removieron del API; el determinismo se
    persigue con structured outputs y prompts, no con temperature.
    """

    model: str = "claude-opus-5"
    max_tokens: int = 16000
    max_validation_retries: int = 3


class ParserConfig(BaseModel):
    """Ajustes del parser SAS para el proyecto.

    ``extra_db_engines``: engines de BD del cliente que no están en la lista
    default de SAS/ACCESS (alias custom, engines exóticos). ``ignore_db_engines``
    los remueve del set (p. ej. un engine que el cliente usa para archivos).
    La resolución vive en ``core.parser.statements.resolve_db_engines``.
    """

    extra_db_engines: list[str] = Field(default_factory=list)
    ignore_db_engines: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """Raíz de project_config.yaml."""

    db: DBConfig = Field(default_factory=DBConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)


def load_project_config(workspace: Path | str | None = None) -> ProjectConfig:
    """Carga project_config.yaml desde el workspace; defaults neutros si no existe."""
    if workspace is None:
        return ProjectConfig()
    path = Path(workspace)
    if path.is_dir():
        path = path / CONFIG_FILENAME
    if not path.exists():
        return ProjectConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ProjectConfig.model_validate(data)
