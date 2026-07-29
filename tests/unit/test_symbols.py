"""Nombres sin definir a nivel de notebook.

El módulo es deliberadamente conservador: un falso positivo manda a needs_human
un nodo que estaba bien. Los dos primeros bloques cubren los falsos positivos
que en producción descartaron 4 traducciones válidas de los notebooks; el
último es la regresión que impide que el fix vuelva ciego al chequeo.
"""

from __future__ import annotations

from sas_migrator.core.validation.symbols import undefined_in_cells


def _missing(*cells: str, known: set[str] | None = None) -> list[str]:
    return undefined_in_cells(list(cells), known or {"pd", "np"})[0]


# ── Ámbito de función: ligar antes de visitar ───────────────────────────────

def test_for_con_desempaquetado_dentro_de_una_funcion():
    """`for i, (name, group) in ...` liga los tres para todo el cuerpo.

    Caso real: Templates_gráficos quedó fuera del notebook por esto.
    """
    assert _missing(
        "def graficar(df, ax, periodo_col, y_col):\n"
        "    for i, (name, group) in enumerate(df.groupby('k')):\n"
        "        sorted_group = group.sort_values(periodo_col)\n"
        "        ax.plot(sorted_group[periodo_col], sorted_group[y_col], label=str(name))\n"
    ) == []


def test_asignacion_en_una_rama_usada_en_la_misma_rama():
    """Caso real de S1_01_RevDCV: `bf_group` se asigna y se usa dentro del else."""
    assert _missing(
        "def procesar(df, has_tenedor):\n"
        "    if len(df) == 0:\n"
        "        out = pd.DataFrame()\n"
        "    else:\n"
        "        bf_group = ['anio', 'trim']\n"
        "        if has_tenedor:\n"
        "            bf_group.insert(3, 'tenedor')\n"
        "        out = df.groupby(bf_group, as_index=False)['dato'].sum()\n"
        "    return out\n"
    ) == []


def test_with_y_try_dentro_de_una_funcion():
    assert _missing(
        "def cargar(engine):\n"
        "    try:\n"
        "        with engine.begin() as conn:\n"
        "            filas = conn.execute('select 1')\n"
        "        return filas\n"
        "    except ValueError as exc:\n"
        "        detalle = str(exc)\n"
        "        raise RuntimeError(detalle)\n",
        known={"pd", "np", "engine"},
    ) == []


# ── Guardas de existencia ───────────────────────────────────────────────────

def test_guarda_in_locals_en_un_if():
    """Caso real de S2_01_Inicio, el nodo que inicializa BD_CTSI."""
    assert _missing(
        "bd_ctsi = pd.DataFrame()\n"
        "if 'rp_hh' in locals():\n"
        "    bd_ctsi = pd.concat([bd_ctsi, rp_hh], ignore_index=True)\n"
    ) == []


def test_guarda_in_locals_en_un_ternario():
    """Caso real de S2_06_Bonos (la mejor de las dos copias del nodo)."""
    assert _missing(
        "af31_51_70 = af31_51_70 if 'af31_51_70' in locals() else pd.DataFrame()\n"
    ) == []


def test_guarda_negada_not_in_locals():
    """La forma `not in` protege la carga en la otra rama; cuenta igual."""
    assert _missing(
        "x = pd.DataFrame() if 'x' not in locals() else x\n"
    ) == []


def test_guarda_con_globals_y_vars():
    assert _missing("if 'a' in globals():\n    b = a\n") == []
    assert _missing("if 'a' in vars():\n    b = a\n") == []


def test_la_guarda_no_exporta_el_nombre_al_nodo_siguiente():
    """Consumir `x` bajo guarda no es definirlo: el nodo no lo deja para nadie."""
    _, definidos = undefined_in_cells(
        ["if 'rp_hh' in locals():\n    y = rp_hh\n"], {"pd"}
    )
    assert "rp_hh" not in definidos


# ── Regresión: el chequeo tiene que seguir detectando lo real ───────────────

def test_sigue_detectando_un_indefinido_de_verdad():
    assert _missing("total = ventas['dato'].sum()\n") == ["ventas"]


def test_sigue_detectando_uso_antes_de_asignar_a_nivel_de_modulo():
    assert _missing("y = x + 1\n", "x = 2\n") == ["x"]


def test_una_guarda_de_otro_nombre_no_habilita_este():
    assert _missing(
        "if 'rp_hh' in locals():\n    z = sifmi\n"
    ) == ["sifmi"]


def test_un_in_que_no_es_guarda_no_habilita_nada():
    """`'x' in mi_lista` no dice nada sobre la existencia de `x`."""
    assert _missing("if 'ventas' in columnas:\n    z = ventas\n",
                    known={"pd", "columnas"}) == ["ventas"]


def test_indefinido_dentro_de_una_funcion_se_sigue_reportando():
    """El cuerpo se difiere, pero al final se juzga contra todo lo definido."""
    assert _missing(
        "def f():\n    return tabla_inexistente['x']\n"
    ) == ["tabla_inexistente"]
