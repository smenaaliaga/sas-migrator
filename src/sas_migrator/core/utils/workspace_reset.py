"""Reset del workspace — borrar lo derivado sin tocar lo que no se regenera.

Separado de la CLI a propósito: qué se borra es una decisión del dominio, no
de la interfaz. La CLI solo pregunta y ejecuta el plan.

La frontera es exacta: `state/` y `output/` los escribe el runtime y se
reconstruyen corriendo de nuevo; `input/` y `project_config.yaml` los escribió
un humano y no los toca nadie. Un reset que pudiera comerse el `.egp` sería un
reset que no se puede usar sin miedo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from shutil import rmtree

# Lo que escribe el runtime, en orden de aparición.
DERIVED_DIRS = ("state", "output")

# Lo que jamás se toca (se nombra explícito para poder mostrarlo al confirmar:
# la garantía sirve si el usuario la lee ANTES de decir que sí).
PRESERVED = ("input/", "project_config.yaml")

# Artefactos de `state/` que codifican decisiones HUMANAS. Todo lo demás se
# recalcula solo; esto se recupera únicamente volviendo a contestar. Es lo que
# la confirmación tiene que poner adelante — el conteo de archivos no dice
# nada, "vas a perder la entrevista post-análisis" sí.
DECISION_ARTIFACTS = (
    "initial_interview.yaml",
    "post_analysis_interview.yaml",
    "ignored_nodes.yaml",
    "placement_decisions.yaml",
    "db_connections.yaml",
    "approved_improvements.yaml",
    "cell_logging.yaml",
)


@dataclass(frozen=True)
class ResetTarget:
    """Un directorio derivado y su peso."""

    name: str
    path: Path
    files: int
    size: int


@dataclass(frozen=True)
class ResetPlan:
    """Qué se borraría. Construirlo no borra nada."""

    workspace: Path
    targets: list[ResetTarget] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    phase: int | None = None

    @property
    def files(self) -> int:
        return sum(t.files for t in self.targets)

    @property
    def size(self) -> int:
        return sum(t.size for t in self.targets)

    @property
    def is_empty(self) -> bool:
        return self.files == 0


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - inalcanzable


def _measure(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def _phase_reached(state_dir: Path) -> int | None:
    """Fase de `migration_state.json`, si existe y es legible."""
    import json

    path = state_dir / "migration_state.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("current_phase")
    except (ValueError, OSError):
        return None
    return value if isinstance(value, int) else None


def plan_reset(workspace: Path | str, *, keep_output: bool = False) -> ResetPlan:
    """Inventario de lo que un reset borraría.

    Lanza ``ValueError`` si el directorio no parece un workspace. Es la única
    defensa real contra correr el comando parado en la carpeta equivocada: sin
    ella, un `reset` en el repo o en el home borra un `output/` ajeno.
    """
    ws = Path(workspace).resolve()
    if not (ws / "input" / "egp").is_dir():
        raise ValueError(
            f"{ws} no parece un workspace de migración (falta input/egp/) — "
            "correr el reset desde la raíz del workspace o pasar --workspace"
        )

    names = DERIVED_DIRS[:1] if keep_output else DERIVED_DIRS
    targets = []
    for name in names:
        path = ws / name
        if not path.is_dir():
            continue
        files, size = _measure(path)
        targets.append(ResetTarget(name=name, path=path, files=files, size=size))

    state_dir = ws / "state"
    decisions = [n for n in DECISION_ARTIFACTS if (state_dir / n).exists()]
    return ResetPlan(
        workspace=ws,
        targets=targets,
        decisions=decisions,
        phase=_phase_reached(state_dir),
    )


def apply_reset(plan: ResetPlan) -> list[str]:
    """Ejecuta el plan. Devuelve los nombres de los directorios vaciados.

    Se recrean vacíos: un workspace tras el reset debe estar listo para `run`,
    no a medio armar.
    """
    done = []
    for target in plan.targets:
        rmtree(target.path)
        target.path.mkdir(parents=True, exist_ok=True)
        done.append(target.name)
    return done
