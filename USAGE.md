# FPA Library — Usage Guide

A DuckDB-centric Python library for resolving financial measures across time
periods and scenarios.  Base measures are defined as SQL filter queries; the
engine handles date filtering, scenario filtering, and GROUP BY automatically.

For concepts and design rationale, see [OVERVIEW.md](OVERVIEW.md).
For a terse AI-facing reference, see [AI_CONTEXT.md](AI_CONTEXT.md).

---

## Installation

```bash
pip install git+https://github.com/you/fpa.git
pip install duckdb
```

Core dependencies (auto-installed):
```
pandas
python-dateutil
networkx
```

---

## Quickstart

```python
import fpa
import duckdb
from datetime import date

con = duckdb.connect("warehouse.duckdb")

# 1. Configure the fiscal calendar
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# 2. Define measures
registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
        value_col="amount",
        date_col="period_enddate",
        agg_type=fpa.AggType.SUM,
    ),
    fpa.BaseMeasure(
        name="COGS",
        sql="SELECT * FROM general_ledger WHERE account_type = 'COGS'",
        value_col="amount",
        date_col="period_enddate",
        agg_type=fpa.AggType.SUM,
    ),
    fpa.Measure(
        name="Gross Profit",
        dependencies=["Revenue", "COGS"],
        formula=lambda v: v["Revenue"] - v["COGS"],
    ),
    fpa.Measure(
        name="Gross Margin %",
        dependencies=["Gross Profit", "Revenue"],
        formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0,
    ),
])

# 3. Build the calculator and request a P&L table
calc = fpa.Calculator(registry, connection=con)
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    ["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    months,
    scenario="Actual",
)
# Returns a pandas DataFrame:
#                  Jan 2024   Feb 2024   ...   Dec 2024
# Revenue           850,000    920,000          1,100,000
# COGS              340,000    368,000            440,000
# Gross Profit      510,000    552,000            660,000
# Gross Margin %       60.0       60.0               60.0
```

---

## FiscalCalendar

Configure once at startup.

```python
# Calendar year (Jan–Dec)
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# Fiscal year starting in July, labelled by the year it ends
calendar = fpa.FiscalCalendar(fiscal_year_start_month=7, year_label_convention="ending")
# Jul 2024 – Jun 2025 is labelled "FY2025"
```

### Getting a single period

```python
jan = calendar.month_period(date(2024, 1, 15))    # → Period("Jan 2024")
q1  = calendar.quarter_period(date(2024, 1, 15))  # → Period("FY2024 Q1")
fy  = calendar.year_period(date(2024, 1, 15))     # → Period("FY2024")

p = calendar.period_for(date(2024, 1, 15), fpa.Grain.MONTH)
```

### Getting ranges

```python
months   = calendar.month_range(date(2024, 1, 1), date(2024, 12, 31))
quarters = calendar.quarter_range(date(2024, 1, 1), date(2024, 12, 31))

months   = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)    # 12 items
quarters = calendar.periods_for_fiscal_year(2024, fpa.Grain.QUARTER)  # 4 items
year     = calendar.periods_for_fiscal_year(2024, fpa.Grain.YEAR)     # [FY2024]
```

### Navigating from a period

```python
prior_month = calendar.prior_period(jan)
prior_year  = calendar.prior_year_period(jan)
ytd         = calendar.ytd_periods(march)      # [Jan, Feb, Mar 2024]
rolling_12  = calendar.rolling_periods(aug, 12)
```

### Period attributes

```python
period.start             # date — first day (inclusive)
period.end               # date — last day (inclusive)
period.label             # "Jan 2024", "FY2024 Q1", "FY2024"
period.grain             # Grain.MONTH | QUARTER | YEAR
period.fiscal_year       # int — e.g. 2024
period.fiscal_period_num # int — month: 1-12, quarter: 1-4, year: 1
period.calendar_year     # int
period.calendar_month    # int
```

---

## Measures

### BaseMeasure — data from DuckDB

```python
fpa.BaseMeasure(
    name="Revenue",
    sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
    value_col="amount",           # column to aggregate
    date_col="period_enddate",    # column for date-range filtering
    agg_type=fpa.AggType.SUM,    # how value_col is aggregated per period
    tags=["income_statement"],
    description="Total product revenue",
)
```

