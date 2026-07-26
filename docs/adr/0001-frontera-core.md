# ADR-0001 — core/ es determinista puro y no conoce el transporte

**Estado**: aceptada (Etapa 1; extendida en Etapa 3)

## Decisión

`core/` no puede importar `langgraph`, `anthropic`, `sas_migrator.graph`,
`sas_migrator.llm`, `mcp`, `sas_migrator.mcp_server` ni `sas_migrator.service`.

La extensión de la Etapa 3: la **construcción de entrevistas es core**
(`core/interview/` — builders de tarjetas, validación de respuestas,
escritores de decisión). No puede depender del transporte (LangGraph
`interrupt()`, MCP, CLI): los nodos del grafo solo llaman `interrupt(card)` y
los clientes solo renderizan.

## Por qué

- Todo lo que decide algo debe ser testeable sin API key, sin grafo y sin
  servidor: los tests de `core/interview` corren contra un directorio.
- Cambiar de transporte (otra UI, otro protocolo) no puede tocar la lógica de
  qué se pregunta ni cómo se validan las respuestas.

## Enforcement

`tests/unit/test_graph.py::test_core_does_not_import_llm_or_graph` con
`FORBIDDEN_IN_CORE` — un import prohibido es un test rojo, no una convención.
