"""Cobertura de datasets de salida: el gate contra la traducción que abrevia.

Los números de los tests salen de la calibración sobre la corrida real
(Síntesis_M_CR18): los nodos colapsados quedaron <= 54,5% y los completos
>= 80%, sin nada en el medio.
"""

from __future__ import annotations

from sas_migrator.core.validation.coverage import (
    coverage_shortfall,
    coverage_warning,
    output_coverage,
)


def _cells(*nombres: str) -> list[str]:
    return [f"{n} = pd.DataFrame()\n" for n in nombres]


# ── La métrica ──────────────────────────────────────────────────────────────

def test_el_libref_no_cuenta_para_el_match():
    """`WORK.VENTAS` en el SAS es `ventas` en el Python."""
    cobertura, faltan = output_coverage(["WORK.VENTAS"], ["ventas = pd.DataFrame()"])
    assert (cobertura, faltan) == (1.0, [])


def test_una_tabla_nombrada_dentro_de_un_sql_cuenta():
    """El nodo la produce igual, aunque el nombre viva en un string."""
    cobertura, _ = output_coverage(
        ["TABLAS.BD_CTSI"], ['df = pd.read_sql(text("SELECT * FROM TABLAS.dbo.BD_CTSI"), e)']
    )
    assert cobertura == 1.0


def test_el_match_es_por_identificador_no_por_substring():
    """`fin` no está presente solo porque exista `fin_temp`."""
    _, faltan = output_coverage(["WORK.FIN"], ["fin_temp = pd.DataFrame()"])
    assert faltan == ["fin"]


def test_sin_output_datasets_no_se_exige_nada():
    """5 de los 75 targets del proyecto no traen el campo; fingir 0% los mataría."""
    assert output_coverage([], ["x = 1"]) == (1.0, [])


# ── El gate ─────────────────────────────────────────────────────────────────

def test_rechaza_el_colapso():
    outs = [f"WORK.T{i}" for i in range(20)]
    motivo = coverage_shortfall(outs, _cells("t0", "t1"))
    assert motivo is not None
    assert "20 tablas" in motivo and "2" in motivo
    # El detalle viaja al reintento: tiene que nombrar lo que falta.
    assert "t5" in motivo


def test_no_rechaza_una_traduccion_completa():
    outs = [f"WORK.T{i}" for i in range(20)]
    assert coverage_shortfall(outs, _cells(*[f"t{i}" for i in range(20)])) is None


def test_no_rechaza_por_pocas_tablas_intermedias_faltantes():
    """65 de 66 pasa: el gate ataja el colapso, no la diferencia de nombres."""
    outs = [f"WORK.T{i}" for i in range(66)]
    assert coverage_shortfall(outs, _cells(*[f"t{i}" for i in range(65)])) is None


def test_el_piso_de_datasets_evita_el_falso_positivo_del_nodo_chico():
    """Casos reales: WORK.SALDOS_SI y WORK.SORTTEMPTABLESORTED daban 0/1."""
    assert coverage_shortfall(["WORK.SORTTEMPTABLESORTED"], ["df = x.sort_values('a')"]) is None
    assert coverage_shortfall([f"WORK.T{i}" for i in range(4)], ["x = 1"]) is None
    # Con 5 ya opina.
    assert coverage_shortfall([f"WORK.T{i}" for i in range(5)], ["x = 1"]) is not None


def test_el_umbral_esta_en_el_hueco_medido():
    outs = [f"WORK.T{i}" for i in range(100)]
    al_60 = _cells(*[f"t{i}" for i in range(60)])
    al_80 = _cells(*[f"t{i}" for i in range(80)])
    assert coverage_shortfall(outs, al_60) is not None  # colapsado
    assert coverage_shortfall(outs, al_80) is None      # completo


# ── El aviso ────────────────────────────────────────────────────────────────

def test_avisa_de_los_faltantes_aunque_pase_el_gate():
    outs = [f"WORK.T{i}" for i in range(66)]
    aviso = coverage_warning(outs, _cells(*[f"t{i}" for i in range(65)]))
    assert aviso is not None and "t65" in aviso


def test_sin_faltantes_no_hay_aviso():
    outs = [f"WORK.T{i}" for i in range(10)]
    assert coverage_warning(outs, _cells(*[f"t{i}" for i in range(10)])) is None


def test_el_aviso_no_depende_del_piso_del_gate():
    """Un nodo de 1 tabla no se rechaza, pero si falta el revisor tiene que verlo."""
    assert coverage_shortfall(["WORK.SALDOS_SI"], ["x = 1"]) is None
    assert coverage_warning(["WORK.SALDOS_SI"], ["x = 1"]) is not None
