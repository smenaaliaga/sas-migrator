"""Construcción de prompts desde los recursos del paquete.

El system de traducción es UN prefijo estable (convenciones + AMBAS tablas de
patrones): una sola entrada de prompt-cache para todos los nodos, sin importar
la strategy — el mensaje user declara qué tabla aplica. Todo lo volátil viaja
en el user.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files


def _read(name: str) -> str:
    return (files("sas_migrator.llm") / "prompts" / name).read_text(encoding="utf-8")


def _read_fewshot() -> str:
    """Ejemplos curados SAS→NodeTranslation (llm/prompts/fewshot/, en orden).

    Cumplen doble función: enseñan el formato con casos reales del eval set, y
    engordan el prefijo cacheable — el system sin ejemplos quedaba bajo el
    mínimo cacheable de algunos modelos (4096 tokens en Haiku) y el cache
    rendía exactamente 0 en producción.
    """
    fewshot_dir = files("sas_migrator.llm") / "prompts" / "fewshot"
    parts = [
        entry.read_text(encoding="utf-8")
        for entry in sorted(fewshot_dir.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".md")
    ]
    return "# Ejemplos de traducción (formato exacto del output)\n\n" + "\n\n".join(parts)


def build_analysis_system() -> list[str]:
    return [_read("analysis_pfd.md")]


def build_improvements_system() -> list[str]:
    return [_read("analysis_improvements.md")]


def build_matching_system() -> list[str]:
    return [_read("matching.md")]


def build_diagnosis_system() -> list[str]:
    return [_read("mismatch_diagnosis.md")]


def build_docs_system() -> list[str]:
    return [_read("doc_writer.md")]


def build_verify_system() -> list[str]:
    return [_read("verify_system.md")]


def build_verify_user(target: dict, node_code: str, translation_json: str) -> str:
    """User del verificador: header + SAS original + traducción a revisar."""
    head = header_line(
        {
            "node_id": target.get("node_id"),
            "node_label": target.get("node_label", ""),
            "strategy": target.get("strategy", "pandas"),
            "input_datasets": target.get("input_datasets", []),
            "output_datasets": target.get("output_datasets", []),
            "output_tables": target.get("output_tables", []),
        }
    )
    return (
        head
        + "\n\n## SAS original\n```sas\n"
        + (node_code or "(sin código)")
        + "\n```\n\n## Traducción a verificar (NodeTranslation)\n```json\n"
        + translation_json
        + "\n```\n"
    )


def build_translation_system(project_context: str | None = None) -> list[str]:
    """System de traducción en (hasta) TRES bloques con cache escalonado.

    1. Contrato + ambas tablas de patrones — idéntico entre proyectos.
    2. Few-shot curado del eval set — idéntico entre proyectos.
    3. Contexto del proyecto — estable por corrida.

    Cada bloque lleva su propio breakpoint de cache (lo pone el cliente): si
    cambia el contexto del proyecto, los bloques 1-2 siguen cacheados.
    """
    blocks = [
        _read("translation_system.md")
        + "\n\n"
        + _read("patterns_sas_pandas.md")
        + "\n\n"
        + _read("patterns_sas_tsql.md"),
        _read_fewshot(),
    ]
    if project_context:
        blocks.append(project_context)
    return blocks


def build_project_context(
    *,
    connections: list[dict],
    improvements: list[dict],
    macro_param_values: dict,
    allowed_imports: list[str] | None = None,
    api_connections: list[dict] | None = None,
) -> str | None:
    """Bloque system con lo que el proyecto sabe y el traductor necesitaba.

    Responde la queja literal del modelo en sus warnings de producción: no
    sabía a qué base apunta cada alias, qué mejoras M-xxx aplicar ni qué valor
    tienen las macro vars. Es texto determinista (orden de archivo / sorted)
    para que el prefijo cachee entre nodos. ``None`` si no hay nada que decir.
    """
    parts: list[str] = ["# Contexto del proyecto (estable durante la corrida)"]
    if connections:
        parts.append(
            "\n## Conexiones de BD (alias = libref del SAS)\n"
            "Todo acceso a estas tablas usa la conexión del alias; un libref "
            "que no esté acá NO es una BD."
        )
        for c in connections:
            tablas = ", ".join(str(t) for t in (c.get("tables") or [])) or "(sin listar)"
            parts.append(
                f"- `{c.get('alias', '')}` → database `{c.get('database', '')}`"
                f" (role: {c.get('role', 'source')}) — tablas: {tablas}"
            )
    if api_connections:
        parts.append(
            "\n## Conexiones externas (APIs HTTP)\n"
            "Decisión del usuario por host que el SAS consulta por HTTP:"
        )
        for c in api_connections:
            host = c.get("host", "")
            if c.get("mode") == "sdk":
                parts.append(
                    f"- `{host}` → usa el paquete `{c.get('package', '')}` "
                    "(librería oficial). NO repliques la llamada HTTP cruda; "
                    "credenciales por os.environ, jamás literales."
                )
            else:
                parts.append(
                    f"- `{host}` → replica la llamada con requests al MISMO host "
                    "y método que el SAS; credenciales por os.environ."
                )
    if improvements:
        parts.append(
            "\n## Mejoras aprobadas por el usuario (M-xxx)\n"
            "Aplícalas SOLO en sus nodos afectados; el user de cada nodo lista "
            "los ids que le tocan. El resto del código replica el SAS tal cual."
        )
        for imp in improvements:
            nodos = ", ".join(str(n) for n in (imp.get("affected_nodes") or [])) or "global"
            parts.append(
                f"- {imp.get('id', '')} — {imp.get('title', '')}: "
                f"{imp.get('description', '')} (nodos: {nodos})"
            )
    if macro_param_values:
        parts.append(
            "\n## Macro variables con valor declarado (run.macro_params)\n"
            "Suben a la celda de parámetros del notebook; usa el NOMBRE, no el "
            "literal."
        )
        for k in sorted(macro_param_values):
            parts.append(f"- &{k} = {macro_param_values[k]!r}")
    if allowed_imports:
        parts.append(
            "\n## Librerías permitidas en el código destino\n"
            "stdlib de Python más: "
            + ", ".join(sorted(allowed_imports))
            + ". Cualquier otro import hace fallar el chequeo estático."
        )
    if len(parts) == 1:
        return None
    return "\n".join(parts)


_OMITTED_MARK = re.compile(r"^… \(\d+ items omitidos\)$")


def _iter_lists(obj):
    if isinstance(obj, list):
        yield obj
        for v in obj:
            yield from _iter_lists(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_lists(v)


def _kept_items(lst: list) -> list:
    """Los items reales de la lista (sin el marcador de omisión)."""
    if lst and isinstance(lst[-1], str) and _OMITTED_MARK.match(lst[-1]):
        return lst[:-1]
    return lst


def _halve(lst: list) -> None:
    kept = _kept_items(lst)
    previos = 0
    if len(kept) != len(lst):
        previos = int(re.search(r"\d+", lst[-1]).group())
    quedan = max(1, len(kept) // 2)
    omitidos = previos + (len(kept) - quedan)
    lst[:] = kept[:quedan] + [f"… ({omitidos} items omitidos)"]


def json_excerpt(data, limit: int = 40_000) -> str:
    """JSON de a lo sumo ~limit chars que SIGUE SIENDO JSON válido.

    El slicing crudo (``json.dumps(x)[:40_000]``) mandaba al modelo un objeto
    cortado a mitad de una llave, sin avisar: el modelo lo "repara" mentalmente
    y razona sobre datos que no existen. Acá se recortan ITEMS de las listas
    (las más pesadas primero) y cada recorte se anuncia dentro del propio JSON
    con "… (K items omitidos)". Si no hay listas que achicar, el fallback
    envuelve el texto cortado en un objeto que declara el recorte.
    """
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    data = json.loads(text)  # copia mutable ya normalizada por default=str
    for _ in range(500):
        shrinkable = [l for l in _iter_lists(data) if len(_kept_items(l)) > 1]
        if not shrinkable:
            break
        target = max(
            shrinkable, key=lambda l: len(json.dumps(l, ensure_ascii=False))
        )
        _halve(target)
        text = json.dumps(data, ensure_ascii=False)
        if len(text) <= limit:
            return text
    return json.dumps(
        {
            "_recorte": (
                "objeto sin listas reducibles; texto JSON cortado — NO es el "
                "documento completo"
            ),
            "_texto": text[:limit],
        },
        ensure_ascii=False,
    )


def header_line(payload: dict) -> str:
    """Primera línea del mensaje user: contexto estructurado en JSON.

    Da al modelo (y a los fakes de test) los identificadores exactos que el
    output debe ecoar (node_id, strategy, pfd_id...).
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_translation_user(
    target: dict, node_code: str, db_aliases: list[str],
    iteration_note: str | None = None,
    review_note: str | None = None,
    dependencies_context: dict | None = None,
) -> str:
    head = header_line(
        {
            "node_id": target.get("node_id"),
            "node_label": target.get("node_label", ""),
            "strategy": target.get("strategy", "pandas"),
            "placement": target.get("placement"),
            "dependencies": target.get("dependencies", []),
            "approved_improvements": target.get("approved_improvements", []),
            "db_aliases": db_aliases,
            # Vista v2 del parser: qué consume y qué produce este nodo. Sin
            # esto el modelo adivinaba los nombres de los DataFrames.
            "input_datasets": target.get("input_datasets", []),
            "output_datasets": target.get("output_datasets", []),
            "output_tables": target.get("output_tables", []),
            "macro_params": target.get("macro_params", []),
        }
    )
    user = head
    if dependencies_context:
        user += (
            "\n\n## Qué deja cada dependencia (ya traducida antes en el notebook)\n"
            + header_line(dependencies_context)
        )
    if review_note:
        user += f"\n\n## Nota del analista (fase 2) sobre este nodo\n{review_note}"
    user += (
        "\n\nCódigo SAS del nodo:\n```sas\n"
        + (node_code or "(sin código)")
        + "\n```\n"
    )
    if iteration_note:
        user += (
            "\n## Iteración solicitada (Fase 9)\n"
            "Re-traduce el nodo aplicando este ajuste pedido por el usuario:\n"
            f"{iteration_note}\n"
        )
    return user
