# ADR-0005 — MigrationSession es la única vía al grafo; MCP y CLI son clientes

**Estado**: aceptada (Etapa 3)

## Decisión

1. `service/session.py::MigrationSession` envuelve `build_graph(SqliteSaver)`
   con `thread_id="migration"` (una migración por workspace) y expone
   `start / answer / resume / pending / status`.
2. La CLI la usa **in-process**; las tools MCP (`mcp_server/server.py`,
   FastMCP por stdio, `sas-migrator serve`) son wrappers delgados sobre la
   misma sesión. Un solo camino de código testeado para ambos frentes.
3. Contrato de tools (estable para VS Code / Claude Code):
   `start_migration, status, get_pending_question, answer, approve_plan,
   authorize_execution, iterate`.
4. Las tools de fases futuras (`authorize_execution` → Etapa 5, `iterate` →
   Etapa 7) responden `{"status": "not_available", ...}` con el mensaje de
   cuándo estarán: **nunca fingen éxito ni escriben nada**.
5. La fuente de verdad del interrupt pendiente es
   `graph.get_state(config).tasks[*].interrupts` — no hay espejo del payload
   en el estado del grafo (se eliminó `pending_interrupt`).

## Por qué

Se evaluó CLI hablando MCP por stdio a un subproceso: más fiel al eslogan
"la CLI es el cliente de referencia", pero duplica el costo de test/debug sin
beneficio para el DoD (que exige migración conducida desde la CLI). La capa
compartida da la misma garantía — CLI y MCP no pueden divergir porque son la
misma función — con tests síncronos simples.

## Enforcement

`tests/unit/test_session.py` (CLI e2e por guion) y
`tests/unit/test_mcp_tools.py` (migración completa solo con tools; noops
honestos sin efectos en disco).
