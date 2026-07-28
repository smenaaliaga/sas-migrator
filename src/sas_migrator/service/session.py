"""MigrationSession — el grafo con checkpointer detrás de una API de sesión.

Un workspace = una migración = un thread (``thread_id="migration"``, igual que
la CLI de Etapa 1). La fuente de verdad del interrupt pendiente es
``graph.get_state(config).tasks[*].interrupts`` — nunca un espejo en el estado.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sas_migrator.core.models.interview import InterviewCard
from sas_migrator.service.models import SessionResult, SessionStatus

CHECKPOINT_FILE = "graph_checkpoint.sqlite"
THREAD_ID = "migration"


class MigrationSession:
    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.state_dir = self.workspace / "state"
        self._graph = None
        self._known_gates = 0  # para derivar "✅ Fase N completada" por delta

    # ── infraestructura ────────────────────────────────────────────────────

    @property
    def graph(self):
        if self._graph is None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            from sas_migrator.graph.builder import build_graph

            self.state_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.state_dir / CHECKPOINT_FILE), check_same_thread=False
            )
            self._graph = build_graph(checkpointer=SqliteSaver(conn))
        return self._graph

    @staticmethod
    def _config() -> dict:
        return {"configurable": {"thread_id": THREAD_ID}}

    def _pending_from_result(self, result: dict) -> InterviewCard | None:
        interrupts = result.get("__interrupt__") or ()
        if interrupts:
            return InterviewCard.model_validate(interrupts[0].value)
        return None

    def _to_result(self, result: dict) -> SessionResult:
        history = result.get("gate_history") or []
        completed = [g["phase"] for g in history if g.get("passed")]
        messages = [
            f"✅ Fase {g['phase']} completada"
            for g in history[self._known_gates:]
            if g.get("passed")
        ]
        self._known_gates = len(history)

        pending = self._pending_from_result(result)
        if pending is not None:
            status = SessionStatus.WAITING_USER
        elif result.get("done"):
            status = SessionStatus.COMPLETED
            messages.append("✅ Migración base completa (fases 0-8, gates en verde).")
        else:
            status = SessionStatus.BLOCKED
        gate = result.get("last_gate") or {}
        return SessionResult(
            status=status,
            phase=int(result.get("current_phase", -1)),
            pending_card=pending,
            gate_errors=[] if gate.get("passed", True) else list(gate.get("errors", [])),
            completed_phases=completed,
            messages=messages,
        )

    # ── operaciones ────────────────────────────────────────────────────────

    def start(self, egp: Path | None = None, *, stub_mode: bool = False) -> SessionResult:
        from sas_migrator.graph.builder import initial_state

        if egp is None:
            candidates = sorted((self.workspace / "input" / "egp").glob("*.egp"))
            if len(candidates) != 1:
                raise FileNotFoundError(
                    f"se esperaba exactamente un .egp en input/egp/, hay {len(candidates)}"
                )
            egp = candidates[0]
        result = self.graph.invoke(
            initial_state(self.workspace, egp, stub_mode=stub_mode), self._config()
        )
        return self._to_result(result)

    def resume(self) -> SessionResult:
        """Continúa desde el checkpoint (tras un reinicio del proceso).

        Si la corrida terminó en ``gate_blocked``, "continuar" significa
        re-evaluar el gate (los needs_human ya resueltos, el artefacto
        corregido): antes esto era un no-op sobre un checkpoint terminal — el
        comando prometía reintentar y devolvía el mismo BLOCKED sin ejecutar
        nada.
        """
        snapshot = self.graph.get_state(self._config())
        gate = (snapshot.values or {}).get("last_gate") or {}
        terminal = not (snapshot.next or ()) and not snapshot.tasks
        if terminal and gate.get("passed") is False:
            return self.retry_gate()
        result = self.graph.invoke(None, self._config())
        return self._to_result(result)

    def retry_gate(self) -> SessionResult:
        """Re-evalúa el gate que bloqueó y, si pasa, la corrida sigue.

        Mecánica: ``update_state(as_node=<nodo de la fase>)`` crea un
        checkpoint nuevo como si la fase acabara de terminar — SIN
        re-ejecutarla — con ``next=(gateN,)`` y sin writes viejos colgando;
        ``invoke(None)`` ejecuta el gate fresco (post-P0.5 los gates son
        puros: re-evaluarlos es barato). Esto NO agrega aristas ni ciclos a
        la topología: el gate sigue siendo el único camino a la fase
        siguiente. Para RE-EJECUTAR la fase (p. ej. re-traducir nodos
        faltantes), ``rewind_to_phase``.
        """
        from sas_migrator.graph.builder import PHASES

        snapshot = self.graph.get_state(self._config())
        gate = (snapshot.values or {}).get("last_gate") or {}
        if gate.get("passed") is not False:
            raise LookupError("no hay gate bloqueado que reintentar")
        phase = int(gate["phase"])
        phase_node = next(name for name, _fn, ph in PHASES if ph == phase)
        self.graph.update_state(self._config(), None, as_node=phase_node)
        result = self.graph.invoke(None, self._config())
        return self._to_result(result)

    def rewind_to_phase(self, phase: int) -> SessionResult:
        """Descarta lo hecho desde el inicio de ``phase`` y la re-ejecuta.

        Las respuestas ya entregadas a los ``interrupt()`` de una entrevista no
        viven en ``state/`` (el nodo escribe recién al final) sino como writes
        ``__resume__`` del checkpointer: reanudar sin más las replica y la
        entrevista sigue donde iba. Rebobinar es reanudar desde el checkpoint
        ANTERIOR al nodo de la fase — esos resumes quedan en la rama vieja y el
        nodo vuelve a preguntar desde la primera tarjeta.
        """
        from sas_migrator.graph.builder import PHASE_ENTRY_NODE

        # El nodo de ENTRADA de la fase: una fase puede ser varios sub-nodos
        # (la 7 es verify → authorize → execute_validate) y rebobinarla es
        # arrancar en el primero, no en el que toca el gate.
        node = PHASE_ENTRY_NODE.get(phase)
        if node is None:
            raise ValueError(
                f"fase desconocida: {phase} (esperado 0-{max(PHASE_ENTRY_NODE)})"
            )

        history = list(self.graph.get_state_history(self._config()))
        for i, snap in enumerate(history):
            if node in (snap.next or ()) and i + 1 < len(history):
                # invoke() sobre el config del padre bifurca desde ahí: el gate
                # anterior se re-evalúa (barato) y la fase arranca limpia.
                result = self.graph.invoke(None, history[i + 1].config)
                return self._to_result(result)
        raise LookupError(
            f"no hay checkpoint anterior a {node}: la fase {phase} nunca se alcanzó "
            "en este workspace"
        )

    def answer(self, payload: dict[str, Any]) -> SessionResult:
        """Entrega respuestas (forma CardAnswers) al interrupt activo y avanza
        hasta el próximo interrupt, gate bloqueado o el final."""
        from langgraph.types import Command

        result = self.graph.invoke(Command(resume=payload), self._config())
        return self._to_result(result)

    def pending(self) -> InterviewCard | None:
        """Tarjeta pendiente según el checkpointer (sin ejecutar nada)."""
        snapshot = self.graph.get_state(self._config())
        for task in snapshot.tasks:
            for intr in task.interrupts:
                return InterviewCard.model_validate(intr.value)
        return None

    def iterate(
        self,
        description: str,
        request_type: str = "enhancement",
        affected_nodes: list[str] | None = None,
        *,
        resume: bool = False,
    ) -> dict:
        """Corre (o retoma con ``resume=True``) un ciclo de iteración
        post-migración (Fase 9, sub-grafo con gate propio y su propio thread
        en el MISMO sqlite del checkpointer). Requiere migración base
        completa. Con ``resume``, ``description`` se ignora — el ciclo
        retoma con la instrucción original."""
        from langgraph.checkpoint.sqlite import SqliteSaver

        from sas_migrator.graph.iteration import run_iteration

        self.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.state_dir / CHECKPOINT_FILE), check_same_thread=False
        )
        try:
            return run_iteration(
                self.workspace, description,
                request_type=request_type, affected_nodes=affected_nodes,
                checkpointer=SqliteSaver(conn), resume=resume,
            )
        finally:
            conn.close()

    def status(self) -> SessionResult:
        """Estado actual desde el checkpointer, sin avanzar el grafo."""
        if not (self.state_dir / CHECKPOINT_FILE).exists():
            return SessionResult(status=SessionStatus.NOT_STARTED)
        snapshot = self.graph.get_state(self._config())
        values = snapshot.values or {}
        if not values:
            return SessionResult(status=SessionStatus.NOT_STARTED)
        history = values.get("gate_history") or []
        pending = self.pending()
        if pending is not None:
            status = SessionStatus.WAITING_USER
        elif values.get("done"):
            status = SessionStatus.COMPLETED
        else:
            status = SessionStatus.BLOCKED
        gate = values.get("last_gate") or {}
        return SessionResult(
            status=status,
            phase=int(values.get("current_phase", -1)),
            pending_card=pending,
            gate_errors=[] if gate.get("passed", True) else list(gate.get("errors", [])),
            completed_phases=[g["phase"] for g in history if g.get("passed")],
        )
