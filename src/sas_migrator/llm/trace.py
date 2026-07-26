"""Trazabilidad local de llamadas LLM — state/llm_trace.jsonl.

Sin esto, "el traductor anda peor esta semana" es una anécdota; con esto es
un diff de trazas: qué task, qué modelo, qué versión de prompt (hash del
system), cuántos intentos, cuántos tokens y cuánto tardó cada llamada. Es un
log local append-only por workspace — sin servicios externos.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from sas_migrator.llm.client import StructuredCaller
from sas_migrator.llm.errors import NeedsHuman

T = TypeVar("T", bound=BaseModel)

TRACE_FILE = "llm_trace.jsonl"


def _prompt_hash(system_blocks: list[str]) -> str:
    digest = hashlib.sha256("\n".join(system_blocks).encode("utf-8"))
    return digest.hexdigest()[:12]


class TracingCaller:
    """Decorador de cualquier StructuredCaller que registra cada llamada."""

    def __init__(self, inner: StructuredCaller, state_dir: Path):
        self._inner = inner
        self._trace_path = Path(state_dir) / TRACE_FILE

    def _write(self, record: dict) -> None:
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def call(
        self,
        *,
        task: str,
        system_blocks: list[str],
        user_content: str,
        output_model: type[T],
        max_tokens: int | None = None,
    ) -> T:
        record = {
            "at": datetime.now(UTC).isoformat(),
            "task": task,
            "output_model": output_model.__name__,
            "prompt_hash": _prompt_hash(system_blocks),
            "user_chars": len(user_content),
            "model": getattr(getattr(self._inner, "config", None), "model", "fake"),
        }
        start = time.monotonic()
        try:
            result = self._inner.call(
                task=task, system_blocks=system_blocks, user_content=user_content,
                output_model=output_model, max_tokens=max_tokens,
            )
            record["outcome"] = "ok"
            return result
        except NeedsHuman as exc:
            record["outcome"] = f"needs_human:{exc.reason}"
            record["attempts"] = exc.attempts
            raise
        except Exception as exc:
            record["outcome"] = f"error:{type(exc).__name__}"
            raise
        finally:
            record["duration_ms"] = int((time.monotonic() - start) * 1000)
            usage = getattr(self._inner, "last_usage", None)
            if usage:
                record["usage"] = usage
            self._write(record)


def summarize(state_dir: Path) -> dict:
    """Resumen agregado del trace (por task): llamadas, fallos, duración."""
    path = Path(state_dir) / TRACE_FILE
    if not path.exists():
        return {"calls": 0, "by_task": {}}
    by_task: dict[str, dict] = {}
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        total += 1
        entry = by_task.setdefault(
            rec.get("task", "?"),
            {"calls": 0, "ok": 0, "needs_human": 0, "errors": 0, "duration_ms": 0},
        )
        entry["calls"] += 1
        entry["duration_ms"] += int(rec.get("duration_ms", 0))
        outcome = str(rec.get("outcome", ""))
        if outcome == "ok":
            entry["ok"] += 1
        elif outcome.startswith("needs_human"):
            entry["needs_human"] += 1
        else:
            entry["errors"] += 1
    return {"calls": total, "by_task": by_task}
