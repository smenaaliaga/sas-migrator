"""Data models — profiling, file mapping, column mapping, preprocess candidates."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

# ── Human Artifacts ─────────────────────────────────────────────────────────

class ArtifactType(str, Enum):
    MULTIPLE_SHEETS = "multiple_sheets"
    MERGED_CELLS = "merged_cells"
    FORMULAS = "formulas"
    PIVOTED_LAYOUT = "pivoted_layout"
    SHIFTED_HEADERS = "shifted_headers"
    SENTINEL_VALUES = "sentinel_values"
    MIXED_TYPES = "mixed_types"
    OTHER = "other"


class HumanArtifact(BaseModel):
    """A human-introduced artifact detected in a data file."""

    artifact_type: ArtifactType
    description: str
    location: str = ""  # e.g. "Sheet2", "Column B rows 5-10"
    recommendation: str = ""


# ── Data Profile ────────────────────────────────────────────────────────────

class ColumnProfile(BaseModel):
    name: str
    dtype: str
    null_count: int = 0
    null_pct: float = 0.0
    unique_count: int = 0
    sample_values: list[str] = Field(default_factory=list, max_length=5)
    min_val: str | None = None
    max_val: str | None = None
    mean_val: float | None = None


class DataProfile(BaseModel):
    """Profile of a tabular data file."""

    file_path: str
    file_type: str  # csv, xlsx, parquet, txt
    row_count: int = 0
    column_count: int = 0
    columns: list[ColumnProfile] = Field(default_factory=list)
    sheets: list[str] = Field(default_factory=list)  # for Excel files
    encoding: str | None = None
    artifacts: list[HumanArtifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── File → Node Mapping ────────────────────────────────────────────────────

class FileMapping(BaseModel):
    """Mapping of a data file to a SAS DAG node."""

    file_path: str
    node_id: str | None = None
    role: str = ""  # input | output | intermediate | unknown
    confidence: float = 0.0  # 0.0 – 1.0
    reasons: list[str] = Field(default_factory=list)
    needs_confirmation: bool = False


# ── Database Connections (MSSQL) ────────────────────────────────────────────

class AuthMethod(str, Enum):
    ACTIVE_DIRECTORY_INTEGRATED = "active_directory_integrated"
    WINDOWS_AUTH = "windows_auth"


class ConnectionRole(str, Enum):
    SOURCE = "source"
    TARGET = "target"
    BOTH = "both"


class DatabaseConnection(BaseModel):
    """Conexión a una base de datos MSSQL para lectura y/o escritura de datos."""

    alias: str = Field(description="Nombre corto para identificar la conexión (ej: PLATDAT)")
    server: str = Field(
        default="",
        description=(
            "Hostname o IP del servidor SQL Server. Vacío = se resuelve desde "
            "db.default_server en project_config.yaml; sin default configurado, "
            "conectar falla con error explícito (nunca se asume un servidor)."
        ),
    )
    port: int = Field(default=1433, ge=1, le=65535)
    database: str = Field(description="Nombre de la base de datos (ej: PYDCNI)")
    schema_name: str = Field(
        default="dbo",
        description="Esquema por defecto para las tablas",
    )
    auth_method: AuthMethod = Field(default=AuthMethod.ACTIVE_DIRECTORY_INTEGRATED)
    driver: str = Field(default="ODBC Driver 17 for SQL Server")
    role: ConnectionRole = Field(
        default=ConnectionRole.SOURCE,
        description="Rol de la conexión: source (lectura), target (escritura) o both",
    )
    tables: list[str] = Field(
        default_factory=list,
        description="Tablas asociadas a esta conexión",
    )
    assumed: bool = Field(
        default=False,
        description=(
            "True cuando la conexión la dedujo el migrador (el usuario confirmó "
            "librefs como base de datos pero no confirmó conectar el resultado). "
            "El supuesto queda explícito en vez de degradar en silencio."
        ),
    )


class DatabaseConnections(BaseModel):
    """Coleccion de conexiones MSSQL declaradas para una migracion."""

    connections: list[DatabaseConnection] = Field(default_factory=list)


# ── External API Connections (HTTP) ─────────────────────────────────────────

_IMPORT_NAME_RE = r"^[A-Za-z_][A-Za-z0-9_]*$"


class ApiConnectionMode(str, Enum):
    HTTP = "http"  # replicar la llamada del SAS con requests
    SDK = "sdk"    # usar una librería oficial declarada por el usuario


class ApiConnection(BaseModel):
    """Decisión B4c para UN host HTTP que el SAS consulta."""

    host: str = Field(description="Host leído del URL= del SAS (ej: si3.bcentral.cl)")
    mode: ApiConnectionMode = Field(
        default=ApiConnectionMode.HTTP,
        description="http = replicar con requests; sdk = usar la librería declarada",
    )
    package: str = Field(
        default="",
        description="Nombre de IMPORT del SDK (no el nombre pip); requerido si mode=sdk",
    )
    node_ids: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _sdk_requires_package(self) -> ApiConnection:
        if self.mode is ApiConnectionMode.SDK:
            if not re.match(_IMPORT_NAME_RE, self.package or ""):
                raise ValueError(
                    f"mode=sdk para {self.host!r} exige `package` con forma de nombre "
                    f"de import Python (recibido: {self.package!r})"
                )
        return self


class ApiConnections(BaseModel):
    """Decisiones B4c de conexiones externas (una por host detectado)."""

    connections: list[ApiConnection] = Field(default_factory=list)
