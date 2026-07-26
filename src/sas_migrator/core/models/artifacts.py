"""Contratos de artefactos transversales del pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FileCatalogEntry(BaseModel):
    """Archivo detectado durante la fase de intake."""

    path: str
    size_bytes: int = Field(default=0, ge=0)
    type: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class IntakeCatalog(BaseModel):
    """Catalogo de archivos del workspace producido en Fase 0."""

    egp_files: list[FileCatalogEntry] = Field(default_factory=list)
    data_files: list[FileCatalogEntry] = Field(default_factory=list)
    doc_files: list[FileCatalogEntry] = Field(default_factory=list)
    selected_egp: str | None = None
    scanned_at: datetime = Field(default_factory=datetime.utcnow)
    notes: list[str] = Field(default_factory=list)