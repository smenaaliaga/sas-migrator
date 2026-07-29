"""Parseo del código PYTHON GENERADO — el que escribe el traductor.

Existe por una sola razón: silenciar el ruido que ese código produce al
parsearse. Una celda con ``str.contains('D\\.')`` (string normal, no raw) hace
que Python emita ``<unknown>:7: SyntaxWarning: invalid escape sequence '\\.'``
en CADA pasada — y el migrador parsea las mismas celdas varias veces (chequeo
estático, símbolos, ensamblado, auditoría). El resultado era una consola
inundada de advertencias sobre código que el usuario no puede corregir en
medio de la corrida, y que además ahogaban el progreso por nodo.

La advertencia no se pierde para siempre: el notebook conserva el literal, así
que reaparece al ejecutarlo, que es cuando el usuario puede hacer algo.
"""

from __future__ import annotations

import ast
import threading
import warnings

# `warnings.catch_warnings` manipula el filtro GLOBAL: dos threads entrando a
# la vez pueden restaurar snapshots cruzados y dejar el filtro alterado. La
# fase 6 traduce con un pool de threads, así que el parseo va serializado —
# parsear una celda son microsegundos, no es un cuello de botella.
_LOCK = threading.Lock()


def parse_generated(source: str, filename: str = "<celda>") -> ast.AST:
    """``ast.parse`` sobre código generado, sin SyntaxWarning en la consola.

    Propaga ``SyntaxError`` igual que ``ast.parse``: un error de sintaxis SÍ es
    accionable (el chequeo estático lo reporta y el nodo se re-traduce).
    """
    with _LOCK, warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source, filename=filename)
