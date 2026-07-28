"""prompt_builder: el traductor recibe TODO el contexto que el proyecto sabe.

La corrida real mostró al modelo quejándose en sus propios warnings de no
conocer conexiones, mejoras ni datasets. Estos tests fijan el contrato: qué
viaja en el system (estable, cacheable) y qué en el user (por nodo).
"""

from __future__ import annotations

import json

from sas_migrator.llm import prompt_builder


def _target() -> dict:
    return {
        "node_id": "CodeTask-1",
        "node_label": "Carga",
        "strategy": "pandas",
        "placement": "pandas",
        "dependencies": ["CodeTask-0"],
        "approved_improvements": ["M-001"],
        "input_datasets": ["GG.CLIENTES", "WORK.BASE"],
        "output_datasets": ["WORK.RESULT"],
        "output_tables": ["GG.SALIDA"],
        "macro_params": ["ANIO"],
    }


# ── bloque de contexto del proyecto ─────────────────────────────────────────

def test_project_context_none_si_no_hay_nada() -> None:
    assert prompt_builder.build_project_context(
        connections=[], improvements=[], macro_param_values={}
    ) is None


def test_project_context_es_determinista_y_completo() -> None:
    kwargs = dict(
        connections=[{"alias": "GG", "database": "Gestion", "role": "source",
                      "tables": ["CLIENTES", "SALIDA"]}],
        improvements=[{"id": "M-001", "title": "Idempotencia",
                       "description": "borrar el período antes de insertar",
                       "affected_nodes": ["CodeTask-1"]}],
        macro_param_values={"ANIO": 2026, "TRIM": 1},
    )
    block = prompt_builder.build_project_context(**kwargs)
    assert block == prompt_builder.build_project_context(**kwargs), "cacheable"
    assert "`GG` → database `Gestion`" in block
    assert "M-001" in block and "CodeTask-1" in block
    assert "&ANIO = 2026" in block and "&TRIM = 1" in block


def test_translation_system_agrega_el_bloque_de_proyecto() -> None:
    base = prompt_builder.build_translation_system()
    con_contexto = prompt_builder.build_translation_system("# Contexto del proyecto")
    assert len(base) == len(con_contexto) - 1
    assert con_contexto[-1] == "# Contexto del proyecto"
    # el bloque 1 (contrato + patrones) es idéntico entre proyectos
    assert base[0] == con_contexto[0]


def test_translation_system_son_tres_bloques_con_fewshot() -> None:
    blocks = prompt_builder.build_translation_system("# ctx")
    assert len(blocks) == 3
    contrato, fewshot, ctx = blocks
    assert "patrones" in contrato.lower() or "NodeTranslation" in contrato
    assert "Ejemplos de traducción" in fewshot
    # un ejemplo por strategy principal, con el anti-patrón clave a la vista
    assert "data-step-merge" in fewshot          # pandas
    assert "DELETE FROM" in fewshot              # pushdown idempotente, no DROP
    assert "macro-utility" in fewshot            # utility sin I/O
    assert fewshot == prompt_builder.build_translation_system()[1], "determinista"


# ── logging por celda (decisión B5b) ────────────────────────────────────────

def test_fewshot_off_no_contiene_log_y_conserva_la_forma_original() -> None:
    """Invariante del strip: la variante OFF es la de siempre — sin `_log`,
    sin el binding `res = ` del pushdown. Protege evals grabados y cache."""
    off = prompt_builder._read_fewshot(False)
    assert "_log" not in off
    assert "res = conn.exec_driver_sql" not in off
    assert "conn.exec_driver_sql" in off  # la llamada sigue, sin binding
    assert prompt_builder._read_fewshot() == off, "default = OFF"


def test_fewshot_on_modela_log_de_shape_y_rowcount() -> None:
    on = prompt_builder._read_fewshot(True)
    assert '_log(\\"cruce\\", cruce)' in on            # pandas: shape del DF
    assert "res.rowcount" in on                        # pushdown: filas del execute
    assert on.count("SELECT COUNT") == 0, "jamás query extra solo para loguear"


def test_project_context_con_cell_logging_agrega_la_seccion() -> None:
    base = dict(connections=[], improvements=[], macro_param_values={})
    off = prompt_builder.build_project_context(**base, cell_logging=False)
    on = prompt_builder.build_project_context(**base, cell_logging=True)
    assert off is None
    assert "_log(label, value=None)" in on
    assert "NO lo definas" in on
    assert "res.rowcount" in on


