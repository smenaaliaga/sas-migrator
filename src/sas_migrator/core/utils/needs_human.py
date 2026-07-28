"""Cola needs_human — registro visible de trabajo LLM que requiere un humano.

El contrato del DoD de la Etapa 4: tras el retry acotado (o un fallo de
chequeo estático del ensamblador), el nodo queda registrado aquí y el gate de
su fase bloquea hasta que alguien lo resuelva. Nunca silencio.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from sas_migrator.core.models.state import NeedsHumanItem, NeedsHumanQueue
from sas_migrator.core.utils import fsio

FILENAME = "needs_human.yaml"

# Serializa load-modify-save dentro del proceso: cuando la traducción se
# paralelice por nodo, dos workers no pueden pisarse la cola.
_LOCK = threading.Lock()


def load_queue(state_dir: Path) -> NeedsHumanQueue:
    data = fsio.load_yaml(Path(state_dir) / FILENAME) or {}
    return NeedsHumanQueue.model_validate(data)


def _save(state_dir: Path, queue: NeedsHumanQueue) -> None:
    fsio.dump_yaml(
        Path(state_dir) / FILENAME, json.loads(queue.model_dump_json())
    )


def _next_id(queue: NeedsHumanQueue) -> str:
    """NH-{max+1}: `len+1` colisiona si alguien borró un item del medio."""
    numbers = [
        int(m.group(1))
        for i in queue.items
        if (m := re.fullmatch(r"NH-(\d+)", i.id))
    ]
    return f"NH-{max(numbers, default=0) + 1:03d}"


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
    """Registra (o actualiza) un item en la cola y persiste.

    Upsert por clave natural ``(phase, task, node_id, reason)`` sobre los items
    NO resueltos: re-ejecutar la fase 6 con los mismos 20 nodos caídos no puede
    duplicar la cola — el gate cuenta items, y 40 duplicados leen como 40
    problemas. Un item ya resuelto con la misma clave no se toca: si el fallo
    reaparece tras resolverse, eso ES un item nuevo (la historia se conserva).
    """
    with _LOCK:
        queue = load_queue(state_dir)
        key = (phase, task, node_id, reason)
        for item in queue.items:
            if not item.resolved and (item.phase, item.task, item.node_id, item.reason) == key:
                item.detail = detail or item.detail
                item.attempts = max(item.attempts, attempts)
                _save(state_dir, queue)
                return item
        item = NeedsHumanItem(
            id=_next_id(queue),
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