**sql** is your business-logic filter.  Use `SELECT *` so all dimension
columns (entity, department, account_id, …) are available for GROUP BY.  Do
NOT include WHERE conditions for date range, scenario, or dimension values —
those are appended by the engine automatically.  Trailing semicolons are
stripped automatically.

**value_col** is the numeric column to aggregate.  Required when `sql` is set.

**date_col** is the date column used for period filtering.  Defaults to the
Calculator's `date_col` argument if not set on the measure.  If `date_col`
does not match an actual column in the query result, the `FILTER` clause
matches no rows and every cell silently returns 0.  Always verify the column
name against your table schema.

**agg_type** controls the SQL aggregation function:

| AggType | SQL | Use for |
|---|---|---|
| `SUM` | `COALESCE(SUM(value_col) FILTER (WHERE …), 0)` | Revenue, Expenses |
| `AVERAGE` | `COALESCE(AVG(value_col) FILTER (WHERE …), 0)` | Average Price |
| `LAST_DAY` | `arg_max(value_col, date_col) FILTER (WHERE …)` | Headcount, ARR, Balance |
| `CALCULATED` | not valid for BaseMeasure | — |

**resolver** is an optional Python callable `(CalculationContext) → float`.
Provide one if you need the library to work without a DuckDB connection (e.g.
unit tests, offline environments).  Return `None` to treat the value as 0.0.

### Measure — calculated from other measures

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],
    formula=lambda v: v["Revenue"] - v["COGS"],
    agg_type=fpa.AggType.CALCULATED,  # default
    tags=["income_statement"],
    description="Revenue minus cost of goods sold",
)
```

**formula** receives `{measure_name: float}` for every declared dependency.
Dependencies are resolved before the formula is called, in topological order.

**Python conditional formulas** work naturally:
```python
formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0
```
On the DuckDB path, the library attempts vectorized pandas arithmetic first
and falls back to row-wise `.apply()` automatically for formulas that cannot
be vectorized.

### Headcount (AggType.LAST_DAY)

```python
fpa.BaseMeasure(
    name="Headcount",
    sql="SELECT * FROM hr_snapshots WHERE status = 'Active'",
    value_col="employee_count",
    date_col="snapshot_date",
    agg_type=fpa.AggType.LAST_DAY,
)
```

`arg_max(employee_count, snapshot_date)` returns the headcount from the row
with the latest `snapshot_date` within the period — the correct period-end
snapshot for stock measures.

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()
registry.register(revenue)                   # one measure
registry.register_many([rev, cogs, gp, gm])  # many at once

measure = registry.get("Revenue")            # → BaseMeasure or Measure (KeyError if missing)
names   = registry.names()                   # → List[str]
bases   = registry.base_measures()           # → List[BaseMeasure]
derived = registry.derived_measures()        # → List[Measure]
tagged  = registry.by_tag("income_statement")# → List[AnyMeasure]

"Revenue" in registry                        # → bool
len(registry)                                # → int
```

---

## Calculator

```python
import duckdb

con = duckdb.connect("warehouse.duckdb")

calc = fpa.Calculator(
    registry,
    connection=con,          # open duckdb.DuckDBPyConnection
    date_col="date",         # default date column (overridden per-measure by BaseMeasure.date_col)
    scenario_col="scenario", # default "scenario"
)
```

### Resolve a single value (Python path)

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",
)
value = calc.resolve("Revenue", ctx)   # → float (memoized)
```

Requires `resolver` on BaseMeasure.

### Resolve multiple measures (Python path)

```python
values = calc.resolve_many(["Revenue", "COGS", "Gross Profit"], ctx)
# → {"Revenue": 850000.0, "COGS": 340000.0, "Gross Profit": 510000.0}
```

### build_table — measures × periods

Returns a `pd.DataFrame` with measures as rows and period labels as columns.

Uses the DuckDB path when a connection is present.  One SQL query per base
measure; derived measures computed in pandas.

```python
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,
    scenario="Actual",
    entity="North",     # optional fixed filters applied to every query
)
# table.loc["Revenue", "Jan 2024"]  → float
```

### build_breakdown_table — dimension values × periods

Returns a `pd.DataFrame` with dimension values as rows and period labels as
columns.

```python
# With explicit dimension_values — results filtered to those values
breakdown = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimension="department",
    dimension_values=["Engineering", "Sales", "Marketing"],
)

