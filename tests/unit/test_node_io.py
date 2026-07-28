"""node_io — el código SAS se guarda como array de líneas, legible en el JSON.

El contrato: en disco ``code_lines`` (una línea por elemento), en memoria
``code: str`` idéntico byte a byte al extraído. La lectura tolera el formato
viejo (``code`` string) para workspaces existentes.
"""

import json

from sas_migrator.core.utils.node_io import (
    decode_node_code,
    dump_node,
    encode_node_code,
    load_node,
)


def test_roundtrip_byte_a_byte():
    for code in (
        "data work.x;\n  set base.y;\nrun;",
        "",
        "una sola línea sin newline",
        "termina en newline\n",
        "\n\ncomienza con blancos\n\n",
        "con \r embebido\nsegunda",  # splitlines() cortaría el \r; split("\n") no
    ):
        node = {"id": "n1", "code": code}
        assert decode_node_code(encode_node_code(node))["code"] == code


def test_encode_reemplaza_code_por_code_lines():
    enc = encode_node_code({"id": "n1", "code": "a;\nb;", "label": "x"})
    assert "code" not in enc
    assert enc["code_lines"] == ["a;", "b;"]
    assert enc["label"] == "x"


def test_encode_code_vacio_es_lista_vacia():
    assert encode_node_code({"id": "n1", "code": ""})["code_lines"] == []


def test_decode_tolera_formato_viejo():
    assert decode_node_code({"id": "n1", "code": "a;\nb;"})["code"] == "a;\nb;"


def test_decode_sin_codigo_da_string_vacio():
    assert decode_node_code({"id": "n1"})["code"] == ""


def test_dump_y_load_en_disco(tmp_path):
    path = tmp_path / "CodeTask-1.json"
    dump_node(path, {"id": "n1", "code": "data w.a;\n  set b.c;\nrun;"})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["code_lines"] == ["data w.a;", "  set b.c;", "run;"]
    assert "code" not in raw

    loaded = load_node(path)
    assert loaded["code"] == "data w.a;\n  set b.c;\nrun;"
    assert "code_lines" not in loaded


def test_load_node_inexistente():
    from pathlib import Path

    assert load_node(Path("no-existe.json")) is None
