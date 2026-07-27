# Tabla de patrones: SAS → pandas

| SAS | Python/pandas |
|---|---|
| `DATA out; SET in;` | `out = in_.copy()` |
| `SET a b;` (concatenación) | `pd.concat([a, b], ignore_index=True)` |
| `MERGE a(in=x) b(in=y); BY k;` | `a.merge(b, on="k", how="outer", indicator=True)` y filtrar `_merge` según in= |
| `IF cond;` (subsetting) | `df = df[cond]` |
| `IF/THEN/ELSE` asignación | `np.where(cond, v1, v2)`; múltiples ramas → `np.select([c1, c2], [v1, v2], default)` |
| `WHERE=` dataset option | filtro booleano antes de operar |
| `KEEP=` / `DROP=` | `df[cols]` / `df.drop(columns=[...])` |
| `RENAME=(a=b)` | `df.rename(columns={"a": "b"})` |
| `PROC SORT; BY x DESCENDING y;` | `df.sort_values(["x", "y"], ascending=[True, False])` |
| `PROC SORT NODUPKEY; BY k;` | `df.drop_duplicates(subset=["k"], keep="first")` |
| `PROC MEANS; CLASS g; VAR v;` | `df.groupby("g")["v"].agg(["mean", "sum", "min", "max", "count"])` |
| `PROC FREQ; TABLES a*b;` | `pd.crosstab(df["a"], df["b"])`; una vía → `value_counts()` |
| `PROC TRANSPOSE; BY k; ID c; VAR v;` | `df.pivot(index="k", columns="c", values="v").reset_index()` |
| `FIRST.k` / `LAST.k` | `~df.duplicated("k")` / `~df.duplicated("k", keep="last")` (tras ordenar) |
| `RETAIN acum; acum + v;` | `df["acum"] = df["v"].cumsum()` (por grupo: `groupby(...)["v"].cumsum()`) |
| `LAG(v)` | `df["v"].shift(1)` (por grupo: `groupby(...)["v"].shift(1)`) |
| `PROC IMPORT DATAFILE= DBMS=XLSX` | `pd.read_excel(ruta, sheet_name=...)` |
| `PROC IMPORT DBMS=CSV` | `pd.read_csv(ruta, encoding="utf-8")` |
| `PROC EXPORT` | `df.to_excel(...)` / `df.to_csv(..., index=False)` — nunca to_parquet |
| `INPUT(x, best.)` / `PUT(x, fmt.)` | `pd.to_numeric(x, errors="coerce")` / `x.astype(str)` o format spec |
| `INTNX('month', d, n)` | `d + pd.DateOffset(months=n)` (inicio de mes: `.to_period("M").to_timestamp()`) |
| `INTCK('month', a, b)` | `(b.dt.year - a.dt.year) * 12 + (b.dt.month - a.dt.month)` |
| `MDY(m,d,y)` / `datepart()` | `pd.Timestamp(year, month, day)` / `.dt.date` — fechas SAS: origen 1960-01-01 (`pd.Timestamp("1960-01-01") + pd.to_timedelta(n, "D")`) |
| `COALESCE(a,b)` / missing `.` | `a.fillna(b)` / `pd.NA`/`np.nan` — cuidado: en SAS missing < todo en comparaciones |
| `COMPRESS/STRIP/UPCASE(s)` | `s.str.replace(" ", "")` / `s.str.strip()` / `s.str.upper()` |
| `SUBSTR(s,i,n)` / `SCAN(s,n,sep)` | `s.str[i-1:i-1+n]` / `s.str.split(sep).str[n-1]` (SAS indexa desde 1) |
| `%LET var = valor;` | variable Python al inicio de la celda con `# parámetro` |
| `FILENAME resp TEMP; PROC HTTP URL="..." METHOD="get" OUT=resp;` | `r = requests.get(url, params={...}, timeout=30); r.raise_for_status()` — la URL literal del SAS se descompone en `params`, nunca se arma por f-string |
| `LIBNAME lib json fileref=resp automap=create;` | `pd.json_normalize(r.json()[...])` — `automap` aplana el JSON; replicá la ruta de campos que el SAS lee después (`SUBSTR(indexdateString,...)`, etc.) |
| URL con credenciales en macro vars (`&user`, `&password`) | `os.environ["..."]` — jamás el literal; el scanner de secretos rechaza el ensamblado |
