"""Cola needs_human — registro visible de trabajo LLM que requiere un humano.

El contrato del DoD de la Etapa 4: tras el retry acotado (o un fallo de
chequeo estático del ensamblador), el nodo queda registrado aquí y el gate de
su fase bloquea hasta que alguien lo resuelva. Nunca silencio.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sas_migrator.core.models.state import NeedsHumanItem, NeedsHumanQueue

FILENAME = "needs_human.yaml"


def load_queue(state_dir: Path) -> NeedsHumanQueue:
    path = Path(state_dir) / FILENAME
    if not path.exists():
        return NeedsHumanQueue()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return NeedsHumanQueue.model_validate(data)


def _save(state_dir: Path, queue: NeedsHumanQueue) -> None:
    import json

    path = Path(state_dir) / FILENAME
    path.write_text(
        yaml.safe_dump(json.loads(queue.model_dump_json()), allow_unicode=True,
                       sort_keys=False),
        encoding="utf-8",
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