def test_translation_system_propaga_cell_logging_al_fewshot() -> None:
    off = prompt_builder.build_translation_system()
    on = prompt_builder.build_translation_system(cell_logging=True)
    assert "_log" not in off[1]
    assert "_log" in on[1]
    assert off[0] == on[0], "el bloque contrato no cambia con el feature"


# ── mensaje user por nodo ───────────────────────────────────────────────────

def test_user_header_declara_datasets_y_macro_params() -> None:
    user = prompt_builder.build_translation_user(_target(), "DATA x; RUN;", ["GG"])
    head = json.loads(user.splitlines()[0])
    assert head["input_datasets"] == ["GG.CLIENTES", "WORK.BASE"]
    assert head["output_datasets"] == ["WORK.RESULT"]
    assert head["output_tables"] == ["GG.SALIDA"]
    assert head["macro_params"] == ["ANIO"]
    assert head["db_aliases"] == ["GG"]


def test_user_incluye_review_note_y_contexto_de_dependencias() -> None:
    user = prompt_builder.build_translation_user(
        _target(), "DATA x; RUN;", [],
        review_note="ojo: depende del período cargado",
        dependencies_context={"CodeTask-0": {"deja_datasets": ["WORK.BASE"]}},
    )
    assert "ojo: depende del período cargado" in user
    assert "Qué deja cada dependencia" in user
    assert "WORK.BASE" in user
    # el header sigue siendo la primera línea (contrato de los fakes)
    json.loads(user.splitlines()[0])


def test_user_sin_extras_no_agrega_secciones_vacias() -> None:
    user = prompt_builder.build_translation_user(_target(), "DATA x; RUN;", [])
    assert "Qué deja cada dependencia" not in user
    assert "Nota del analista" not in user


# ── json_excerpt: recorte que sigue siendo JSON ─────────────────────────────

def test_json_excerpt_bajo_el_limite_no_toca_nada() -> None:
    data = {"a": [1, 2, 3], "b": "x"}
    assert json.loads(prompt_builder.json_excerpt(data, 1000)) == data


def test_json_excerpt_recorta_items_y_lo_anuncia() -> None:
    data = {"meta": "v1", "items": [{"id": i, "texto": "x" * 50} for i in range(200)]}
    out = prompt_builder.json_excerpt(data, 2000)
    assert len(out) <= 2000
    parsed = json.loads(out)  # ← el slicing crudo fallaba exactamente acá
    assert parsed["meta"] == "v1", "lo que no es lista sobrevive intacto"
    assert "items omitidos" in parsed["items"][-1], "el recorte se anuncia"
    kept = len(parsed["items"]) - 1
    omitted = int(parsed["items"][-1].split("(")[1].split(" ")[0])
    assert kept + omitted == 200, "la cuenta cierra"


def test_json_excerpt_sin_listas_declara_el_corte() -> None:
    data = {"texto": "y" * 5000}
    out = prompt_builder.json_excerpt(data, 1000)
    parsed = json.loads(out)
    assert "_recorte" in parsed and "NO es el documento completo" in parsed["_recorte"]


# ── helpers de fases: contexto de dependencias y review notes ───────────────

def test_deps_context_cap_declarado() -> None:
    from sas_migrator.llm.phases import MAX_DEPS_IN_CONTEXT, _deps_context

    n = MAX_DEPS_IN_CONTEXT + 5
    targets = {
        f"N{i}": {"node_label": f"n{i}", "output_datasets": [f"WORK.T{i}"],
                  "output_tables": []}
        for i in range(n)
    }
    target = {"node_id": "X", "dependencies": [f"N{i}" for i in range(n)]}
    ctx = _deps_context(target, targets)
    assert len([k for k in ctx if not k.startswith("_")]) == MAX_DEPS_IN_CONTEXT
    assert "5 dependencias más" in ctx["_omitidas"], "el recorte se anuncia"


def test_review_notes_mapea_por_nodo_y_tolera_basura(tmp_path) -> None:
    from sas_migrator.llm.phases import _review_notes

    reviews = tmp_path / "analysis_reviews"
    reviews.mkdir()
    (reviews / "PFD-1.json").write_text(
        json.dumps({"reviews": [{"node_id": "A", "note": "cuidado con &ANIO"},
                                 {"node_id": "B", "note": ""}]}),
        encoding="utf-8",
    )
    (reviews / "roto.json").write_text("{truncado", encoding="utf-8")
    notes = _review_notes(tmp_path)
    assert notes == {"A": "cuidado con &ANIO"}
