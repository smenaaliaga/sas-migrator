"""Cobertura de los datasets de salida del nodo SAS en la traducción.

El chequeo estático detecta código roto y el verificador LLM detecta semántica
cambiada. Entre los dos quedaba un hueco: el modelo que traduce los primeros N
pasos con fidelidad y después emite un cierre plausible. El JSON valida, el
Python parsea, ningún chequeo protesta — y el nodo entra al notebook cubriendo
el 3% de lo que hacía. Un nodo de 144.647 caracteres de SAS devolvió 2.826
tokens y quedó registrado como ``ok``.

Este módulo mide eso con el dato que el parser ya extrajo: ``output_datasets``
del plan (``core.planning``), o sea las tablas que el nodo SAS crea. Si el
Python no nombra ninguna, no las está creando.

Es un proxy de PRESENCIA DE NOMBRE, no de equivalencia semántica: no dice si el
filtro es correcto, solo si el paso existe. Sirve para detectar el colapso —lo
único que mide bien— y por eso el gate está calibrado para disparar únicamente
ante el colapso, no ante una diferencia de nombres. Lo que falta a cualquier
nivel de cobertura se reporta igual, para que un 65/66 no pase en silencio.

Calibración sobre la corrida Síntesis_M_CR18 (44 nodos con >= 5 datasets):
10 nodos <= 54,5% (los colapsados) y 34 nodos >= 80% (los completos), sin nada
en el medio. El umbral vive en ese hueco.
"""

from __future__ import annotations

import re

# Piso de datasets para que el gate opine. Un nodo que declara 1 sola salida
# da 0% o 100% según cómo el modelo haya nombrado UNA variable: es ruido, no
# señal. Casos reales bajo el piso: WORK.SALDOS_SI (S8_wtw - Copy) y
# WORK.SORTTEMPTABLESORTED (V2_2_NF, una temporal de PROC SORT que en pandas no
# tiene por qué existir).
MIN_DATASETS_FOR_GATE = 5

# Bajo esto se rechaza. El nodo COMPLETO más flojo del corpus está en 80%.
MIN_COVERAGE = 0.70

# Cuántos faltantes se nombran en el mensaje. El detalle viaja al modelo como
# instrucción del reintento y a needs_human como diagnóstico: 15 alcanzan para
# saber qué tramo se perdió sin volver el mensaje ilegible.
_MAX_LISTED = 15


def _base_name(dataset: str) -> str:
    """``WORK.AF29_31_6`` → ``af29_31_6`` (el libref no sobrevive a la traducción)."""
    return str(dataset).strip().split(".")[-1].lower()


def output_coverage(
    output_datasets: list[str], cells: list[str]
) -> tuple[float, list[str]]:
    """Fracción de datasets de salida presentes en el Python, y los ausentes.

    El match es por identificador (``\\b``) sobre el fuente completo —código y
    strings—: una tabla nombrada dentro de un ``text("SELECT ... FROM x")``
    cuenta, porque el nodo efectivamente la produce. Buscar por substring
    inflaba la cobertura (``fin`` matcheaba dentro de ``fin_temp``).

    Sin ``output_datasets`` devuelve ``(1.0, [])``: no hay nada que exigir, y
    fingir 0% mandaría a needs_human a los nodos que el parser no supo anotar.
    """
    bases = {_base_name(d) for d in output_datasets if str(d).strip()}
    if not bases:
        return 1.0, []

    source = "\n".join(cells).lower()
    missing = sorted(
        b for b in bases if not re.search(rf"\b{re.escape(b)}\b", source)
    )
    return (len(bases) - len(missing)) / len(bases), missing


def coverage_shortfall(
    output_datasets: list[str], cells: list[str]
) -> str | None:
    """Motivo de rechazo si el nodo quedó por debajo del gate, o ``None``.

    El texto va derecho al reintento, así que nombra lo que falta: el modelo
    necesita saber QUÉ tramo se perdió, no que "cubrió poco".
    """
    total = len({_base_name(d) for d in output_datasets if str(d).strip()})
    if total < MIN_DATASETS_FOR_GATE:
        return None

    coverage, missing = output_coverage(output_datasets, cells)
    if coverage >= MIN_COVERAGE:
        return None

    listed = ", ".join(missing[:_MAX_LISTED])
    extra = f" (y {len(missing) - _MAX_LISTED} más)" if len(missing) > _MAX_LISTED else ""
    return (
        f"traducción incompleta: el nodo SAS crea {total} tablas y el Python solo "
        f"nombra {total - len(missing)} ({coverage:.0%}). Faltan: {listed}{extra}. "
        "Traducí el nodo COMPLETO, de punta a punta: cada PROC SQL / DATA step "
        "del fuente tiene que estar. Si algo no se puede resolver con la "
        "información disponible, esa celda va con raise NotImplementedError "
        "y el motivo en warnings — nunca se omite en silencio"
    )


def coverage_warning(
    output_datasets: list[str], cells: list[str]
) -> str | None:
    """Aviso de faltantes para ``warnings``, aunque el nodo pase el gate.

    El gate ataja el colapso; esto ataja lo demás. Un nodo con 65 de 66 tablas
    pasa —y debe pasar—, pero el revisor tiene que ver cuál es la que falta.
    """
    _, missing = output_coverage(output_datasets, cells)
    if not missing:
        return None
    listed = ", ".join(missing[:_MAX_LISTED])
    extra = f" (y {len(missing) - _MAX_LISTED} más)" if len(missing) > _MAX_LISTED else ""
    return (
        f"[COBERTURA] {len(missing)} tabla(s) de salida del SAS no aparecen en la "
        f"traducción: {listed}{extra}. Puede ser una fusión legítima de pasos "
        "intermedios o un paso perdido — verificar contra el fuente"
    )
