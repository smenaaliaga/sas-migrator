# ADR-0007 — Ejecución autorizada, validación dialect-agnóstica e iteración con gate

**Estado**: aceptada (Etapa 5)

## Decisión

1. **La pausa de ejecución es sagrada**: los notebooks solo se ejecutan
   (nbclient, in-process) detrás del interrupt `execution_approval`, cuyo
   default recomendado es NO ejecutar — el camino fácil jamás dispara
   efectos irreversibles. `authorize_execution` (MCP) responde esa tarjeta.
   Faltan tablas DESTINO (`table_verification`) ⇒ la ejecución no corre
   aunque esté autorizada: el sistema no crea tablas.
2. **La capa de BD es dialect-agnóstica vía SQLAlchemy**: `db.connection_url`
   reemplaza la construcción mssql+pyodbc completa (BD de prueba
   sqlite/LocalDB para CI y DoD; producción sigue siendo SQL Server con AD
   integrated); `verify_tables` usa el inspector y la cascada cita nombres
   según el dialecto. La URL llega a los notebooks por `SASMIG_DB_URL`
   (bootstrap del ensamblador cuando hay conexiones) — notebooks standalone,
   sin secretos.
3. **La validación tiene sustancia en el gate 7**: cascada full con
   FAIL/ERROR bloquea; modo blocked (sin acceso) bloquea; un notebook FAIL en
   ejecución autorizada bloquea. Los modos `not_applicable`/`blocked`
   conservan sus exit codes semánticos (0/3). Los mismatches se diagnostican
   con el nodo LLM de los 8 patrones (`MismatchCause`) → `MismatchDiagnosis`
   tipado en `validation_report.diagnoses`.
4. **La Fase 9 dejó de ser honor system**: la iteración es un sub-grafo
   (`iterate_apply → iteration_gate`) que registra una `IterationEntry`
   obligatoria, re-traduce solo los nodos afectados (traducciones
   persistidas en `state/translations/`), re-audita y RE-CORRE la
   validación. El gate exige entry completada + `validation_result` poblado
   (WARN honesto sin insumos) + auditoría sin high + validación sin FAIL —
   cerrar un ciclo sin validar es imposible por topología.
5. **doc-writer** (fase 8) es el único nodo donde la prosa libre es el
   producto; ante `NeedsHuman` cae al template y el gate 8 bloquea por el
   item registrado — nunca docs vacíos ni silencio.

## Enforcement

`test_db_validation.py` (verify/cascada vs sqlite, exits),
`test_execution.py` (pausa sagrada e2e), `test_validation_docs.py`
(diagnóstico + gates 7/8), `test_iteration.py` (gate de ciclo) y
`test_e2e_testdb.py` — el DoD: pipeline completo contra una BD de prueba
(sqlite siempre; LocalDB/SQL Server real en CI windows-latest).
