"""Tokenizador SAS — capa 1 del parser v2.

Reemplaza el parsing por regex del v1, cuyos modos de falla conocidos eran:
- ``re.sub(r'\\*[^;]*;')`` destruía multiplicaciones (``monto = a * b;``);
- los comentarios de bloque y strings no se respetaban al buscar keywords;
- el split por statements no existía (regex sobre el código completo).

Semántica implementada (la de SAS real):
- ``/* ... */`` es comentario en cualquier posición (no anidado), salvo dentro
  de un string.
- ``* ... ;`` y ``%* ... ;`` son comentario SOLO cuando ``*`` es el primer
  token de un statement — por eso ``a * b`` sobrevive.
- Strings con ``'`` o ``"`` respetan comillas duplicadas (``'it''s'``).
- Los statements se separan por ``;`` fuera de strings y comentarios.
"""

from __future__ import annotations


def strip_block_comments(code: str) -> str:
    """Elimina /* ... */ respetando strings. No anidado (como SAS)."""
    out: list[str] = []
    i, n = 0, len(code)
    in_string: str | None = None
    while i < n:
        ch = code[i]
        if in_string:
            out.append(ch)
            if ch == in_string:
                # comilla duplicada = escape
                if i + 1 < n and code[i + 1] == in_string:
                    out.append(code[i + 1])
                    i += 2
                    continue
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and code[i + 1] == "*":
            end = code.find("*/", i + 2)
            if end == -1:
                break  # comentario sin cerrar: descartar el resto
            # preservar saltos de línea para no pegar tokens
            out.append(" ")
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def mask_noncode(code: str) -> str:
    """El código con comentarios y strings BLANQUEADOS, misma longitud.

    Para buscar fronteras (``^PROC|^DATA``) con offsets que sigan valiendo
    sobre el original: un ``/* proc sort viejo */``, un ``* proc means...;``
    de statement o un ``'PROC'`` dentro de un string no son bloques y no deben
    producir un corte. Los saltos de línea se conservan (los regex ^ los usan).
    """
    out = list(code)
    i, n = 0, len(code)
    in_string: str | None = None
    in_stmt_comment = False
    stmt_start = True  # ¿el próximo token no-blanco abre un statement?
    while i < n:
        ch = code[i]
        if in_string:
            if ch != "\n":
                out[i] = " "
            if ch == in_string:
                if i + 1 < n and code[i + 1] == in_string:  # comilla duplicada
                    out[i + 1] = " "
                    i += 2
                    continue
                in_string = None
            i += 1
            continue
        if in_stmt_comment:
            if ch == ";":
                in_stmt_comment = False
                stmt_start = True
            elif ch != "\n":
                out[i] = " "
            i += 1
            continue
        if ch == "/" and i + 1 < n and code[i + 1] == "*":
            end = code.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            for j in range(i, stop):
                if code[j] != "\n":
                    out[j] = " "
            i = stop
            continue
        if ch in ("'", '"'):
            in_string = ch
            out[i] = " "
            i += 1
            continue
        if ch == ";":
            stmt_start = True
            i += 1
            continue
        if stmt_start and ch == "%" and i + 1 < n and code[i + 1] == "*":
            in_stmt_comment = True
            out[i] = out[i + 1] = " "
            i += 2
            continue
        if stmt_start and ch == "*":
            in_stmt_comment = True
            out[i] = " "
            i += 1
            continue
        if not ch.isspace():
            stmt_start = False
        i += 1
    return "".join(out)


def split_statements(code: str) -> list[str]:
    """Divide en statements por ';' fuera de strings. Aplica strip a cada uno."""
    code = strip_block_comments(code)
    statements: list[str] = []
    buf: list[str] = []
    in_string: str | None = None
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if in_string:
            buf.append(ch)
            if ch == in_string:
                if i + 1 < n and code[i + 1] == in_string:
                    buf.append(code[i + 1])
                    i += 2
                    continue
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def is_comment_statement(stmt: str) -> bool:
    """``* ...`` o ``%* ...`` como primer token = statement de comentario."""
    s = stmt.lstrip()
    return s.startswith("*") or s.startswith("%*")


def code_statements(code: str) -> list[str]:
    """Statements ejecutables: sin comentarios de bloque ni de statement."""
    return [s for s in split_statements(code) if not is_comment_statement(s)]


def words(stmt: str) -> list[str]:
    """Tokens 'palabra' de un statement (identificadores, keywords, refs a.b,
    macro refs &x). Los strings se reemplazan por un marcador para no aportar
    falsos identificadores."""
    out: list[str] = []
    buf: list[str] = []
    in_string: str | None = None
    for ch in stmt:
        if in_string:
            if ch == in_string:
                in_string = None
                out.append("<str>")
            continue
        if ch in ("'", '"'):
            in_string = ch
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        if ch.isalnum() or ch in ("_", ".", "&", "%"):
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out
