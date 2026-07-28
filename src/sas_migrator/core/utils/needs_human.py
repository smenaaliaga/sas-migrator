"""Cola needs_human — registro visible de trabajo LLM que requiere un humano.

El contrato del DoD de la Etapa 4: tras el retry acotado (o un fallo de
chequeo estático del ensamblador), el nodo queda registrado aquí y el gate de
su fase bloquea hasta que alguien lo resuelva. Nunca silencio.
"""

from __future__ import annotations

import json
from pathlib import Path

from sas_migrator.core.models.state import NeedsHumanItem, NeedsHumanQueue
from sas_migrator.core.utils import fsio

FILENAME = "needs_human.yaml"


def load_queue(state_dir: Path) -> NeedsHumanQueue:
    data = fsio.load_yaml(Path(state_dir) / FILENAME) or {}
    return NeedsHumanQueue.model_validate(data)


def _save(state_dir: Path, queue: NeedsHumanQueue) -> None:
    fsio.dump_yaml(
        Path(state_dir) / FILENAME, json.loads(queue.model_dump_json())
    )


def record(
    state_dir: Path,
    *,
    phase: int,
    task: str,
    reason: str,
    node_id: str | None = None,
    detail: str = "",
    attempts: int = 0,
) -> NeedsHumanItem:
    """Agrega un item a la cola (id NH-NNN correlativo) y persiste."""
    queue = load_queue(state_dir)
    item = NeedsHumanItem(
        id=f"NH-{len(queue.items) + 1:03d}",
        phase=phase,
        task=task,
        node_id=node_id,
        reason=reason,
        detail=detail,
        attempts=attempts,
    )
    queue.items.append(item)
    _save(state_dir, queue)
    return item


def unresolved(state_dir: Path, phase: int | None = None) -> list[NeedsHumanItem]:
    items = [i for i in load_queue(state_dir).items if not i.resolved]
    if phase is not None:
        items = [i for i in items if i.phase == phase]
    return items
