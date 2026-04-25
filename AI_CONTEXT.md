# FPA Library — AI Context

Terse reference for AI assistants, code generation, and rapid onboarding.
For full examples and rationale, see [USAGE.md](USAGE.md) and [OVERVIEW.md](OVERVIEW.md).

---

## What This Library Is

A Python calculation engine for financial measures across time periods and scenarios.
Handles fiscal calendars, measure dependency resolution (DAG), scenario awareness,
dimensional filtering, memoization, and optional DuckDB-accelerated breakdowns.

**Does NOT:**
- Connect to databases or execute queries (Python path — resolvers own data access)
- Store or cache source data between runs
- Aggregate across grains (resolvers interpret `ctx.period.start` / `ctx.period.end`)
- Enumerate dimension values (caller provides them)
- Produce reports, charts, or exports
- Handle forecasting or driver-based models

---

## Install

```bash
pip install git+https://github.com/you/fpa.git
pip install duckdb   # optional — only needed for SQL execution path
```

```python
import fpa
```

---

## Public API (`fpa/__init__.py`)

```python
fpa.FiscalCalendar       # Fiscal calendar config and period navigation
fpa.Period               # Frozen, hashable dataclass for a time slice
fpa.Grain                # Enum: MONTH, QUARTER, YEAR
fpa.AggType              # Enum: SUM, LAST_DAY, AVERAGE, CALCULATED (metadata only)
fpa.BaseMeasure          # Leaf measure — value from a resolver callable
fpa.Measure              # Derived measure — value from a formula over dependencies
fpa.AnyMeasure           # Type alias: BaseMeasure | Measure
fpa.MeasureRegistry      # Dict-based store for all measure definitions
fpa.Calculator           # Resolves measures; produces DataFrames
fpa.CalculationContext   # Frozen dataclass passed to every resolver
```

---

## FiscalCalendar

```python
calendar = fpa.FiscalCalendar(
    fiscal_year_start_month=1,        # int 1-12, default 1 (calendar year)
    year_label_convention="ending",   # "ending" | "starting"
)
```

**Single period:**
```python
calendar.month_period(date(2024, 3, 15))   # → Period("Mar 2024")
calendar.quarter_period(date(2024, 3, 15)) # → Period("FY2024 Q1")
calendar.year_period(date(2024, 3, 15))    # → Period("FY2024")
calendar.period_for(date, grain)           # dispatch by Grain enum
```

**Ranges:**
```python
calendar.month_range(start_date, end_date)    # → List[Period]
calendar.quarter_range(start_date, end_date)  # → List[Period]
calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)    # all 12 months
calendar.periods_for_fiscal_year(2024, fpa.Grain.QUARTER)  # all 4 quarters
calendar.periods_for_fiscal_year(2024, fpa.Grain.YEAR)     # [FY2024]
```

**Navigation:**
```python
calendar.prior_period(period)       # previous period (same grain)
calendar.prior_year_period(period)  # same period one fiscal year earlier
calendar.ytd_periods(period)        # all months from FY start through period (MONTH only)
calendar.rolling_periods(period, n) # last n months ending at period (MONTH only)
```

---

## Period (frozen dataclass, hashable)

```python
period.grain              # Grain.MONTH | QUARTER | YEAR
period.start              # date — first day (inclusive)
period.end                # date — last day (inclusive)
period.fiscal_year        # int — e.g. 2024
period.fiscal_period_num  # int — month: 1-12, quarter: 1-4, year: 1
period.label              # str — "Jan 2024" | "FY2024 Q1" | "FY2024"
period.calendar_year      # int — period.start.year
period.calendar_month     # int — period.start.month
```

---

## AggType (metadata — NOT enforced by the library)

| Value | Meaning | Resolver uses |
|---|---|---|
| `SUM` | Flow — sum within period | `WHERE date BETWEEN start AND end` |
| `LAST_DAY` | Stock — value at period end | `WHERE date <= end` (cumulative) |
| `AVERAGE` | Rate — average over period | `AVG(...)` or `SUM/COUNT` within range |
| `CALCULATED` | Ratio — always recalculate, never sum | n/a — derived from other measures |

---

## BaseMeasure

```python
fpa.BaseMeasure(
    name="Revenue",           # str — unique registry key
    resolver=my_callable,     # Callable(CalculationContext) -> float | None
    agg_type=fpa.AggType.SUM, # default: SUM
    tags=["income_statement"],# List[str], default []
    description="...",        # str, default ""
    sql_expr="SUM(CASE WHEN account_id = '4000' "
             "AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)",
    # sql_expr: optional SQL fragment with {start} and {end} placeholders.
    # Used by the DuckDB path. Resolver is used by build_table and as fallback.
    # Omit or leave "" to always use the resolver.
)
```

- `resolver` may return `None` → treated as `0.0`
- Exceptions from `resolver` are caught and re-raised as `RuntimeError("Resolver error in BaseMeasure 'name': ...")`

---

## Measure

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],           # List[str] — at least one
    formula=lambda v: v["Revenue"] - v["COGS"], # Callable(dict[str, float]) -> float
    agg_type=fpa.AggType.CALCULATED,            # default: CALCULATED
    tags=["income_statement"],
    description="...",
)
```

- `formula` receives `{name: float}` for every declared dependency
- Dependencies resolved before formula is called, in topological (DAG) order
- Circular dependencies → `ValueError` at `Calculator()` construction, not resolution time
- Python conditionals work: `lambda v: (v["GP"] / v["Rev"] * 100) if v["Rev"] else 0.0`
  - DuckDB path attempts vectorized pandas arithmetic; falls back to `.apply()` automatically

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()
registry.register(measure)              # one (raises ValueError on duplicate name)
registry.register_many([m1, m2, ...])   # many at once
registry.get("Revenue")                 # → AnyMeasure (KeyError if missing)
registry.names()                        # → List[str]
registry.base_measures()                # → List[BaseMeasure]
registry.derived_measures()             # → List[Measure]  (NOT .measures())
registry.by_tag("income_statement")     # → List[AnyMeasure]
"Revenue" in registry                   # → bool
len(registry)                           # → int
```

