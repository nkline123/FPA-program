# FPA Library — Quick Reference

Terse API reference for rapid onboarding.
For full examples and rationale, see [USAGE.md](USAGE.md) and [OVERVIEW.md](OVERVIEW.md).

---

## What This Library Is

A DuckDB-centric Python calculation engine for financial measures across time
periods and scenarios.  Measures are SQL queries or Python formulas; the engine
builds CTE-chained queries covering all periods simultaneously and supports SQL
composition via `measure.<name>` references.

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
fpa.AggType              # Enum: SUM, LAST_DAY, AVERAGE, CUMULATIVE_END, CUMULATIVE_START, CALCULATED
fpa.Measure              # Single measure class — SQL leaf, SQL composed, formula, or resolver
fpa.AnyMeasure           # Type alias for Measure (backward compat)
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
| `SUM` | `COALESCE(SUM(value_col) FILTER (WHERE date BETWEEN start AND end), 0)` | Revenue, Expenses |
| `LAST_DAY` | `arg_max(value_col, date_col) FILTER (WHERE …)` | Headcount, ARR snapshot |
| `AVERAGE` | `COALESCE(AVG(value_col) FILTER (WHERE …), 0)` | Average Price |
| `CUMULATIVE_END` | `COALESCE(SUM(value_col) FILTER (WHERE date <= period_end), 0)` | Balance sheet closing |
| `CUMULATIVE_START` | `COALESCE(SUM(value_col) FILTER (WHERE date < period_start), 0)` | Balance sheet opening |
| `CALCULATED` | n/a — formula measures only | Gross Margin %, Growth Rate |

---

## Measure

Single class for all measure types.  Execution path is determined by which
fields are set.

```python
fpa.Measure(
    name="Revenue",                         # str — unique registry key

    # --- SQL path (leaf or composed) ---
    sql="SELECT * FROM gl WHERE ...",       # leaf: reference real tables
                                            # composed: use measure.<name> to reference another measure
                                            # Do NOT include date or dimension WHERE clauses — engine appends those
                                            # For scenario_col measures, do NOT include a scenario WHERE — engine appends it
                                            # For scenario= measures, DO include the scenario filter in the SQL itself
                                            # Trailing semicolons stripped automatically
    value_col="amount",                     # column to aggregate (required on leaf measures)
    date_col="period_enddate",              # date column (default: Calculator.date_col)
    agg_type=fpa.AggType.SUM,              # aggregation type (inherited by composed measures)
    scenario_col="scenario",                # scenario column name (default: Calculator.scenario_col)
                                            # engine injects WHERE scenario_col = ? automatically
    scenario="Actual",                      # label for pre-scoped data with no scenario column
                                            # engine skips scenario WHERE; cannot combine with scenario_col

    # --- Python formula path ---
    dependencies=["Revenue", "COGS"],       # List[str] — at least one required with formula
    formula=lambda v: v["Revenue"] - v["COGS"],  # Callable(MeasureValues) → float

    # --- Python resolver path ---
    resolver=lambda ctx: ...,               # Callable(CalculationContext) → float
                                            # used when no DuckDB connection, or as fallback

    # --- metadata ---
    tags=["income_statement"],
    description="...",
)
```

**Rules:**
- At least one of `sql`, `formula`, or `resolver` must be set
- `sql` and `formula` are mutually exclusive
- `formula` and `resolver` are mutually exclusive
- `value_col` is required on leaf SQL measures (no `measure.<name>` refs in sql)
- Leaf SQL measures require either `scenario_col` or `scenario` (but not both)
- `formula` requires at least one `dependencies` entry
- `AggType.CALCULATED` is not valid on SQL measures
- Composed measures inherit `value_col`, `date_col`, `agg_type`, `scenario_col` from nearest SQL ancestor
- `scenario` labels pre-scoped data (SQL already filters to one scenario); engine skips scenario WHERE for that measure. Cannot be combined with `scenario_col`
- `measure.<name>` SQL references require names with only word characters (`[a-zA-Z0-9_]`); names with spaces/special characters work as formula `dependencies` only

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()
registry.register(measure)               # one (ValueError on duplicate name)
registry.register_many([m1, m2, …])     # many at once
registry.get("Revenue")                  # → Measure (KeyError if missing)
registry.names()                         # → List[str]
registry.all_measures()                  # → List[Measure]
registry.sql_measures()                  # → List[Measure] — those with sql set
registry.formula_measures()              # → List[Measure] — those with formula set
registry.by_tag("income_statement")      # → List[Measure]
"Revenue" in registry                    # → bool
len(registry)                            # → int
```

---

## CalculationContext (Python path)

Frozen dataclass — hashable, used as memo cache key.

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",                    # str — required
    entity="North",                       # scalar filter → WHERE "entity" = ?
    department=["Sales", "Mktg"],         # list filter → WHERE "department" IN (?, ?)
)

ctx.period             # Period
ctx.scenario           # str
ctx.get("entity")      # → "North"
ctx.get("department")  # → ("Sales", "Mktg")  — lists stored as tuples
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
    date_col="date",         # default date column (overridden by Measure.date_col)
    scenario_col="scenario", # default scenario column (overridden by Measure.scenario_col)
    calendar=calendar,       # required for time-shifted lookups in formula measures
)

# Python path — no database required (measures need resolver)
calc = fpa.Calculator(registry)
```

