"""Configuración de proyecto — todo lo específico de un cliente/entorno vive aquí.

El código genérico no puede traer defaults de un cliente (servidores, heurísticas
de dominio, marcadores de auditoría). Esos valores se cargan desde
``project_config.yaml`` en la raíz del workspace de migración; sin archivo,
aplican defaults neutros y los pasos que requieren un valor (p. ej. servidor de
BD) fallan con un error claro en vez de apuntar silenciosamente a infra ajena.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CONFIG_FILENAME = "project_config.yaml"


class _StrictSection(BaseModel):
    """Toda sección del config rechaza claves desconocidas.

    Con extra permisivo, un typo (`max_worker` por `max_workers`) se ignora en
    silencio y el usuario cree haber configurado algo que no configuró. Mejor
    un error al cargar, con la clave exacta, que una corrida entera con el
    default equivocado.
    """

    model_config = ConfigDict(extra="forbid")


class DBConfig(_StrictSection):
    """Defaults de conexión SQL Server para el proyecto."""

    default_server: str = ""
    default_port: int = 1433
    default_driver: str = "ODBC Driver 17 for SQL Server"
    # Hoy la conexión usa siempre AD integrated + Encrypt. Se mantienen como
    # default pero el certificado ya no se confía a ciegas salvo opt-in.
    trust_server_certificate: bool = False
    # URL SQLAlchemy completa que REEMPLAZA la construcción mssql+pyodbc
    # (BD de prueba: sqlite:///..., LocalDB, u otro dialecto). Vacía = flujo
    # normal SQL Server con AD integrated.
    connection_url: str = ""


class AuditConfig(_StrictSection):
    """Lo que la auditoría de traducción no puede leer del SAS.

    Los endpoints que el flujo consulta y las tablas que puebla NO se declaran:
    se infieren nodo por nodo del ``URL=`` y del destino de escritura del propio
    código SAS (``core.http_evidence.extract_http_hosts`` /
    ``core.audit.extract_dest_tables``), de
    modo que las reglas de deriva corren en cualquier proyecto sin configurar
    nada. Acá queda solo lo que el SAS no dice: qué formas de manejar secretos
    aceptas y qué DataFrames quieres vigilar en runtime.
    """

    env_secret_markers: list[str] = Field(
        default_factory=lambda: ["os.environ", "dotenv"]
    )
    runtime_df_checks: list[str] = Field(default_factory=list)


class LlmConfig(_StrictSection):
    """Configuración de los nodos LLM (Etapa 4).

    ``model`` va pineado (ID fijo, sin sufijo de fecha). Sin parámetros de
    sampling: los modelos actuales los removieron del API; el determinismo se
    persigue con structured outputs y prompts, no con temperature.

    ``provider`` elige el backend: ``anthropic`` (API directa) o ``foundry``
    (Claude servido por Microsoft Foundry / Azure AI Foundry). Las credenciales
    NUNCA viven acá — este archivo se commitea. Van por entorno o ``.env``:
    ``ANTHROPIC_API_KEY``, o ``ANTHROPIC_FOUNDRY_API_KEY`` para Foundry.
    ``foundry_resource`` sí va acá: es el nombre del recurso de Azure
    (``https://<resource>.services.ai.azure.com``), infraestructura, no secreto.
    """

    provider: Literal["anthropic", "foundry"] = "anthropic"
    model: str = "claude-opus-5"
    # Modelo POR TAREA (translation, verify, analysis_reviews, improvements,
    # matching, mismatch_diagnosis, docs). Un solo modelo para todo es el
    # anti-patrón medido en producción (haiku-para-todo): la traducción y el
    # verificador ameritan el modelo fuerte; matching o docs pueden ir en un
    # tier menor. Ausente la clave, rige `model`.
    models_by_task: dict[str, str] = Field(default_factory=dict)
    # Razonamiento del modelo antes de responder: "adaptive" (el modelo decide
    # cuánto pensar) o "disabled". None NO significa "sin pensar": significa que
    # el parámetro no se manda y rige el default del modelo — y ese default NO
    # es el mismo entre familias. Haiku 4.5 no piensa si no se lo piden;
    # Sonnet 5 / Opus 4.7+ piensan por default. Dos modelos, el mismo código, y
    # el comportamiento cambia solo por el ID: dejarlo implícito era el hueco.
    # Los tokens de pensamiento se facturan como salida y cuentan contra
    # max_tokens, así que subir esto pide revisar aquel.
    # OJO: "adaptive" es de la generación 4.6+. Los modelos viejos (Haiku 4.5,
    # Sonnet 4.5) usan otra forma y lo rechazan — por eso hay _by_task.
    thinking: Literal["adaptive", "disabled"] | None = None
    thinking_by_task: dict[str, str] = Field(default_factory=dict)
    # Profundidad de razonamiento y gasto de tokens: low|medium|high|xhigh|max.
    # None = no se manda y rige el default del modelo (high). Para traducción y
    # código, "xhigh" es el recomendado por Anthropic; "low"/"medium" bajan
    # costo y latencia a cambio de menos elaboración.
    # OJO: no todos los modelos lo aceptan — Haiku 4.5 devuelve error si se lo
    # mandás. Con modelos mezclados, usar effort_by_task y no el global.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    effort_by_task: dict[str, str] = Field(default_factory=dict)
    # Llamadas al LLM en paralelo dentro de una fase: análisis por Process Flow
    # (fase 2) y traducción por nodo (fase 6/9). Default 1 = secuencial; opt-in
    # consciente. Con N>1 la primera llamada va sola (calienta el prompt cache:
    # N requests simultáneos no leen lo que otro está escribiendo) y el resto
    # por pool de threads. Lo que se escribe a disco no depende de N: cada
    # unidad persiste su propio archivo y el ensamblado final es SIEMPRE
    # secuencial en el orden del plan — notebooks byte-idénticos.
    max_workers: int = 1
    max_tokens: int = 16000
    # Presupuesto de salida POR TAREA (analysis, matching, translate, verify,
    # diagnose, docs). Un solo max_tokens para 6 tareas obliga a dimensionar
    # todo para la peor: la traducción de un nodo de 600 LOC necesita mucho más
    # que un matching. Ausente la clave, rige max_tokens.
    max_tokens_by_task: dict[str, int] = Field(default_factory=dict)
    max_validation_retries: int = 3
    # Reintentos de TRANSPORTE (429, 5xx, 529 Overloaded) que hace el SDK con
    # backoff exponencial. Su default es 2, pensado para una petición suelta:
    # una fase que traduce decenas de nodos durante horas atraviesa ventanas de
    # sobrecarga del proveedor, y un 529 sin reintentar corta la corrida entera.
    # No tiene nada que ver con max_validation_retries (respuestas mal formadas).
    max_transport_retries: int = 8
    # Cómo se obtiene el objeto estructurado:
    #   native → output_config.format (structured outputs del API)
    #   tool   → tool use no-strict + validación Pydantic local
    #   auto   → native, con degradación a tool si el backend lo rechaza
    # Los workspaces de Foundry sin structured_outputs habilitado necesitan
    # `tool`; `auto` lo detecta solo, sin tocar config.
    structured_mode: Literal["auto", "native", "tool"] = "auto"
    # Solo para provider=foundry. Puede sobreescribirse por entorno con
    # ANTHROPIC_FOUNDRY_RESOURCE (útil si dev/prod usan recursos distintos).
    foundry_resource: str = ""
    # Tope de gasto de la corrida en USD (0 = sin tope). Se evalúa ANTES de
    # cada llamada contra el acumulado del trace: al alcanzarlo, la corrida
    # corta con BudgetExceeded — reanudable con `resume` tras subir el tope,
    # nunca un NeedsHuman. El acumulado sobrevive reinicios (relee el trace).
    max_run_cost_usd: float = 0.0
    # Override de la tabla de precios por prefijo de modelo (USD por MTok):
    #   prices: {"claude-opus": {input: 5, output: 25, cache_read: 0.5, cache_write: 6.25}}
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("thinking", mode="before")
    @classmethod
    def _yaml_bool_is_thinking_mode(cls, v):
        # YAML 1.1 parsea `off`/`no`/`false` como False: `thinking: off` es lo
        # natural de escribir y no puede ser un error de validación.
        if v is False:
            return "disabled"
        if v is True:
            return "adaptive"
        return v

    @field_validator("thinking_by_task", "effort_by_task", mode="after")
    @classmethod
    def _valores_por_tarea_validos(cls, v, info):
        # Un typo acá se descubría gastando tokens: el valor viajaba al API y
        # volvía 400 recién en la primera llamada de la fase.
        validos = (
            {"adaptive", "disabled"}
            if info.field_name == "thinking_by_task"
            else {"low", "medium", "high", "xhigh", "max"}
        )
        for tarea, valor in v.items():
            texto = "disabled" if valor is False else "adaptive" if valor is True else str(valor)
            if texto not in validos:
                raise ValueError(
                    f"llm.{info.field_name}.{tarea}: '{valor}' no es válido "
                    f"(opciones: {', '.join(sorted(validos))})"
                )
            v[tarea] = texto
        return v


class TranslationConfig(_StrictSection):
    """Contrato del código Python DESTINO.

    ``allowed_imports``: librerías de terceros que las traducciones pueden
    importar (además de la stdlib). Es la unión de lo que los prompts y la
    auditoría ya daban por buenas — antes vivía repartida en tres capas que se
    contradecían: el prompt decía "pandas, numpy o sqlalchemy", la tabla de
    patrones sugería requests/matplotlib, y el chequeo estático validaba contra
    el entorno del MIGRADOR (que no es el entorno donde correrán los
    notebooks) y rechazó nodos correctos. El entorno destino lo documenta
    ``output/requirements.txt``, que el ensamblador emite desde esta lista.
    """

    allowed_imports: list[str] = Field(
        default_factory=lambda: [
            "matplotlib",
            "numpy",
            "openpyxl",
            "pandas",
            "pyreadstat",
            "requests",
            "scipy",
            "sqlalchemy",
        ]
    )
    # Verificador LLM post-chequeo estático (fase 6): un segundo par de ojos
    # que compara SAS vs traducción buscando semántica perdida o cambiada.
    #   all → todos los nodos (default: el costo de UN nodo mal traducido en
    #         producción supera el de verificar los 75)
    #   low → solo los nodos cuya traducción declaró confidence=low
    #   off → sin verificador
    verify: Literal["off", "low", "all"] = "all"
    # Logging liviano por celda en los notebooks generados (`_log` al final de
    # las celdas con resultados importantes + output/log/<notebook>.log). Este
    # flag es la RECOMENDACIÓN de la tarjeta B5b de la entrevista; la decisión
    # que manda es la del usuario (state/cell_logging.yaml).
    cell_logging: bool = False
    # ¿Dónde corre el procesamiento por default?
    #   sql    → en la BD. WORK es un schema temporal del server (tabla temp),
    #            no memoria del proceso, y solo la lógica PROCEDURAL de un DATA
    #            step (retain/lag/first.-last./do) baja los datos al cliente.
    #   pandas → comportamiento histórico: WORK ⇒ DataFrame y cualquier
    #            statement dentro de un DATA step ⇒ no-SQL.
    # Los datos viven en SQL Server: bajar una tabla entera a memoria para
    # hacerle UPDATE fila por fila no es una traducción fiel del SAS, que
    # ejecutaba ese UPDATE en la base. Se puede volver a `pandas` por proyecto
    # (p. ej. flujos que en realidad trabajan sobre .sas7bdat en disco).
    default_target: Literal["sql", "pandas"] = "sql"

    @field_validator("verify", mode="before")
    @classmethod
    def _yaml_bool_is_verify_mode(cls, v):
        # YAML 1.1 parsea `off` como False: escribir `verify: off` sin comillas
        # es lo natural y no puede ser un error de validación.
        if v is False:
            return "off"
        if v is True:
            return "all"
        return v


class ParserConfig(_StrictSection):
    """Ajustes del parser SAS para el proyecto.

    ``extra_db_engines``: engines de BD del cliente que no están en la lista
    default de SAS/ACCESS (alias custom, engines exóticos). ``ignore_db_engines``
    los remueve del set (p. ej. un engine que el cliente usa para archivos).
    La resolución vive en ``core.parser.statements.resolve_db_engines``.
    """

    extra_db_engines: list[str] = Field(default_factory=list)
    ignore_db_engines: list[str] = Field(default_factory=list)


class RunConfig(_StrictSection):
    """Valores de corrida: los parámetros con los que se ejecuta el proceso.

    ``macro_params`` da valor a las variables macro que el SAS heredaba del
    entorno (autoexec del servidor, prompts de EG) y que el .egp por lo tanto
    no define — típicamente el período: ``{ANIO: 2024, TRIM: 4}``. Van acá y no
    en el código porque cambian en cada corrida; la celda ``parameters`` del
    notebook las expone para poder inyectarlas desde afuera.

    Sin valor, el notebook falla al ejecutar diciendo cuál falta: correr un
    proceso trimestral sin declarar el trimestre no es un default razonable.
    """

    macro_params: dict[str, Any] = Field(default_factory=dict)


class ProjectConfig(_StrictSection):
    """Raíz de project_config.yaml."""

    db: DBConfig = Field(default_factory=DBConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)


def load_project_config(workspace: Path | str | None = None) -> ProjectConfig:
    """Carga project_config.yaml desde el workspace; defaults neutros si no existe."""
    if workspace is None:
        return ProjectConfig()
    path = Path(workspace)
    if path.is_dir():
        path = path / CONFIG_FILENAME
    if not path.exists():
        return ProjectConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        unknown = [
            ".".join(str(p) for p in err["loc"])
            for err in exc.errors()
            if err["type"] == "extra_forbidden"
        ]
        if unknown:
            raise ValueError(
                f"{path.name} tiene clave(s) desconocida(s): {', '.join(sorted(unknown))} "
                "— ¿typo o campo que ya no existe? La referencia de claves "
                "válidas es el archivo comentado que escribe `sas-migrator init`."
            ) from exc
        raise
