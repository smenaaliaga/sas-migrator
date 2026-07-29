"""`sas-migrator init` — que un workspace recién creado sea uno que `doctor` acepta.

El valor del comando no es escribir archivos: es que lo que escribe sea
exactamente lo que el preflight y el cargador de config esperan. Por eso el test
principal encadena `init` con `doctor` en vez de comparar nombres de archivo, y
por eso hay un test que exige que toda clave de `ProjectConfig` esté en el
template: el ejemplo se desincronizó del modelo antes y nadie se enteró.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sas_migrator.cli.main import EXIT_USAGE, app
from sas_migrator.core.config import CONFIG_FILENAME, ProjectConfig, load_project_config
from sas_migrator.core.utils.scaffold import (
    CREATED,
    EXISTED,
    TEMPLATES,
    WS_DIRS,
    init_workspace,
    template_text,
)
from sas_migrator.testing.egp_builder import build_egp


@pytest.fixture(autouse=True)
def _fake_llm():
    """`doctor` chequea la credencial del proveedor; con caller inyectado no hay
    credencial que buscar y el test no depende del entorno de la máquina."""
    from sas_migrator.llm import runtime
    from sas_migrator.testing.fake_llm import default_fake_caller

    runtime.set_caller(default_fake_caller())
    yield
    runtime.set_caller(None)


def test_init_deja_un_workspace_que_doctor_acepta(tmp_path: Path) -> None:
    """El contrato entero: init escribe, doctor no bloquea."""
    egp = tmp_path / "origen" / "proyecto.egp"
    egp.parent.mkdir()
    build_egp(egp)
    ws = tmp_path / "mi_migracion"

    result = CliRunner().invoke(app, ["init", "-w", str(ws), "--egp", str(egp)])
    assert result.exit_code == 0, result.output

    for rel in WS_DIRS:
        assert (ws / rel).is_dir(), rel
    for dest in TEMPLATES.values():
        assert (ws / dest).is_file(), dest
    # El .egp se COPIA: el original sigue donde estaba.
    assert (ws / "input" / "egp" / "proyecto.egp").is_file()
    assert egp.is_file()
    # Nunca un .env: el archivo con el secreto lo crea el usuario.
    assert not (ws / ".env").exists()
    assert "doctor" in result.output  # el próximo paso, con el -w que se usó

    doctor = CliRunner().invoke(app, ["doctor", "-w", str(ws)])
    assert doctor.exit_code == 0, doctor.output


def test_init_no_pisa_lo_que_ya_existe(tmp_path: Path) -> None:
    """Correrlo dos veces es seguro: por eso no hay --force."""
    ws = tmp_path / "ws"
    init_workspace(ws)
    editado = "llm:\n  model: claude-sonnet-5\n"
    (ws / CONFIG_FILENAME).write_text(editado, encoding="utf-8")
    (ws / "input" / "data" / "referencia.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    items = init_workspace(ws)

    assert (ws / CONFIG_FILENAME).read_text(encoding="utf-8") == editado
    assert (ws / "input" / "data" / "referencia.csv").is_file()
    assert all(i.status == EXISTED for i in items), [i for i in items if i.status == CREATED]


def test_gitkeep_solo_mientras_la_carpeta_este_vacia(tmp_path: Path) -> None:
    """Sin `.gitkeep` la estructura no sobrevive un commit; con datos reales es ruido."""
    ws = tmp_path / "ws"
    init_workspace(ws)
    assert (ws / "input" / "egp" / ".gitkeep").is_file()

    (ws / "input" / "docs" / ".gitkeep").unlink()
    (ws / "input" / "docs" / "negocio.md").write_text("...", encoding="utf-8")
    init_workspace(ws)
    assert not (ws / "input" / "docs" / ".gitkeep").exists()


def test_el_gitkeep_no_se_cuenta_como_documentacion(tmp_path: Path) -> None:
    """`doctor` anunciaba «input/docs/: 1 archivo(s)» por el placeholder."""
    from sas_migrator.service.preflight import check_workspace

    ws = tmp_path / "ws"
    init_workspace(ws)
    build_egp(ws / "input" / "egp" / "demo.egp")
    assert not [c for c in check_workspace(ws) if c.name == "input/docs/"]

    (ws / "input" / "docs" / "negocio.md").write_text("...", encoding="utf-8")
    docs = [c for c in check_workspace(ws) if c.name == "input/docs/"]
    assert docs and docs[0].detail == "1 archivo(s)"


def test_el_config_escrito_carga_sin_errores(tmp_path: Path) -> None:
    """El template pasa por el cargador estricto: un typo en él sería un error de
    clave desconocida en el primer comando del usuario."""
    ws = tmp_path / "ws"
    init_workspace(ws)
    config = load_project_config(ws)
    # Neutro salvo lo que el usuario complete: nada de infraestructura ajena.
    assert config.db.default_server == ""
    assert config.llm.provider == "anthropic"


def test_el_template_nombra_todas_las_claves_del_modelo() -> None:
    """Anti-deriva: `ProjectConfig` crece y el template comentado se queda atrás,
    así que la única referencia de claves que tiene el usuario miente."""
    texto = template_text("project_config.yaml")
    faltan = []
    for nombre, campo in ProjectConfig.model_fields.items():
        assert f"\n{nombre}:" in texto, f"falta la sección {nombre}"
        for clave in campo.annotation.model_fields:
            if f"{clave}:" not in texto:
                faltan.append(f"{nombre}.{clave}")
    assert not faltan, f"claves sin documentar en el template: {', '.join(faltan)}"


def test_los_templates_viajan_en_el_paquete() -> None:
    """Se leen por importlib.resources, no relativo al repo: es lo que hace que
    `init` funcione instalado con pipx, que es el caso que lo motivó."""
    for name in TEMPLATES:
        assert template_text(name).strip(), name
    # El del gitignore va sin punto: con punto serían reglas reales del paquete.
    assert "gitignore" in TEMPLATES and ".gitignore" not in TEMPLATES


def test_egp_invalido_falla_antes_de_crear_nada(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    result = CliRunner().invoke(
        app, ["init", "-w", str(ws), "--egp", str(tmp_path / "no_existe.egp")]
    )
    assert result.exit_code == EXIT_USAGE
    assert "No existe" in result.output
    assert not ws.exists()  # un typo en la ruta no deja un workspace a medias

    otro = tmp_path / "proyecto.zip"
    otro.write_text("x", encoding="utf-8")
    result = CliRunner().invoke(app, ["init", "-w", str(ws), "--egp", str(otro)])
    assert result.exit_code == EXIT_USAGE
    assert ".egp" in result.output