### resolve / resolve_many

```python
value  = calc.resolve("Revenue", ctx)                    # → float (memoized)
values = calc.resolve_many(["Revenue", "COGS"], ctx)     # → dict[str, float]
```

### build_table — measures × periods

```python
df = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,
    scenario="Actual",           # str — required
    entity="North",              # scalar filter → WHERE "entity" = ?
    department=["Sales", "Mktg"] # list filter → WHERE "department" IN (?, ?)
)
# df: pd.DataFrame, index=measure_names, columns=period.label strings
# df.loc["Revenue", "Jan 2024"]  → float
```

### build_breakdown_table — dimension value(s) × periods

```python
# Single dimension — plain Index on rows
df = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimensions="department",             # str or List[str]
    dimension_values=["Eng", "Sales"],   # optional — omit to return all groups
    entity="North",                      # optional fixed filters
)
# df: pd.DataFrame, index=dimension values, columns=period.label strings
# df.loc["Eng", "Jan 2024"]  → float
# Missing dimension values → 0.0  (no KeyError)

# Multiple dimensions — MultiIndex on rows
df = calc.build_breakdown_table(
    measure_name="Expense",
    periods=months,
    scenario="Actual",
    dimensions=["entity", "department"],
    dimension_values=[                   # list of tuples; omit for all groups
        ("North", "Sales"),
        ("South", "Marketing"),
    ],
)
# df: pd.DataFrame with MultiIndex(names=["entity", "department"])
# df.loc[("North", "Sales"), "Jan 2024"]  → float
# Missing combinations → 0.0  (no KeyError)
```

**dimension_values** serves two purposes:
- **Python path** (resolver-only, no DuckDB connection): required. The library
  cannot enumerate dimension combinations without a database. For multiple
  dimensions, pass a list of tuples.
- **DuckDB path**: optional. Omit to get every group DuckDB finds (efficient
  even at high cardinality). Supply it to fix the row set, row order, or to
  ensure zero-rows for absent combinations.

### Cache

```python
calc.clear_cache()   # call after underlying data changes
```

---

## Generated SQL (DuckDB path)

CTE chain per terminal SQL measure.  Period dates embedded as ISO literals;
everything else parameterized.

**SUM — build_breakdown_table, single dimension:**
```sql
WITH "Expense" AS (
    SELECT * FROM gl WHERE account_id IN ('6000','6010')
),
"S&M Expense" AS (
    SELECT * FROM "Expense" WHERE department IN ('Sales', 'Marketing')
)
SELECT "department",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024"
FROM "S&M Expense"
WHERE "scenario" = ?
GROUP BY "department"
```

With `dimension_values=["Sales", "Marketing"]` adds:
```sql
  AND "department" IN (?, ?)
```

**SUM — build_breakdown_table, multiple dimensions:**
```sql
SELECT "entity", "department",
    COALESCE(SUM(amount) FILTER (WHERE date BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    ...
FROM "Expense"
WHERE "scenario" = ?
GROUP BY "entity", "department"
```

With `dimension_values=[("North", "Sales"), ("South", "Mktg")]` uses DuckDB's
row-value IN syntax:
```sql
  AND ("entity", "department") IN ((?, ?), (?, ?))
```

**build_table (no breakdown):** same structure without SELECT dimensions and GROUP BY.

**CUMULATIVE_END:**
```sql
COALESCE(SUM(amount) FILTER (WHERE date <= '2024-01-31'), 0.0) AS "Jan 2024"
```

**CUMULATIVE_START:**
```sql
COALESCE(SUM(amount) FILTER (WHERE date < '2024-01-01'), 0.0) AS "Jan 2024"
```

**LAST_DAY:**
```sql
arg_max(employee_count, snapshot_date) FILTER (WHERE snapshot_date BETWEEN '2024-01-01' AND '2024-01-31') AS "Jan 2024"
```

---

## Fallback Rules

