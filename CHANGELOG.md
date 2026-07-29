# Changelog

Cambios visibles para quien usa `sas-migrator`. Formato basado en
[Keep a Changelog](https://keepachangelog.com/es/1.1.0/); versionado
[PEP 440](https://peps.python.org/pep-0440/) (`2.0.0a1` = alpha 1 de la 2.0.0).

Regla: aquí entran solo cambios que le importan al usuario del CLI —
comandos, flags, comportamiento, formatos de salida, fixes. Los refactors
internos viven en los commits y las decisiones en `docs/adr/`.

## [Unreleased]

### Added
- La traducción muestra progreso por nodo: anuncia el arranque (`→ [3/27] CodeTask-x …`) y el cierre con su duración y el acumulado (`✔ [3/27] CodeTask-x: ok · 94s · 3/27 listos`). Un nodo grande son varios minutos: avisar solo al terminar dejaba la consola muda todo ese rato.
- Cada notebook dice de qué Process Flow del .egp salió, con su nombre y su id. El nombre del archivo es posicional (`NB-NN_<slug>`): si cambia el alcance, la numeración corre y saber qué flujo era un notebook obligaba a cruzar artefactos a mano.
- Una traducción que cubre una fracción del nodo se rechaza en vez de aceptarse: se compara qué tablas crea el SAS contra cuáles nombra el Python, y si faltan demasiadas el nodo se reintenta con la lista de faltantes y, si no se recupera, va a needs_human como `incomplete_translation`. Antes un nodo de 152 KB de SAS traducido al 3% pasaba todos los chequeos y llegaba al notebook: parsea, no tiene patrones prohibidos, los imports resuelven.
- Aunque el nodo pase, las tablas de salida que no aparecen en la traducción quedan anotadas en sus `warnings` y en la auditoría: 65 de 66 ya no pasa en silencio.
- `llm.thinking` y `llm.effort` en `project_config.yaml`, con su variante por tarea (`thinking_by_task` / `effort_by_task`) como el resto de las opciones del LLM. Ausentes no se mandan y rige el default del modelo — que no es el mismo entre familias: hasta ahora cambiar `model` de Haiku a Sonnet activaba el razonamiento sin que nada lo dijera. Un valor inválido falla al cargar la config, no en la primera llamada.

### Changed
- `run` anuncia «corrida de migración» en vez de «modo REAL»: el modo por defecto no necesita jerga.
- Los nodos grandes se parten en tramos de 40 KB de SAS en vez de 120 KB. El techo no lo fijaba la ventana de contexto sino la resistencia del modelo: entraba entero en el prompt y devolvía una traducción abreviada, que ningún `stop_reason` reporta. Un tramo más chico es una tarea que el modelo sí termina.
- Interpolar el NOMBRE de una tabla en un SQL ahora está permitido (`f"... FROM TABLAS.BD_R{ANIO}{TRIM}"`) y solo se rechaza el VALOR interpolado. La regla anterior era insatisfacible para los nodos donde el SAS arma el nombre con una macro var, y el modelo la esquivaba escribiendo el mismo string sin la `f` — que pasaba el chequeo y mandaba las llaves literales a la base. Esa forma ahora también se detecta.

### Fixed
- Conexión externa con SDK ya se puede responder: «2 bcchapi» en una línea vale, y responder solo el paquete tras el aviso completa la elección en vez de degradarla en silencio a replicar con requests.
- El tic `ANIO = ANIO` del traductor se cura solo (y queda declarado en warnings) en vez de quemar reintentos y caer a needs_human — era el 65% de los rechazos de una corrida real.
- Una lista devuelta como string JSON (`cells='["..."]'`) se repara en el acto en vez de agotar los reintentos de validación con el mismo error.
- La consola deja de inundarse con `SyntaxWarning: invalid escape sequence` del código traducido: una regex en string no-raw disparaba tres advertencias por cada pasada de parseo y ahogaba el progreso por nodo. El notebook conserva el literal, así que la advertencia reaparece al ejecutarlo — cuando sí se puede hacer algo.
- `rewind --phase N` repetido vuelve a ejecutar la fase: sobre un historial ya rebobinado antes, rebobinar no corría nada y devolvía el bloqueo anterior como si la fase se hubiera rehecho.
- Un `max_tokens` grande (>16000) ya no hace fallar todos los nodos al instante: el SDK veta requests no-streaming largos y ahora ese camino usa streaming; si el veto llegara igual, se reporta como bug accionable en vez de `validation_retries_exhausted`.
- Un nodo traducido bien ya no se descarta del notebook por dos falsos positivos del chequeo de nombres sin definir: un `for i, (name, group) in ...` dentro de una función, y el patrón `if 'x' in locals():` con el que la traducción consume un DataFrame que puede venir de otro nodo. En una corrida real esto tiró 4 nodos válidos, entre ellos el que inicializa la tabla base de todo el flujo.

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
