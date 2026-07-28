"""Preflight del workspace — lo que `sas-migrator doctor` verifica antes de correr.

Una corrida real dura horas y gasta tokens. Todo lo que puede estar mal antes
de la primera llamada al LLM —falta el `.egp`, el `project_config.yaml` no
parsea, la credencial del proveedor configurado no está en ningún `.env`, el
extra de un paso posterior no está instalado— se puede saber en un segundo.
Descubrirlo en la fase 2, después de contestar la entrevista inicial, es el
peor momento posible.

Los chequeos son de solo lectura y no tocan la red: verifican presencia y
forma, no que el servidor conteste. La única excepción deliberada es que NO se
valida la key contra el API — eso cuesta una llamada y el error del proveedor
ya es claro cuando llega.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    """Un chequeo con su veredicto y, si falló, cómo arreglarlo.

    ``hint`` no es opcional por cortesía: un chequeo que dice "falta X" sin
    decir dónde ponerlo deja al usuario en la misma cacería que el error que
    vino a evitar.
    """

    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    workspace: Path
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        """Sin FAIL. Los WARN no bloquean: describen lo que faltará más tarde."""
        return not self.failures


# ── Chequeos individuales ────────────────────────────────────────────────────


def check_workspace(workspace: Path) -> list[Check]:
    """Estructura mínima: el directorio, `input/egp/` y exactamente un `.egp`."""
    ws = Path(workspace)
    if not ws.is_dir():
        return [
            Check(
                "workspace",
                FAIL,
                f"{ws} no existe o no es un directorio",
                "Creá el workspace o pasá --workspace con la ruta correcta.",
            )
        ]

    checks = [Check("workspace", OK, str(ws.resolve()))]
    egp_dir = ws / "input" / "egp"
    if not egp_dir.is_dir():
        checks.append(
            Check(
                "input/egp/",
                FAIL,
                "no existe",
                f"mkdir {egp_dir} y copiá ahí el .egp a migrar.",
            )
        )
        return checks

    egps = sorted(egp_dir.glob("*.egp"))
    if not egps:
        checks.append(
            Check("input/egp/", FAIL, "sin archivos .egp", f"Copiá el .egp en {egp_dir}.")
        )
    elif len(egps) > 1:
        # No es un error: `run --egp` desempata. Sin el flag, `start` falla.
        checks.append(
            Check(
                "input/egp/",
                WARN,
                f"{len(egps)} archivos .egp: {', '.join(p.name for p in egps)}",
                "Con más de uno hay que elegir: sas-migrator run --egp <ruta>.",
            )
        )
    else:
        checks.append(Check("input/egp/", OK, egps[0].name))

    docs = ws / "input" / "docs"
    if docs.is_dir() and any(docs.iterdir()):
        checks.append(Check("input/docs/", OK, f"{sum(1 for _ in docs.iterdir())} archivo(s)"))
    return checks


def check_references(workspace: Path) -> list[Check]:
    """`input/data/` es el ground truth de la cascada de validación (fase 7)."""
    data = Path(workspace) / "input" / "data"
    files = sorted(data.glob("*.csv")) + sorted(data.glob("*.xlsx")) if data.is_dir() else []
    if not files:
        return [
            Check(
                "input/data/",
                WARN,
                "sin referencias",
                "La cascada de la fase 7 compara contra las salidas SAS; sin "
                "ellas la validación queda sin ground truth.",
            )
        ]
    return [Check("input/data/", OK, f"{len(files)} referencia(s)")]


def check_config(workspace: Path) -> list[Check]:
    """`project_config.yaml`: que exista, que parsee, y que tenga lo de fase 7."""
    from sas_migrator.core.config import CONFIG_FILENAME, load_project_config

    path = Path(workspace) / CONFIG_FILENAME
    if not path.exists():
        return [
            Check(
                CONFIG_FILENAME,
                WARN,
                "no existe — aplican defaults neutros",
                "Copiá project_config.example.yaml al workspace. Sin BD "
                "configurada la fase 7 no puede verificar tablas.",
            )
        ]
    try:
        config = load_project_config(workspace)
    except Exception as exc:  # yaml inválido o campo que no valida
        return [
            Check(
                CONFIG_FILENAME,
                FAIL,
                f"no se pudo leer: {exc}",
                "Corregí el YAML; el modelo de referencia es "
                "project_config.example.yaml.",
            )
        ]

    checks = [Check(CONFIG_FILENAME, OK, str(path))]
    if config.db.connection_url:
        checks.append(Check("db", OK, "connection_url explícita"))
    elif config.db.default_server:
        checks.append(Check("db", OK, f"default_server={config.db.default_server}"))
    else:
        checks.append(
            Check(
                "db",
                WARN,
                "sin db.default_server ni db.connection_url",
                "La fase 7 (verify_tables + cascada) la necesita. Las fases 0-6 corren igual.",
            )
        )
    if config.run.macro_params:
        checks.append(
            Check("run.macro_params", OK, ", ".join(f"{k}={v}" for k, v in config.run.macro_params.items()))
        )
    else:
        checks.append(
            Check(
                "run.macro_params",
                WARN,
                "vacío",
                "Las macro vars que el .egp heredaba del entorno (período, "
                "trimestre) van acá; sin valor el notebook falla al ejecutar.",
            )
        )
    return checks


def check_credential(workspace: Path) -> list[Check]:
    """La credencial del proveedor que la config elige, buscada donde de verdad se busca."""
    import os

    from sas_migrator.core.config import load_project_config

    try:
        config = load_project_config(workspace)
    except Exception:
        return []  # ya lo reportó check_config; no duplicar el ruido

    from sas_migrator.llm.runtime import caller_injected

    if caller_injected():
        return [Check("llm", OK, "caller inyectado — no se usa credencial")]

    provider = config.llm.provider
    from sas_migrator.llm.env import USER_ENV, load_env

    load_env(workspace)

    var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "ANTHROPIC_FOUNDRY_API_KEY"
    checks: list[Check] = [Check("llm.provider", OK, f"{provider} · modelo {config.llm.model}")]
    if os.environ.get(var):
        checks.append(Check(var, OK, "definida"))
    else:
        from sas_migrator.llm.env import describe_sources

        checks.append(
            Check(
                var,
                FAIL,
                "no definida",
                # El ✓ dice "se leyó", no "define la variable": sin aclararlo, un
                # .env existente que no la define parece contradecir el error.
                f"{describe_sources()}\n(✓ = el archivo se leyó, no que "
                f"definiera {var})\nUna key por máquina, desde cualquier "
                f"carpeta: {USER_ENV}",
            )
        )
    if provider == "foundry":
        resource = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or config.llm.foundry_resource
        if resource:
            checks.append(Check("foundry_resource", OK, resource))
        else:
            checks.append(
                Check(
                    "foundry_resource",
                    FAIL,
                    "no definido",
                    "llm.foundry_resource en project_config.yaml (no es secreto) "
                    "o ANTHROPIC_FOUNDRY_RESOURCE en el entorno.",
                )
            )
    return checks


# Módulo importable → (etiqueta, extra que lo trae, para qué, bloquea)
_DEPS = [
    ("langgraph", "graph", "orquestación de fases", True),
    ("langgraph.checkpoint.sqlite", "graph", "checkpoint (resume/rewind)", True),
    ("anthropic", "llm", "llamadas LLM de una corrida real", True),
    ("dotenv", "llm", "lectura de .env", False),
    ("sqlalchemy", "db", "verify_tables y cascada (fase 7)", False),
    ("nbclient", "notebook", "ejecución de notebooks (fase 7)", False),
    ("mcp", "mcp", "servidor MCP (serve)", False),
]


def check_dependencies(*, stub: bool = False) -> list[Check]:
    """Extras instalados. Un extra faltante se ve al importar, en mitad de la fase."""
    checks: list[Check] = []
    for module, extra, purpose, blocking in _DEPS:
        try:
            present = find_spec(module) is not None
        except (ImportError, ValueError):
            present = False
        if present:
            continue
        # En modo stub el extra `llm` no bloquea: es justamente el modo que corre
        # el pipeline completo sin proveedor (CI, smoke test).
        if stub and extra == "llm":
            blocking = False
        checks.append(
            Check(
                module,
                FAIL if blocking else WARN,
                f"no instalado — {purpose}",
                f'pip install -e ".[{extra}]"',
            )
        )
    if not checks:
        checks.append(Check("extras", OK, "todos instalados"))
    return checks


def check_target_env(workspace: Path) -> list[Check]:
    """¿El entorno local puede EJECUTAR lo que las traducciones pueden importar?

    La allowlist define qué puede importar el código destino; la fase 7 lo
    ejecuta acá. Una librería permitida y no instalada no rompe la traducción
    (el chequeo estático no mira el entorno) pero sí la ejecución — mejor
    saberlo antes que en el notebook 14 de 17.
    """
    from sas_migrator.core.config import load_project_config

    try:
        allowed = load_project_config(workspace).translation.allowed_imports
    except Exception:
        return []  # config rota: ya la reportó check_config
    missing = []
    for name in sorted(set(allowed)):
        try:
            if find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if not missing:
        return [Check("translation.allowed_imports", OK, "todas instaladas localmente")]
    return [
        Check(
            "translation.allowed_imports",
            WARN,
            f"sin instalar localmente: {', '.join(missing)}",
            "La traducción no las necesita, pero la fase 7 ejecuta los "
            f"notebooks acá: pip install {' '.join(missing)}",
        )
    ]


def check_state(workspace: Path) -> list[Check]:
    """Migración en curso: decir que existe evita un `run` que arranca de cero."""
    from sas_migrator.service.session import CHECKPOINT_FILE

    ckpt = Path(workspace) / "state" / CHECKPOINT_FILE
    if not ckpt.exists():
        return [Check("checkpoint", OK, "sin migración iniciada")]
    return [
        Check(
            "checkpoint",
            OK,
            "hay una migración en curso",
            "`resume` la retoma; `run` arranca de cero sobre el mismo estado.",
        )
    ]


def run_checks(workspace: Path, *, stub: bool = False) -> Report:
    """Corre todos los chequeos. ``stub=True`` omite los que solo importan al LLM."""
    ws = Path(workspace)
    checks = check_workspace(ws)
    if any(c.status == FAIL and c.name == "workspace" for c in checks):
        return Report(ws, checks)  # sin directorio, el resto no tiene sentido
    checks += check_config(ws)
    checks += check_references(ws)
    if not stub:
        checks += check_credential(ws)
    checks += check_dependencies(stub=stub)
    checks += check_target_env(ws)
    checks += check_state(ws)
    return Report(ws, checks)


def credential_report(workspace: Path) -> Report:
    """Solo la credencial — el preflight que corre `run` antes de gastar nada."""
    return Report(Path(workspace), check_credential(Path(workspace)))