---

## CalculationContext

Frozen dataclass — hashable, used as memo cache key.

```python
# Build (preferred)
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",
    entity="North",       # any kwargs become filters
    department="Sales",
)

# Access in resolver
ctx.period    # Period
ctx.scenario  # str
ctx.get("entity")      # → "North"  (None if not set)
ctx.get("missing")     # → None
ctx.get("missing", 0)  # → 0
ctx.filters   # tuple of sorted (key, value) pairs — use .get(), not direct access
```

---

## Calculator

```python
# Python path (always available)
calc = fpa.Calculator(registry)

# DuckDB path (fast breakdowns when sql_expr is set on BaseMeasures)
calc = fpa.Calculator(
    registry,
    connection=con,         # open duckdb.Connection
    table="gl",             # table name in DuckDB
    date_col="date",        # default "date"
    scenario_col="scenario" # default "scenario"
)
```

### resolve / resolve_many

```python
value  = calc.resolve("Revenue", ctx)                       # → float (memoized)
values = calc.resolve_many(["Revenue", "COGS"], ctx)        # → dict[str, float]
```

### build_table — measures × periods

Always uses Python resolver path.

```python
df = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,             # List[Period]
    scenario="Actual",
    entity="North",             # optional filters → ctx.get("entity")
    department="Sales",
)
# df: pd.DataFrame, index=measure_names, columns=period.label strings
# df.loc["Revenue", "Jan 2024"]  → float
```

### build_breakdown_table — dimension values × periods

Uses DuckDB if: `connection` set + `table` set + at least one base measure has `sql_expr`.
Otherwise falls back to Python.

```python
df = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimension="entity",
    dimension_values=["North", "South", "West"],  # caller provides these
    department="Sales",   # optional fixed filters
)
# df: pd.DataFrame, index=dimension_values, columns=period.label strings
# df.loc["North", "Jan 2024"]  → float
# Missing dimension values → 0.0  (no KeyError)
```

### Cache

```python
calc.clear_cache()   # call after underlying data changes
```

---

## DuckDB Path — What Gets Executed

One SQL query per `build_breakdown_table` call:

```sql
SELECT {dimension},
       {sql_expr_measure1_period1} AS "{period1_label}|{measure1}",
       {sql_expr_measure1_period2} AS "{period2_label}|{measure1}",
       {sql_expr_measure2_period1} AS "{period1_label}|{measure2}",
       ...
FROM {table}
WHERE {scenario_col} = '{scenario}'
  [AND {extra_filter} = '{value}' ...]
  AND {dimension} IN ({dimension_values})
GROUP BY {dimension}
```

After the query:
1. Base measures without `sql_expr` → filled via Python resolver per cell
2. Derived measures → vectorized pandas arithmetic (with `.apply()` fallback)

---

## Fallback Rules

| condition | path |
|---|---|
| No `connection` on Calculator | Python resolver |
| `connection` set, but no base measure in chain has `sql_expr` | Python resolver |
| `connection` set, at least one base measure has `sql_expr` | DuckDB |
| `build_table` (any configuration) | Always Python resolver |

---

## Key Design Rules

1. `resolver` on `BaseMeasure` is always required — it's the `build_table` fallback
2. `sql_expr` is always optional — omit to force Python path
3. `CalculationContext.filters` is a sorted tuple of pairs for hashability
4. All dependencies must be registered before `Calculator` is constructed
5. `derived_measures()` returns only `Measure` instances; `base_measures()` returns only `BaseMeasure`
6. Never call `registry.measures()` — it was renamed to `registry.derived_measures()`

---

## File Structure

```
fpa/
├── __init__.py                   # public API exports
├── calendar/
│   ├── period.py                 # Period, Grain, AggType
│   └── fiscal_calendar.py        # FiscalCalendar
├── measures/
│   ├── measure.py                # BaseMeasure, Measure, AnyMeasure
│   ├── measure_registry.py       # MeasureRegistry
│   └── dag.py                    # MeasureDAG (networkx DiGraph, topo sort)
└── engine/
    └── calculator.py             # CalculationContext, Calculator

tests/
├── test_calculator.py            # CalculationContext, Python path, DuckDB path (142 tests)
├── test_fiscal_calendar.py
├── test_period.py
├── test_measure.py
├── test_registry.py
└── test_dag.py

smoke_test.py                     # end-to-end example with sample data
sample_data/
├── generate_sample_gl.py
├── generate_sample_employees.py
├── sample_gl.csv
└── sample_employees.csv
```

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| `registry.measures()` | Use `registry.derived_measures()` |
| Live DB call per resolver in `build_table` | Pre-load into a dict; resolver slices the dict |
| Passing `sql_expr` without a real `resolver` | Always provide a working resolver — `build_table` never uses `sql_expr` |
| Expecting `build_table` to use DuckDB | It never does — use `build_breakdown_table` for the SQL path |
| Passing `date` objects as strings in `sql_expr` placeholders | `{start}` and `{end}` are ISO date strings (`"2024-01-01"`) — DuckDB handles the cast |
| Circular measure dependency | Raises `ValueError` at `Calculator()` construction with the cycle listed |
| Duplicate measure name | Raises `ValueError` at `registry.register()` |
