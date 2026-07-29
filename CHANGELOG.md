# Changelog

Cambios visibles para quien usa `sas-migrator`. Formato basado en
[Keep a Changelog](https://keepachangelog.com/es/1.1.0/); versionado
[PEP 440](https://peps.python.org/pep-0440/) (`2.0.0a1` = alpha 1 de la 2.0.0).

Regla: aquí entran solo cambios que le importan al usuario del CLI —
comandos, flags, comportamiento, formatos de salida, fixes. Los refactors
internos viven en los commits y las decisiones en `docs/adr/`.

## [Unreleased]

### Added
- La traducción muestra progreso por nodo (`[n/total] CodeTask-x: ok`): una corrida de 75 nodos ya no es minutos de silencio.

### Changed
- `run` anuncia «corrida de migración» en vez de «modo REAL»: el modo por defecto no necesita jerga.

### Fixed
- Conexión externa con SDK ya se puede responder: «2 bcchapi» en una línea vale, y responder solo el paquete tras el aviso completa la elección en vez de degradarla en silencio a replicar con requests.
- El tic `ANIO = ANIO` del traductor se cura solo (y queda declarado en warnings) en vez de quemar reintentos y caer a needs_human — era el 65% de los rechazos de una corrida real.
- Una lista devuelta como string JSON (`cells='["..."]'`) se repara en el acto en vez de agotar los reintentos de validación con el mismo error.
- `rewind --phase N` repetido vuelve a ejecutar la fase: sobre un historial ya rebobinado antes, rebobinar no corría nada y devolvía el bloqueo anterior como si la fase se hubiera rehecho.
- Un `max_tokens` grande (>16000) ya no hace fallar todos los nodos al instante: el SDK veta requests no-streaming largos y ahora ese camino usa streaming; si el veto llegara igual, se reporta como bug accionable en vez de `validation_retries_exhausted`.

## [2.0.0a1] - 2026-07-28

### Added
- Comando `doctor`: chequea entorno, credenciales y peculiaridades del proveedor LLM antes de gastar tokens.
- Comando `reset`: empezar de cero sin borrar a mano lo que no se puede regenerar.
- Rewind: rehacer una fase descarta los resume pendientes en vez de reanudar sobre estado viejo.
- Verificador LLM: un segundo par de ojos sobre cada traducción antes de aceptarla.
- Presupuesto de corrida: el gasto en tokens tiene acumulado visible y tope configurable; los descartes quedan registrados con motivo (ADR-0010).
- Modelo LLM configurable por tarea; backend Claude vía Foundry se elige por config y la credencial por entorno.
- Traducción en paralelo: los nodos ya no se traducen de a uno en fila.
- Query Builder: el SQL que EG generó sobrevive en el log de ejecución.
- Las macro vars van a la celda de parámetros: el período no se congela en el código traducido.
- Logging liviano por celda en los notebooks generados.
- `docs/ARQUITECTURA.md`: el sistema explicado de lo general a cada archivo.

### Changed
- El config rechaza claves desconocidas con nombre y apellido, en vez de ignorarlas.
- Las conexiones externas por HTTP se preguntan al usuario, no se adivinan.
- Entrevista: las preguntas van de a una; las 7 consultas de EG son una decisión repetida, no siete entrevistas.
- Fase 7 dividida en tres sub-nodos y fase 9 reanudable: un interrupt ya no obliga a re-correr trabajo hecho.
- Los gates son predicados puros: el gate 6 ya no ejecuta la auditoría como efecto secundario.
- La allowlist de imports es un contrato declarado, no el azar del venv.
- El split de nodos grandes usa el tokenizador, no regex sobre el crudo.
- Retry con memoria: una ronda de corrección con fallas completas, no cuatro rondas ciegas.
- El código SAS se guarda como array de líneas en los artefactos, no como string ilegible.
- Dependencias con piso verificado y techo de major en `pyproject.toml`.

### Fixed
- Un `529 Overloaded` del proveedor ya no tira abajo una fase de horas.
- El SAS ya no se trunca en silencio (producía traducciones que cubrían el 13% del nodo); los prompts nunca mandan JSON roto.
- El `.env` del repo no se leía desde otro cwd y el error no lo decía; la credencial del comando global ya no depende del cwd.
- Un kill a mitad de escritura ya no deja artefactos truncados; un artefacto ilegible bloquea el gate con motivo en vez de crashearlo.
- Re-ejecutar una fase ya no duplica la cola `needs_human`.
- Un hueco en los datos ya no se rellena en silencio entregando números equivocados.
- El cliente LLM reporta usage fresco y truncado visible, con tope por tarea.
- Los ejemplos del README no corrían: el workspace es opción, no posicional; migrar no exige flag.

## [2.0.0a0] - 2026-07-26

Línea base de la v2: pipeline completo de migración SAS EG → Python
(etapas 0–6 del plan canónico) — parser de .egp, grafo LangGraph con
checkpoints y gates, traducción LLM con cascada de validación, harness
de integración con .egp reales y eval set recorded.