| Condition | Path |
|---|---|
| No `connection` on Calculator | Python resolver (requires resolver on all SQL-less measures) |
| `connection` set, no SQL measure in chain | Python resolver |
| `connection` set, at least one SQL measure in chain | DuckDB |
| Measure has both `sql` and `resolver`, connection present | DuckDB (sql wins) |

---

## Key Design Rules

1. `value_col` is required on leaf SQL measures; inherited on composed measures
2. `date_col` on Measure overrides Calculator's `date_col` for that measure
3. `scenario_col` on Measure overrides Calculator's `scenario_col` for that measure
4. `scenario` on Measure labels pre-scoped data — engine skips scenario WHERE for that measure; cannot combine with `scenario_col`
5. Composed measures inherit metadata from nearest SQL ancestor; override only when genuinely different
6. `dimensions` accepts a single string or a list of strings — a list of one string behaves identically to a plain string (plain Index, no MultiIndex)
7. `dimensions` as a list of two or more strings → result has a pandas MultiIndex with one level per dimension, names set to the dimension column names
8. `dimension_values=None` on DuckDB path → GROUP BY returns all groups (no IN clause); efficient at any cardinality
9. `dimension_values` for a single dimension → list of scalars: `["North", "South"]`
10. `dimension_values` for multiple dimensions → list of tuples, one per desired row: `[("North", "Sales"), ("South", "Mktg")]`
11. `dimension_values=None` on Python path → raises `ValueError` (the library cannot enumerate combinations without a database)
12. Period dates in SQL are ISO literals from FiscalCalendar, never user input
13. All other WHERE values are parameterized (`?`) to prevent SQL injection
14. Circular dependencies → `ValueError` at `Calculator()` construction
15. Duplicate measure name → `ValueError` at `registry.register()`

---

## File Structure

```
fpa/
├── __init__.py                   # public API exports
├── calendar/
│   ├── period.py                 # Period, Grain, AggType
│   └── fiscal_calendar.py        # FiscalCalendar
├── measures/
│   ├── measure.py                # Measure, AnyMeasure
│   ├── measure_registry.py       # MeasureRegistry
│   └── dag.py                    # MeasureDAG (networkx DiGraph, topo sort)
└── engine/
    ├── calculator.py             # CalculationContext, Calculator
    └── measure_values.py         # MeasureValues

tests/
├── test_calculator.py            # CalculationContext, Python path, DuckDB path
├── test_composition.py           # SQL composition and CTE chaining
├── test_cumulative.py            # CUMULATIVE_END and CUMULATIVE_START
├── test_filters.py               # IN-clause filters, multi-value kwargs
├── test_scenario.py              # scenario column, measure-level scenario
├── test_measure.py               # Measure validation and fields
├── test_measure_values.py        # MeasureValues indexing and time shifts
├── test_registry.py              # MeasureRegistry
├── test_dag.py                   # dependency graph
├── test_fiscal_calendar.py
├── test_period.py
└── test_issues.py

smoke_test.py                     # end-to-end example with DuckDB
```

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| `registry.base_measures()` / `registry.derived_measures()` | Use `registry.sql_measures()` / `registry.formula_measures()` |
| Providing `sql` without `value_col` on a leaf measure | Set `value_col` to the numeric column to aggregate |
| Using `measure.<name>` but forgetting to register the referenced measure | Register all ancestors before constructing `Calculator` |
| Omitting both `sql`, `formula`, and `resolver` | Provide at least one |
| Omitting `dimension_values` on Python path | Provide explicit values or use a DuckDB connection |
| Passing scalars in `dimension_values` when `dimensions` is a list | Use tuples: `[("North", "Sales"), ...]` — one tuple per row |
| Expecting a plain Index when `dimensions` is a multi-element list | A list of 2+ dimensions always returns a MultiIndex; use `.loc[("North", "Sales"), "Jan 2024"]` |
| Including date or dimension WHERE in `sql` | Don't — the engine appends those automatically |
| Including scenario WHERE in `sql` when using `scenario_col` | Don't — the engine appends it. Use `scenario=` (without `scenario_col`) if the SQL pre-filters to one scenario |
| Wrong `date_col` value | Every cell silently returns 0 — the `FILTER` clause matches no rows |
| Using `CUMULATIVE_END` for flow measures (Revenue, Expenses) | Use `SUM` for flows; `CUMULATIVE_END` accumulates all history to period end |
| Expecting `LAST_DAY` to accumulate history | It returns the value from the latest date **within the period**, not all-time |
| `AggType.CALCULATED` on a SQL measure | Only valid on formula measures; SQL measures use SUM / AVERAGE / LAST_DAY / CUMULATIVE_* |
| Circular measure dependency | Raises `ValueError` at `Calculator()` construction |
| Duplicate measure name | Raises `ValueError` at `registry.register()` |
