# FPA Library — AI Context

Terse reference for AI assistants, code generation, and rapid onboarding.
For full examples and rationale, see [USAGE.md](USAGE.md) and [OVERVIEW.md](OVERVIEW.md).

---

## What This Library Is

A DuckDB-centric Python calculation engine for financial measures across time
periods and scenarios.  Base measures are SQL filter queries; the engine
appends date, scenario, and dimension filters automatically and executes one
query per base measure.  Derived measures are vectorized pandas operations.

**Does NOT:**
- Execute queries on the Python path (resolvers own data access when used)
- Cache source data between runs
- Aggregate across grains (period.start / period.end define the range)
- Produce reports, charts, or exports
- Handle forecasting or driver-based models

---

## Install

```bash
pip install git+https://github.com/you/fpa.git
pip install duckdb
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
fpa.AggType              # Enum: SUM, LAST_DAY, AVERAGE, CALCULATED
fpa.BaseMeasure          # Leaf measure — SQL filter query + aggregation config
fpa.Measure              # Derived measure — formula over dependencies
fpa.AnyMeasure           # Type alias: BaseMeasure | Measure
fpa.MeasureRegistry      # Dict-based store for all measure definitions
fpa.Calculator           # Resolves measures; produces DataFrames
fpa.CalculationContext   # Frozen dataclass passed to Python-path resolvers
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
calendar.month_period(date(2024, 3, 15))    # → Period("Mar 2024")
calendar.quarter_period(date(2024, 3, 15))  # → Period("FY2024 Q1")
calendar.year_period(date(2024, 3, 15))     # → Period("FY2024")
calendar.period_for(date, grain)            # dispatch by Grain enum
```

**Ranges:**
```python
calendar.month_range(start_date, end_date)
calendar.quarter_range(start_date, end_date)
calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)    # 12 periods
calendar.periods_for_fiscal_year(2024, fpa.Grain.QUARTER)  # 4 periods
calendar.periods_for_fiscal_year(2024, fpa.Grain.YEAR)     # [FY2024]
```

**Navigation:**
```python
calendar.prior_period(period)
calendar.prior_year_period(period)
calendar.ytd_periods(period)        # MONTH only
calendar.rolling_periods(period, n) # MONTH only
```

---

## Period (frozen dataclass, hashable)

```python
period.grain              # Grain.MONTH | QUARTER | YEAR
period.start              # date — first day (inclusive)
period.end                # date — last day (inclusive)
period.fiscal_year        # int
period.fiscal_period_num  # month: 1-12, quarter: 1-4, year: 1
period.label              # "Jan 2024" | "FY2024 Q1" | "FY2024"
period.calendar_year      # int
period.calendar_month     # int
```

---

## AggType — controls SQL aggregation per period

| Value | SQL generated | Use for |
|---|---|---|
| `SUM` | `COALESCE(SUM(value_col) FILTER (WHERE date_col BETWEEN start AND end), 0)` | Revenue, Expenses |
| `LAST_DAY` | `arg_max(value_col, date_col) FILTER (WHERE …)` | Headcount, Balance, ARR |
| `AVERAGE` | `COALESCE(AVG(value_col) FILTER (WHERE …), 0)` | Average Price |
| `CALCULATED` | n/a — derived only | Gross Margin %, Growth Rate |

---

## BaseMeasure

```python
fpa.BaseMeasure(
    name="Revenue",                    # str — unique registry key
    sql="SELECT * FROM gl WHERE account_type = 'Income'",
                                       # SQL filter query — SELECT * recommended
                                       # Do NOT include date/scenario/dimension WHERE clauses
                                       # Trailing semicolons stripped automatically
    value_col="amount",                # column to aggregate (required when sql set)
    date_col="period_enddate",         # date column (defaults to Calculator.date_col)
    agg_type=fpa.AggType.SUM,         # controls SQL aggregation function
    tags=["income_statement"],
    description="...",
    resolver=None,                     # optional Python fallback (CalculationContext → float)
)
```

- At least one of `sql` or `resolver` must be provided
- `value_col` is required when `sql` is set
- `resolver` returning `None` → treated as `0.0`
- `resolver` exceptions → re-raised as `RuntimeError("Resolver error in BaseMeasure 'name': …")`

---

## Measure

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],            # List[str] — at least one
    formula=lambda v: v["Revenue"] - v["COGS"],  # Callable(dict[str, float]) -> float
    agg_type=fpa.AggType.CALCULATED,             # default: CALCULATED
    tags=["income_statement"],
    description="...",
)
```

- `formula` receives `{name: float}` for every declared dependency
- Dependencies resolved in topological (DAG) order before formula is called
- Circular dependencies → `ValueError` at `Calculator()` construction
- Python conditionals work: `lambda v: (v["GP"] / v["Rev"] * 100) if v["Rev"] else 0.0`
  DuckDB path attempts vectorized pandas first; falls back to `.apply()` automatically

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()
registry.register(measure)               # one (raises ValueError on duplicate name)
registry.register_many([m1, m2, …])     # many at once
registry.get("Revenue")                  # → AnyMeasure (KeyError if missing)
registry.names()                         # → List[str]
registry.base_measures()                 # → List[BaseMeasure]
registry.derived_measures()              # → List[Measure]   (NOT .measures())
registry.by_tag("income_statement")      # → List[AnyMeasure]
"Revenue" in registry                    # → bool
len(registry)                            # → int
```

