---
name: changelog
description: Mantener CHANGELOG.md y la versión de pyproject.toml al día. Usar SIEMPRE al commitear un cambio visible para el usuario del CLI (comando, flag, comportamiento, formato de salida, fix), y cuando el usuario pida un bump/release de versión.
---

# Changelog y versionado

Una sola fuente de verdad para la versión: `version` en `pyproject.toml`
(PEP 440: `2.0.0a1` = alpha 1). La instalación es editable, así que el bump
no requiere reinstalar. Cada versión publicada lleva tag `v<version>`.

## Al commitear un cambio

Antes de cada commit, preguntarse: ¿esto le cambia algo a quien *usa*
`sas-migrator`? (comando nuevo, flag, comportamiento distinto, formato de
artefacto/salida, bug que le afectaba).

- **Sí** → agregar una línea bajo `## [Unreleased]` en `CHANGELOG.md`,
  **en el mismo commit**, bajo la categoría que corresponda:
  - `### Added` — capacidad nueva
  - `### Changed` — comportamiento existente que cambia
  - `### Fixed` — bug corregido
  - `### Removed` — algo que deja de existir
  (crear la subsección solo si no existe; mantener el orden Added/Changed/Fixed/Removed)
- **No** (refactor interno, tests, docs de desarrollo) → no tocar el changelog.

Estilo de las líneas: como los commits de este repo — comportamiento en
español, orientado al efecto ("las conexiones HTTP se preguntan, no se
adivinan"), sin jerga interna de módulos.

## Al hacer bump de versión

Solo cuando el usuario lo pida o apruebe. Pasos, en un solo commit:

1. Elegir la versión nueva:
   - `aN → aN+1`: tanda significativa de cambios en alpha (caso normal hoy).
   - `a → b0`: alcance congelado, solo estabilización.
   - Final `2.0.0`: migró un proyecto real de punta a punta.
2. Editar `version` en `pyproject.toml`.
3. En `CHANGELOG.md`: renombrar `## [Unreleased]` a
   `## [<version>] - <fecha ISO de hoy>` y abrir un `## [Unreleased]` vacío
   encima.
4. Commit con mensaje `Release 2.0.0aN: <resumen de una línea>` y tag:
   `git tag v<version>`.
5. Verificar: `sas-migrator --version` debe mostrar la versión nueva
   (editable install — sin reinstalar).
