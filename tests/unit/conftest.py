"""Fixtures compartidas — re-exportan el builder de .egp sintético.

La implementación vive en ``sas_migrator.testing.egp_builder`` para que la CLI
(golden runs) y los tests usen exactamente el mismo generador. Si el formato de
un .egp real difiere, ajustar solo ese módulo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sas_migrator.testing.egp_builder import (  # noqa: F401 — re-export para tests
    CODE_TASK_1_SAS,
    CODE_TASK_2_SAS,
    QUERY_1_SAS,
    build_egp,
)


@pytest.fixture
def synthetic_egp(tmp_path: Path):
    """Factory: ``synthetic_egp(**flags)`` construye el .egp en ``tmp_path``."""

    def _make(**kwargs) -> Path:
        return build_egp(tmp_path / "proyecto.egp", **kwargs)

    return _make