---

## CalculationContext (Python path)

Frozen dataclass — hashable, used as memo cache key.

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",
    entity="North",    # any kwargs become filters
)

ctx.period             # Period
ctx.scenario           # str
ctx.get("entity")      # → "North"  (None if not set)
ctx.get("missing", 0)  # → 0
ctx.filters            # tuple of sorted (key, value) pairs — use .get()
```

---

## Calculator

```python
# DuckDB path — primary
calc = fpa.Calculator(
    registry,
    connection=con,          # open duckdb.DuckDBPyConnection
    date_col="date",         # default date column (overridden by BaseMeasure.date_col)
    scenario_col="scenario"  # default "scenario"
)

# Python path — no database required (BaseMeasures need resolver)
calc = fpa.Calculator(registry)
```

### resolve / resolve_many (Python path)

```python
value  = calc.resolve("Revenue", ctx)                    # → float (memoized)
values = calc.resolve_many(["Revenue", "COGS"], ctx)     # → dict[str, float]
```

### build_table — measures × periods

```python
df = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,
    scenario="Actual",
    entity="North",   # optional fixed filters → WHERE "entity" = ?
)
# df: pd.DataFrame, index=measure_names, columns=period.label strings
# df.loc["Revenue", "Jan 2024"]  → float
```

DuckDB path: one SQL query per base measure with no GROUP BY.
Python path: one resolver call per (measure, period) cell, memoized.

### build_breakdown_table — dimension values × periods

```python
df = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimension="department",
    dimension_values=["Eng", "Sales"],   # optional — omit to return all groups
    entity="North",                      # optional fixed filters
)
# df: pd.DataFrame, index=dimension_values (or all found groups), columns=period.label strings
# df.loc["Eng", "Jan 2024"]  → float
# Missing dimension values → 0.0  (no KeyError)
```

DuckDB path: one SQL query per base measure with GROUP BY dimension.
Python path: one resolver call per (dimension_value, period) cell; dimension_values required.

### Cache

```python
calc.clear_cache()   # call after underlying data changes
```

---

## Generated SQL (DuckDB path)

One query per BaseMeasure.  Period dates are embedded as ISO literals (from
FiscalCalendar — not user input).  Everything else is parameterized.

**SUM — build_breakdown_table:**
```sql
SELECT "department",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024"
FROM (SELECT * FROM general_ledger WHERE account_type = 'Income') __base
WHERE "scenario" = ?
  [AND "dimension" IN (?, …)]
GROUP BY "department"
```

**LAST_DAY — headcount:**
```sql
SELECT "entity",
    arg_max(employee_count, snapshot_date) FILTER (WHERE snapshot_date BETWEEN '2024-01-01' AND '2024-01-31') AS "Jan 2024"
FROM (SELECT * FROM hr_snapshots WHERE status = 'Active') __base
WHERE "scenario" = ?
GROUP BY "entity"
```

**build_table (no dimension):** same structure without SELECT dimension and GROUP BY.

---

## Fallback Rules

| Condition | Path |
|---|---|
| No `connection` on Calculator | Python resolver (requires resolver on all BaseMeasures) |
| `connection` set, no base measure in chain has `sql` | Python resolver |
| `connection` set, at least one base measure has `sql` | DuckDB (sql measures via SQL, resolver-only measures via Python) |
| `build_table` or `build_breakdown_table`, connection present | DuckDB |

---

## Key Design Rules

1. `sql` and `resolver` are both optional, but at least one must be set
2. `value_col` is required when `sql` is set
3. `date_col` on BaseMeasure overrides Calculator's `date_col` for that measure
4. `dimension_values=None` on DuckDB path → GROUP BY returns all groups (no IN clause)
5. `dimension_values=None` on Python path → raises `ValueError`
6. Period dates in SQL are ISO literals from FiscalCalendar, never user input
7. All other WHERE values are parameterized (`?`) to prevent SQL injection
8. `derived_measures()` returns only `Measure`; `base_measures()` returns only `BaseMeasure`
9. Never call `registry.measures()` — use `registry.derived_measures()`
10. All dependencies must be registered before `Calculator` is constructed

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
├── test_calculator.py            # CalculationContext, Python path, DuckDB path
├── test_fiscal_calendar.py
├── test_period.py
├── test_measure.py
├── test_registry.py
└── test_dag.py

smoke_test.py                     # end-to-end example with DuckDB
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
| Providing `sql` without `value_col` | Set `value_col` to the numeric column to aggregate |
| Omitting both `sql` and `resolver` | Provide at least one |
| Omitting `dimension_values` on Python path | Provide explicit values or use a DuckDB connection |
| Including date/scenario WHERE in `sql` | Don't — the engine appends those automatically |
| Wrong `date_col` value | Every cell silently returns 0 — the `FILTER` clause matches no rows. Verify the column name against your table schema. |
| Expecting `LAST_DAY` to accumulate history | It returns the value from the latest date **within the period**, not all-time |
| Circular measure dependency | Raises `ValueError` at `Calculator()` construction |
| Duplicate measure name | Raises `ValueError` at `registry.register()` |
