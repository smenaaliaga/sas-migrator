"""Sincronía modelos Pydantic ↔ JSON Schemas en disco.

Hueco del sistema v1: si alguien editaba un modelo sin regenerar los schemas,
los gates validaban contra un contrato viejo y ningún test lo detectaba (los
tests de gates monkeypatchean SCHEMAS_DIR con fixtures propios). Aquí se exige
que cada schema del SCHEMA_MAP exista en el directorio real del paquete y sea
byte-idéntico en contenido a lo que el modelo genera hoy.
"""

from __future__ import annotations

import json

from sas_migrator.core.utils.generate_schemas import SCHEMA_MAP
from sas_migrator.core.utils.schema_validation import SCHEMAS_DIR

REGEN_HINT = (
    "Regenerar con: python -m sas_migrator.core.utils.generate_schemas"
)


def test_schemas_dir_exists() -> None:
    assert SCHEMAS_DIR.is_dir(), f"No existe {SCHEMAS_DIR}. {REGEN_HINT}"


def test_every_model_has_schema_file_in_sync() -> None:
    stale: list[str] = []
    missing: list[str] = []
    for name, model in SCHEMA_MAP.items():
        path = SCHEMAS_DIR / f"{name}.schema.json"
        if not path.exists():
            missing.append(name)
            continue
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        if on_disk != model.model_json_schema():
            stale.append(name)
    assert not missing, f"Schemas faltantes: {missing}. {REGEN_HINT}"
    assert not stale, f"Schemas desactualizados vs modelo: {stale}. {REGEN_HINT}"


def test_no_orphan_schema_files() -> None:
    expected = {f"{name}.schema.json" for name in SCHEMA_MAP}
    on_disk = {p.name for p in SCHEMAS_DIR.glob("*.schema.json")}
    orphans = sorted(on_disk - expected)
    assert not orphans, (
        f"Schemas en disco sin modelo en SCHEMA_MAP: {orphans} — "
        "agregar el modelo al mapa o borrar el archivo."
    )