# Without dimension_values — returns every group DuckDB finds
# Safe for high-cardinality dimensions (100K+ distinct values)
breakdown = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimension="customer_id",
    # dimension_values omitted
)

# breakdown.loc["Engineering", "Jan 2024"]  → float
# Missing dimension values return 0.0 — no KeyError
```

Supported measure types: any base or derived measure.  Pass `"Gross Margin %"`
and the engine traces the DAG, fetches Revenue and COGS via SQL, then
computes Gross Profit and Gross Margin % per dimension group.

### Scenario comparison

```python
actuals  = calc.build_table(["Revenue", "Gross Profit"], months, scenario="Actual")
budget   = calc.build_table(["Revenue", "Gross Profit"], months, scenario="Budget")
variance = actuals - budget
pct_var  = (actuals - budget) / budget * 100
```

### Clear cache

```python
calc.clear_cache()   # call after underlying data changes
```

---

## CalculationContext

Frozen dataclass (hashable) — the single interface between the engine and
resolvers on the Python path.

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 3, 1)),
    scenario="Budget",
    entity="North",       # any keyword args become filters
    department="Sales",
)

# Inside a resolver
ctx.period    # Period object
ctx.scenario  # str
ctx.get("entity")      # → "North"  (None if not set)
ctx.get("missing", 0)  # → 0
ctx.filters            # tuple of sorted (key, value) pairs — use .get()
```

---

## Generated SQL

For a `build_breakdown_table("Revenue", months, scenario="Actual", dimension="department")` call,
the engine executes one query per base measure in the dependency chain.  For Revenue:

```sql
SELECT "department",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024",
    ...
FROM (SELECT * FROM general_ledger WHERE account_type = 'Income') __base
WHERE "scenario" = ?
GROUP BY "department"
```

For LAST_DAY (headcount):

```sql
SELECT "department",
    arg_max(employee_count, snapshot_date) FILTER (WHERE snapshot_date BETWEEN '2024-01-01' AND '2024-01-31') AS "Jan 2024",
    ...
FROM (SELECT * FROM hr_snapshots WHERE status = 'Active') __base
WHERE "scenario" = ?
GROUP BY "department"
```

Period start/end dates are embedded as ISO literals (they come from
`FiscalCalendar` — not user input).  Scenario, filter values, and dimension
values are always parameterized (`?`).

---

## Python Resolver Patterns

Provide a `resolver` alongside `sql` if you need the library to work without
a DuckDB connection.

### Flow measure

```python
def query_revenue(ctx):
    return float(db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM general_ledger
        WHERE account_type = 'Income'
          AND scenario = ?
          AND period_enddate BETWEEN ? AND ?
    """, [ctx.scenario, ctx.period.start, ctx.period.end]).scalar())

fpa.BaseMeasure(
    name="Revenue",
    sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
    value_col="amount",
    date_col="period_enddate",
    resolver=query_revenue,
)
```

### Resolver-only (no sql — Python path only)

```python
fpa.BaseMeasure(
    name="Revenue",
    resolver=lambda ctx: lookup[(ctx.scenario, ctx.get("entity"), ctx.period.label)],
)
```

### Prior period reference

```python
def query_mom_growth(ctx):
    prior_ctx = fpa.CalculationContext.make(
        period=calendar.prior_period(ctx.period),
        scenario=ctx.scenario,
        **dict(ctx.filters),
    )
    current = calc.resolve("Revenue", ctx)
    prior   = calc.resolve("Revenue", prior_ctx)
    return ((current - prior) / prior * 100) if prior else 0.0

registry.register(fpa.BaseMeasure(name="MoM Growth %", resolver=query_mom_growth))
```

---

## Fallback Rules

| Condition | Path used |
|---|---|
| No `connection` on Calculator | Python resolver |
| `connection` set, no base measure in chain has `sql` | Python resolver |
| `connection` set, at least one base measure has `sql` | DuckDB |
| Base measure has both `sql` and `resolver` | DuckDB (sql wins with connection) |

---

## What This Library Does NOT Do

- Connect to databases on the Python path — that's the resolver's job
- Cache source data between runs — only computed values are memoized
- Aggregate across grains — `period.start` / `period.end` define the range
- Produce reports, charts, or exports
- Handle forecasting, driver-based projections, or statistical models
