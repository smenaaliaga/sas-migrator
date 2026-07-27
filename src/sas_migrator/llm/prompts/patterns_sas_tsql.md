# Tabla de patrones: SAS SQL → T-SQL (ejecuta en la BD)

El SQL corre donde viven los datos, vía `pd.read_sql(text(sql), engine,
params=...)` para lecturas y `engine.begin()` + `conn.execute(text(sql))`
para escrituras. El SQL es un string LITERAL con parámetros `:nombre` —
jamás f-strings.

| SAS | T-SQL |
|---|---|
| `CREATE TABLE lib.t AS SELECT ...` | `SELECT ... INTO base.dbo.t` (tabla nueva) o DELETE+INSERT (tabla existente) |
| `LIBREF.TABLA` | `BASE.dbo.TABLA` según el mapeo de db_connections.yaml (alias = libref) |
| `&periodo` en WHERE | parámetro `:periodo` (sqlalchemy `text()` + params) |
| `CONNECT TO x; EXECUTE (...) BY x;` (passthrough) | el SQL interno se conserva casi literal — ya era SQL nativo |
| `SELECT ... FROM connection to x (...)` | `pd.read_sql(text(sql_interno), engine)` |
| `%SYSFUNC(TODAY())` / `DATE()` | `CAST(GETDATE() AS date)` |
| `PUT(x, yymmn6.)` | `FORMAT(fecha, 'yyyyMM')` / `CONVERT(varchar(6), fecha, 112)` |
| `INTNX('month', hoy, -1)` | `DATEADD(month, -1, fecha)` |
| `DATEPART(dt)` | `CAST(dt AS date)` |
| `COALESCE` / `CASE WHEN` | idénticos en T-SQL |
| `UPCASE/LOWCASE` | `UPPER/LOWER` |
| `SUBSTR(s,i,n)` | `SUBSTRING(s, i, n)` |
| `CATX(sep, a, b)` | `CONCAT_WS(sep, a, b)` |
| `INPUT(s, best.)` | `TRY_CAST(s AS float)` |
| outer union corr | `UNION ALL` con columnas alineadas explícitas |
| `CALCULATED alias` | repetir la expresión o usar CTE/subquery (T-SQL no permite alias en WHERE) |

## Semántica de escritura (espejo del SAS, no invención)

```
-- SAS reemplazaba (CREATE TABLE sobre tabla existente):
DELETE FROM BASE.dbo.T;
INSERT INTO BASE.dbo.T (...) SELECT ...;

-- SAS acumulaba (PROC APPEND / INSERT INTO): append tal cual + warning.
```

- `sql_pushdown`: TODO el cómputo va en el SQL (joins, agregaciones, CASE) —
  pandas solo dispara la ejecución y verifica conteos. Nada de leer la tabla
  completa para procesarla en pandas.
- `hybrid`: el `WHERE` del extract filtra al mínimo necesario; el resto del
  cómputo en pandas; la escritura replica la semántica SAS del nodo.
