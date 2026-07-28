"""Tests de la configuración de proyecto y de los fixes del port v1→v2:
sin defaults de cliente, reset con workspace explícito, analyze parametrizable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sas_migrator.core.analysis.analyze as ga
import sas_migrator.core.audit as audit_mod
from sas_migrator.core.config import AuditConfig, ProjectConfig, load_project_config
from sas_migrator.core.db.engine import connection_string, resolve_server
from sas_migrator.core.reset import reset_workspace

# ── project_config ───────────────────────────────────────────────────────────

def test_config_defaults_are_neutral() -> None:
    cfg = ProjectConfig()
    assert cfg.db.default_server == ""
    assert cfg.db.trust_server_certificate is False
    assert cfg.audit.env_secret_markers == ["os.environ", "dotenv"]
    assert cfg.audit.runtime_df_checks == []


def test_config_missing_file_yields_defaults(tmp_path: Path) -> None:
    assert load_project_config(tmp_path).db.default_server == ""


def test_config_loads_from_workspace(tmp_path: Path) -> None:
    (tmp_path / "project_config.yaml").write_text(
        "db:\n  default_server: srv.example.local\n  trust_server_certificate: true\n"
        "audit:\n  runtime_df_checks: [df_resumen]\n",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.db.default_server == "srv.example.local"
    assert cfg.db.trust_server_certificate is True
    assert cfg.audit.runtime_df_checks == ["df_resumen"]


# ── engine / resolución de servidor ──────────────────────────────────────────

def test_resolve_server_prefers_connection_value() -> None:
    cfg = ProjectConfig.model_validate({"db": {"default_server": "fallback.local"}})
    assert resolve_server({"server": "explicit.local"}, cfg) == "explicit.local"


def test_resolve_server_falls_back_to_config() -> None:
    cfg = ProjectConfig.model_validate({"db": {"default_server": "fallback.local"}})
    assert resolve_server({"alias": "X"}, cfg) == "fallback.local"


def test_resolve_server_without_any_source_raises() -> None:
    with pytest.raises(ValueError, match="sin 'server'"):
        resolve_server({"alias": "X"}, ProjectConfig())


def test_connection_string_does_not_trust_certificate_by_default() -> None:
    cs = connection_string({"server": "s.local", "database": "DB1"})
    assert "TrustServerCertificate=no" in cs
    assert "SERVER=tcp:s.local,1433" in cs


def test_connection_string_trusts_certificate_only_on_optin() -> None:
    cfg = ProjectConfig.model_validate({"db": {"trust_server_certificate": True}})
    assert "TrustServerCertificate=yes" in connection_string({"server": "s"}, cfg)


# ── heurísticas de auditoría neutras ─────────────────────────────────────────

def test_audit_default_heuristics_have_no_client_markers() -> None:
    joined = json.dumps(audit_mod.DEFAULT_HEURISTICS)
    for marker in ("si3", "bcentral", "sieterestws", "df_bd_ctsi", "bcch"):
        assert marker not in joined


def test_audit_no_longer_takes_domain_or_table_markers() -> None:
    """Endpoints y tablas se infieren del SAS: declararlos dejó de ser posible."""
    for key in ("domain_markers", "sql_engine_markers", "sql_from_markers"):
        assert key not in audit_mod.DEFAULT_HEURISTICS
        assert key not in AuditConfig.model_fields


def test_audit_heuristics_load_from_project_config(tmp_path: Path) -> None:
    (tmp_path / "project_config.yaml").write_text(
        "audit:\n  runtime_df_checks: [df_pib]\n", encoding="utf-8"
    )
    state = tmp_path / "state"
    state.mkdir()
    heur = audit_mod.load_heuristics(state)
    assert heur["runtime_df_checks"] == ["df_pib"]


def test_audit_heuristics_state_yaml_overrides_project_config(tmp_path: Path) -> None:
    (tmp_path / "project_config.yaml").write_text(
        "audit:\n  runtime_df_checks: [df_pib]\n", encoding="utf-8"
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "audit_heuristics.yaml").write_text(
        "runtime_df_checks: [df_cnt]\n", encoding="utf-8"
    )
    assert audit_mod.load_heuristics(state)["runtime_df_checks"] == ["df_cnt"]


# ── reset con workspace explícito ────────────────────────────────────────────

def test_reset_cleans_given_workspace(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    (state / "migration_state.json").write_text("{}", encoding="utf-8")
    (state / ".gitkeep").write_text("", encoding="utf-8")
    out = tmp_path / "output"
    out.mkdir()
    (out / "NB-01_x.ipynb").write_text("{}", encoding="utf-8")

    counts = reset_workspace(tmp_path)

    assert counts["state"] == 1 and counts["output"] == 1
    assert (state / ".gitkeep").exists(), ".gitkeep debe sobrevivir al reset"
    assert (state / "nodes").is_dir(), "state/nodes/ debe recrearse"
    assert not (out / "NB-01_x.ipynb").exists()


def test_reset_missing_workspace_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        reset_workspace(tmp_path / "no-existe")


# ── analyze con state_dir explícito ──────────────────────────────────────────

def test_analyze_main_writes_to_given_state_dir(tmp_path: Path) -> None:
    state = tmp_path / "mi_estado"
    (state / "nodes").mkdir(parents=True)
    (state / "flow_graph.json").write_text(
        json.dumps({"nodes": [], "edges": []}), encoding="utf-8"
    )

    ga.main(state)

    assert (state / "code_smells.json").exists()
    assert (state / "analysis_evidence.json").exists()
    assert (state / "lineage.json").exists()
