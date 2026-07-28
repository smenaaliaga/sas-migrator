"""Análisis de nombres a nivel de notebook: qué define y qué usa cada celda.

Un notebook ensamblado es un único espacio de nombres que se ejecuta de arriba
abajo. El traductor, en cambio, ve un nodo por vez, así que produce con
naturalidad código que "asume que el DataFrame viene de un nodo anterior" —
cuando ese nodo lo escribió a una tabla SQL, o directamente no existe. Eso no
lo detecta ``ast.parse``: es sintaxis válida que revienta con ``NameError`` en
la primera celda que se corre, o peor, arrastra un nombre parecido.

Este módulo recorre las celdas EN EL ORDEN DE EJECUCIÓN acumulando los nombres
ligados, y reporta las cargas que no puede resolver. Es deliberadamente
conservador: ante la duda, un nombre se da por definido. Un falso positivo
manda a needs_human un nodo que estaba bien, que es el error caro.
"""

from __future__ import annotations

import ast
import builtins

BUILTIN_NAMES = frozenset(dir(builtins)) | {"__file__", "__name__"}


# ── Nombres ligados por una sentencia ───────────────────────────────────────

def _target_names(node: ast.AST) -> set[str]:
    """Nombres ligados por un target de asignación (incluye desempaquetado)."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
    return names


def _alias_names(aliases: list[ast.alias]) -> set[str]:
    # "import a.b.c" liga "a"; "import a.b as x" liga "x".
    return {
        (alias.asname or alias.name.split(".")[0])
        for alias in aliases
    }


def bound_names(stmt: ast.stmt) -> set[str]:
    """Nombres que ``stmt`` liga en el ámbito donde aparece.

    No desciende a cuerpos de función/clase: lo que se liga ahí es local. Sí
    desciende a ``if``/``for``/``with``/``try``, porque en Python esos bloques
    no crean ámbito y sus asignaciones sí quedan visibles después.
    """
    names: set[str] = set()

    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _alias_names(stmt.names)

    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            names |= _target_names(target)
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        names |= _target_names(stmt.target)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        names |= _target_names(stmt.target)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                names |= _target_names(item.optional_vars)
    elif isinstance(stmt, ast.Global | ast.Nonlocal):
        names |= set(stmt.names)

    # Bloques compuestos: sus cuerpos ligan en el mismo ámbito.
    for field in ("body", "orelse", "finalbody"):
        for sub in getattr(stmt, field, []) or []:
            if isinstance(sub, ast.stmt):
                names |= bound_names(sub)
    for handler in getattr(stmt, "handlers", []) or []:
        if handler.name:
            names.add(handler.name)
        for sub in handler.body:
            names |= bound_names(sub)

    # Walrus y comprehensions anidadas en las expresiones de la sentencia.
    for sub in ast.walk(stmt):
        if isinstance(sub, ast.NamedExpr):
            names |= _target_names(sub.target)
    return names


# ── Nombres usados (cargas no resueltas localmente) ─────────────────────────

class _LoadCollector(ast.NodeVisitor):
    """Junta los ``Name`` en contexto Load que el ámbito local no liga.

    Las funciones, lambdas y comprehensions crean ámbito: sus parámetros y
    variables de iteración se ligan localmente y no se reportan. Los cuerpos de
    función se difieren (``deferred``) porque pueden referirse legítimamente a
    nombres que se definen más abajo en el notebook.
    """

    def __init__(self) -> None:
        self.free: set[str] = set()
        self.deferred: set[str] = set()
        self._local: list[set[str]] = []

    def _bound_here(self, name: str) -> bool:
        return any(name in scope for scope in self._local)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not self._bound_here(node.id):
            self.free.add(node.id)

    def _visit_function(self, node) -> None:
        # Defaults y decoradores se evalúan en el ámbito de afuera.
        for default in [*node.args.defaults, *(node.args.kw_defaults or [])]:
            if default is not None:
                self.visit(default)
        for deco in getattr(node, "decorator_list", []):
            self.visit(deco)

        args = node.args
        params = {
            a.arg
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        }
        if args.vararg:
            params.add(args.vararg.arg)
        if args.kwarg:
            params.add(args.kwarg.arg)

        name = getattr(node, "name", None)  # lambda no tiene nombre
        if name:
            params.add(name)

        inner = _LoadCollector()
        inner._local = [*self._local, params]
        body = node.body if isinstance(node.body, list) else [node.body]
        for stmt in body:
            inner.visit(stmt)
            if isinstance(stmt, ast.stmt):
                inner._local[-1] |= bound_names(stmt)
        # El cuerpo corre cuando se llama, no acá: difiere el chequeo.
        self.deferred |= {n for n in inner.free | inner.deferred if not self._bound_here(n)}

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def _visit_comprehension(self, node) -> None:
        targets: set[str] = set()
        for gen in node.generators:
            targets |= _target_names(gen.target)
        self._local.append(targets)
        for gen in node.generators:
            # El primer iterable se evalúa afuera, pero como los targets ya
            # están en scope el resultado es el mismo salvo sombreado — y ante
            # el sombreado preferimos callar (conservador).
            self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for field in ("elt", "key", "value"):
            sub = getattr(node, field, None)
            if isinstance(sub, ast.AST):
                self.visit(sub)
        self._local.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension


# ── Recorrido de un notebook ────────────────────────────────────────────────

class _BlockWalker:
    """Recorre sentencias en orden de ejecución acumulando nombres visibles.

    El recorrido es recursivo y no plano porque un bloque compuesto liga nombres
    para su propio cuerpo: en ``with engine.begin() as conn:`` el ``conn`` que
    usan las líneas de adentro lo define el header, y una pasada plana lo
    reportaría como indefinido.

    Los cuerpos de ``if``/``for``/``with``/``try`` sí exportan sus asignaciones
    hacia afuera (Python no crea ámbito ahí), y se dan por ejecutados aunque la
    rama pueda no correr: asumir lo contrario inventaría indefinidos.
    """

    def __init__(self, visible: set[str]) -> None:
        self.visible = visible
        self.defined: set[str] = set()
        self.missing: list[str] = []
        self.deferred: set[str] = set()

    def _bind(self, names: set[str]) -> None:
        self.visible |= names
        self.defined |= names

    def _loads(self, node: ast.AST | None) -> None:
        if node is None:
            return
        collector = _LoadCollector()
        collector.visit(node)
        self.deferred |= collector.deferred
        for name in sorted(collector.free - self.visible):
            if name not in self.missing:
                self.missing.append(name)

    def block(self, body) -> None:
        for stmt in body or []:
            self.stmt(stmt)

    def stmt(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # El cuerpo corre cuando se llama/instancia: sus cargas se difieren
            # y se juzgan al final, contra todo lo que el nodo definió.
            collector = _LoadCollector()
            collector.visit(stmt)
            self.deferred |= collector.free | collector.deferred
            self._bind({stmt.name})
            return

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._loads(stmt.iter)
            self._bind(_target_names(stmt.target))
            self.block(stmt.body)
            self.block(stmt.orelse)
            return

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self._loads(item.context_expr)
                if item.optional_vars is not None:
                    self._bind(_target_names(item.optional_vars))
            self.block(stmt.body)
            return

        if isinstance(stmt, (ast.If, ast.While)):
            self._loads(stmt.test)
            self.block(stmt.body)
            self.block(stmt.orelse)
            return

        if isinstance(stmt, ast.Try):
            self.block(stmt.body)
            for handler in stmt.handlers:
                self._loads(handler.type)
                if handler.name:
                    self._bind({handler.name})
                self.block(handler.body)
            self.block(stmt.orelse)
            self.block(stmt.finalbody)
            return

        # Sentencia simple: primero se evalúa la expresión, después liga.
        self._loads(stmt)
        self._bind(bound_names(stmt))


def undefined_in_cells(cells: list[str], known: set[str]) -> tuple[list[str], set[str]]:
    """Nombres sin definir en ``cells``, y los nombres que las celdas definen.

    ``known`` se consume sin mutarse. Las celdas se procesan en orden: usar un
    nombre antes de asignarlo es tan ``NameError`` como no asignarlo nunca.
    """
    walker = _BlockWalker(set(known) | BUILTIN_NAMES)
    for cell in cells:
        try:
            tree = ast.parse(cell)
        except SyntaxError:
            continue  # ya lo reporta el chequeo de sintaxis, con mejor mensaje
        walker.block(tree.body)

    for name in sorted(walker.deferred - walker.visible):
        if name not in walker.missing:
            walker.missing.append(name)
    return walker.missing, walker.defined
