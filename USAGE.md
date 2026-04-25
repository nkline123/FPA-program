# FPA Library — Usage Guide

A Python library for resolving financial measures across time periods and scenarios.
It handles fiscal calendars, measure dependencies, scenario awareness, memoization,
and optional DuckDB-accelerated breakdowns.

For concepts and design rationale, see [OVERVIEW.md](OVERVIEW.md).
For a terse AI-facing reference, see [AI_CONTEXT.md](AI_CONTEXT.md).

---

## Installation

```bash
pip install git+https://github.com/you/fpa.git
```

Core dependencies (auto-installed):
```
pandas
python-dateutil
networkx
```

DuckDB is optional — required only for the SQL execution path:
```bash
pip install duckdb
```

---

## Quickstart

```python
import fpa
from datetime import date

# 1. Configure the fiscal calendar
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# 2. Define measures
registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        resolver=lambda ctx: my_db.sum(
            accounts=["4000"], start=ctx.period.start,
            end=ctx.period.end, scenario=ctx.scenario,
        ),
    ),
    fpa.BaseMeasure(
        name="COGS",
        resolver=lambda ctx: my_db.sum(
            accounts=["5000"], start=ctx.period.start,
            end=ctx.period.end, scenario=ctx.scenario,
        ),
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
calc = fpa.Calculator(registry)
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    ["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    months,
    scenario="Actual",
)
# Returns a pandas DataFrame:
#                Jan 2024   Feb 2024   ...   Dec 2024
# Revenue         850,000    920,000          1,100,000
# COGS            340,000    368,000            440,000
# Gross Profit    510,000    552,000            660,000
# Gross Margin %     60.0       60.0               60.0
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

# Fiscal year starting in October, labelled by the year it starts
calendar = fpa.FiscalCalendar(fiscal_year_start_month=10, year_label_convention="starting")
# Oct 2024 – Sep 2025 is labelled "FY2024"
```

### Getting a single period

```python
jan   = calendar.month_period(date(2024, 1, 15))   # → Period("Jan 2024")
q1    = calendar.quarter_period(date(2024, 1, 15)) # → Period("FY2024 Q1")
fy    = calendar.year_period(date(2024, 1, 15))    # → Period("FY2024")

# Dispatch by grain enum
p = calendar.period_for(date(2024, 1, 15), fpa.Grain.MONTH)
```

### Getting ranges

```python
# All months from Jan 1 through Dec 31 (inclusive of any period that overlaps)
months   = calendar.month_range(date(2024, 1, 1), date(2024, 12, 31))   # 12 months
quarters = calendar.quarter_range(date(2024, 1, 1), date(2024, 12, 31)) # 4 quarters

# All periods in a complete fiscal year
months   = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)    # List[Period], 12 items
quarters = calendar.periods_for_fiscal_year(2024, fpa.Grain.QUARTER)  # 4 items
year     = calendar.periods_for_fiscal_year(2024, fpa.Grain.YEAR)     # [FY2024]
```

### Navigating from a period

```python
prior_month   = calendar.prior_period(jan)         # Dec 2023
prior_q       = calendar.prior_period(q1)          # FY2023 Q4
prior_year    = calendar.prior_year_period(jan)    # Jan 2023 (same grain, one FY back)
ytd           = calendar.ytd_periods(march)        # [Jan, Feb, Mar 2024]
rolling_12    = calendar.rolling_periods(aug, 12)  # Sep 2023 – Aug 2024
```

### Period attributes

```python
period.start             # date — first day of the period (inclusive)
period.end               # date — last day of the period (inclusive)
period.label             # str  — "Jan 2024", "FY2024 Q1", "FY2024"
period.grain             # Grain.MONTH | Grain.QUARTER | Grain.YEAR
period.fiscal_year       # int  — e.g. 2024
period.fiscal_period_num # int  — month: 1-12, quarter: 1-4, year: always 1
period.calendar_year     # int  — period.start.year
period.calendar_month    # int  — period.start.month
```

---

## Measures

### BaseMeasure — data from your source

```python
fpa.BaseMeasure(
    name="Revenue",                    # str, unique in the registry
    resolver=my_callable,              # Callable(CalculationContext) -> float | None
    agg_type=fpa.AggType.SUM,         # default: SUM — metadata only, not enforced
    tags=["income_statement"],         # List[str], default []
    description="Total product revenue from account 4000",
    sql_expr="SUM(CASE WHEN account_id = '4000' "
             "AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)",
    # sql_expr is optional — enables the DuckDB execution path for breakdowns
)
```

**resolver** receives a `CalculationContext` and must return a `float` (or `None`,
which is treated as `0.0`). Exceptions are caught and re-raised as `RuntimeError`
with the measure name included.

**sql_expr** is a SQL fragment with `{start}` and `{end}` placeholders. When the
DuckDB path is active, this replaces the resolver. The expression should aggregate to
a single float per group — typically `SUM(CASE WHEN ... THEN amount ELSE 0 END)`.
Leave blank to always use the resolver.

### Measure — calculated from other measures

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],           # List[str], at least one required
    formula=lambda v: v["Revenue"] - v["COGS"], # Callable(dict[str, float]) -> float
    agg_type=fpa.AggType.CALCULATED,            # default: CALCULATED
    tags=["income_statement"],
    description="Revenue minus cost of goods sold",
)
```

**formula** receives `{measure_name: float}` for every declared dependency.
Dependencies are resolved before the formula is called, in topological order.
Circular dependencies raise `ValueError` when `Calculator` is instantiated.

**Python conditional formulas** work naturally:
```python
formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0
```
On the DuckDB path, the library attempts vectorized pandas arithmetic first and falls
back to row-wise `.apply()` automatically for formulas that cannot be vectorized.

### AggType

Metadata for the reporting layer — the library does not use it to aggregate.

| AggType | Meaning | Examples |
|---|---|---|
| `SUM` | Flow — accumulates over the period | Revenue, Expenses, Bookings |
| `LAST_DAY` | Stock — point-in-time at period end | Headcount, ARR, Cash Balance |
| `AVERAGE` | Rate — average over the period | Average Order Value, Average Price |
| `CALCULATED` | Ratio — must be recalculated, not summed | Gross Margin %, Growth Rate |

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()

# Register one measure (raises ValueError on duplicate name)
registry.register(revenue)

# Register many at once
registry.register_many([revenue, cogs, gross_profit, gross_margin])

# Retrieve
measure = registry.get("Revenue")           # → BaseMeasure or Measure (KeyError if missing)

# Query
names   = registry.names()                  # → List[str]
bases   = registry.base_measures()          # → List[BaseMeasure]
derived = registry.derived_measures()       # → List[Measure]
tagged  = registry.by_tag("income_statement") # → List[BaseMeasure | Measure]

# Membership
"Revenue" in registry                       # → bool
len(registry)                               # → int
```

---

## Calculator

### Python path (no DuckDB)

```python
calc = fpa.Calculator(registry)
```

### DuckDB path (fast breakdowns)

```python
import duckdb

con = duckdb.connect("warehouse.duckdb")     # or duckdb.connect() for in-memory

calc = fpa.Calculator(
    registry,
    connection=con,        # open duckdb.Connection
    table="gl",            # table name in DuckDB
    date_col="date",       # default "date"
    scenario_col="scenario",  # default "scenario"
)
```

`build_table` always uses the Python resolver path regardless — SQL has no throughput
advantage for summary tables with no GROUP BY dimension. `build_breakdown_table` uses
DuckDB when a connection, table, and at least one `sql_expr` are available.

### Resolve a single value

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",
)
value = calc.resolve("Revenue", ctx)   # → float (memoized)
```

### Resolve multiple measures for one context

```python
values = calc.resolve_many(["Revenue", "COGS", "Gross Profit"], ctx)
# → {"Revenue": 850000.0, "COGS": 340000.0, "Gross Profit": 510000.0}
```

### build_table — measures × periods

Returns a `pd.DataFrame` with measures as rows and period labels as columns.
Always uses the Python resolver path.

```python
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,
    scenario="Actual",
    entity="North",       # optional — forwarded to every resolver
    department="Sales",
)
# table.loc["Revenue", "Jan 2024"]   → float
# table.loc["Gross Margin %", "Q1"]  → float (if quarters were used)
```

### build_breakdown_table — dimension values × periods

Returns a `pd.DataFrame` with dimension values as rows and period labels as columns.
Uses DuckDB when available; falls back to Python otherwise.

```python
entities = ["North", "South", "West"]

breakdown = calc.build_breakdown_table(
    measure_name="Revenue",
    periods=months,
    scenario="Actual",
    dimension="entity",
    dimension_values=entities,
    # optional fixed filters applied to every cell:
    department="Sales",
)
# breakdown.loc["North", "Jan 2024"]  → float
# breakdown.loc["South", "Feb 2024"]  → float

# Missing dimension values return 0.0 — no KeyError
```

Supported measure types in breakdown:
- A base measure directly (`"Revenue"`)
- Any derived measure — dependencies are resolved automatically (`"Gross Profit"`,
  `"Gross Margin %"`)

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

Frozen dataclass (hashable) — the single interface between the engine and resolvers.

```python
# Preferred constructor
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 3, 1)),
    scenario="Budget",
    entity="North",        # any keyword args become filters
    department="Sales",
    customer_id=42,
)

# Inside a resolver
ctx.period    # Period object
ctx.scenario  # str
ctx.get("entity")      # → "North" (or None if not set)
ctx.get("customer_id") # → 42
ctx.get("missing")     # → None
ctx.get("missing", 0)  # → 0  (with default)
ctx.filters   # tuple of sorted (key, value) pairs — prefer .get()
```

---

## Resolver Patterns

### Flow measure — sum transactions within the period

```python
def query_revenue(ctx):
    entity = ctx.get("entity")
    sql = """
        SELECT COALESCE(SUM(amount), 0)
        FROM gl
        WHERE account_id IN ('4000', '4010')
          AND scenario = ?
          AND date BETWEEN ? AND ?
    """
    params = [ctx.scenario, ctx.period.start, ctx.period.end]
    if entity:
        sql += " AND entity = ?"
        params.append(entity)
    return float(db.execute(sql, params).scalar())
```

### Stock measure — cumulative through period end

Balance sheet accounts and headcount accumulate over all history; don't filter by
period start.

```python
def query_total_assets(ctx):
    return float(db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM gl
        WHERE account_id IN ('1000', '1010', '1020')
          AND scenario = ?
          AND date <= ?
    """, [ctx.scenario, ctx.period.end]).scalar())
```

### Headcount (active employees at period end)

```python
def query_headcount(ctx):
    entity = ctx.get("entity")
    mask = (
        (employees["start_date"] <= ctx.period.end) &
        (employees["end_date"].isna() | (employees["end_date"] >= ctx.period.start))
    )
    if entity:
        mask &= employees["entity"] == entity
    return float(mask.sum())
```

### Prior period reference

Call back into the calculator. Results are memoized, so there is no redundant work.

```python
# Wire calc into the resolver at definition time
def query_mom_growth(ctx):
    prior_ctx = fpa.CalculationContext.make(
        period=calendar.prior_period(ctx.period),
        scenario=ctx.scenario,
        **dict(ctx.filters),   # carry all filters through
    )
    current = calc.resolve("Revenue", ctx)
    prior   = calc.resolve("Revenue", prior_ctx)
    return ((current - prior) / prior * 100) if prior else 0.0

registry.register(fpa.BaseMeasure(name="MoM Growth %", resolver=query_mom_growth))
```

### YTD measure

```python
def query_revenue_ytd(ctx):
    fy_start = calendar._fiscal_year_start_date(ctx.period.fiscal_year)
    return float(db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM gl
        WHERE account_id IN ('4000', '4010')
          AND scenario = ?
          AND date BETWEEN ? AND ?
    """, [ctx.scenario, fy_start, ctx.period.end]).scalar())
```

### Multi-scenario resolver

```python
SCENARIO_TABLES = {"Actual": "gl_actual", "Budget": "gl_budget", "Forecast": "gl_forecast"}

def query_revenue(ctx):
    table = SCENARIO_TABLES.get(ctx.scenario, "gl_actual")
    return float(db.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM {table} WHERE account_id = '4000'"
        f" AND date BETWEEN ? AND ?",
        [ctx.period.start, ctx.period.end]
    ).scalar())
```

---

## Performance — Pre-load Data Once

`build_table` fires one resolver call per `(measure, period)` cell. If each resolver
call makes a live database query, a 5-measure × 12-month P&L fires up to 60 queries.
The solution is to pre-load all the data you need into a dict before building the table,
then have each resolver slice the in-memory dict.

```python
from collections import defaultdict

# Load once — one query for all accounts, all months
rows = db.execute("""
    SELECT scenario, account_id, entity,
           strftime('%Y-%m', date) AS month,
           SUM(amount) AS amount
    FROM gl
    WHERE scenario IN ('Actual', 'Budget')
      AND date BETWEEN '2024-01-01' AND '2024-12-31'
    GROUP BY scenario, account_id, entity, month
""").fetchall()

lookup = defaultdict(float)
for scenario, account_id, entity, month, amount in rows:
    lookup[(scenario, account_id, entity, month)] = amount

# Resolver slices the dict — zero database calls
def query_revenue(ctx):
    month = ctx.period.start.strftime("%Y-%m")
    entity = ctx.get("entity")
    accts = ["4000", "4010"]
    if entity:
        return sum(lookup[(ctx.scenario, a, entity, month)] for a in accts)
    # Sum across all entities
    return sum(
        v for (s, a, e, m), v in lookup.items()
        if s == ctx.scenario and a in accts and m == month
    )
```

With this pattern, `build_table` is effectively free — dict lookups are ~10M/sec.

---

## DuckDB Integration

### When to use it

`build_breakdown_table` with DuckDB excels when:
- You have many dimension values (50+ customers, regions, departments)
- Across many periods (12+ months)
- And the data lives in DuckDB (or a Parquet/CSV file DuckDB can scan)

At 10M rows × 50K dimension values × 60 periods, the DuckDB path is ~7× faster than
the pre-loaded dict approach and ~300× faster than live-query-per-cell.

`build_table` (no dimension, just measures × periods) always uses the Python path
regardless of whether DuckDB is configured. SQL has no advantage there.

### Setup

```python
import duckdb
import fpa

con = duckdb.connect("warehouse.duckdb")  # or an in-memory connection

registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        resolver=lambda ctx: 0.0,   # required — used by build_table and as fallback
        sql_expr=(
            "SUM(CASE WHEN account_id IN ('4000', '4010') "
            "AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"
        ),
    ),
    fpa.BaseMeasure(
        name="COGS",
        resolver=lambda ctx: 0.0,
        sql_expr=(
            "SUM(CASE WHEN account_id = '5000' "
            "AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"
        ),
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

calc = fpa.Calculator(registry, connection=con, table="gl")
```

### How it works

```python
# This fires one SQL query covering all dimension values × all periods:
breakdown = calc.build_breakdown_table(
    "Gross Margin %",
    months,
    scenario="Actual",
    dimension="entity",
    dimension_values=["North", "South", "West", "East"],
)
```

The generated SQL looks like:

```sql
SELECT entity,
       SUM(CASE WHEN account_id IN ('4000','4010') AND date BETWEEN '2024-01-01' AND '2024-01-31' THEN amount ELSE 0 END) AS "Jan 2024|Revenue",
       SUM(CASE WHEN account_id = '5000'           AND date BETWEEN '2024-01-01' AND '2024-01-31' THEN amount ELSE 0 END) AS "Jan 2024|COGS",
       SUM(CASE WHEN account_id IN ('4000','4010') AND date BETWEEN '2024-02-01' AND '2024-02-29' THEN amount ELSE 0 END) AS "Feb 2024|Revenue",
       SUM(CASE WHEN account_id = '5000'           AND date BETWEEN '2024-02-01' AND '2024-02-29' THEN amount ELSE 0 END) AS "Feb 2024|COGS",
       ...
FROM gl
WHERE scenario = 'Actual' AND entity IN ('North', 'South', 'West', 'East')
GROUP BY entity
```

Derived measures (`Gross Profit`, `Gross Margin %`) are then computed as vectorized
pandas operations on the resulting DataFrame.

### Mixed SQL and Python base measures

Base measures without `sql_expr` are filled via their Python resolver after the SQL
query returns. This lets you mix DuckDB-sourced and Python-computed base measures in
the same breakdown.

```python
# This base measure has no sql_expr — filled via resolver per cell
fpa.BaseMeasure(
    name="Manual Adjustment",
    resolver=lambda ctx: adjustments.get((ctx.get("entity"), ctx.period.label), 0.0),
    # no sql_expr
)

fpa.Measure(
    name="Adjusted Revenue",
    dependencies=["Revenue", "Manual Adjustment"],
    formula=lambda v: v["Revenue"] + v["Manual Adjustment"],
)

# build_breakdown_table: Revenue via SQL, Adjustment via Python, Adjusted Revenue computed
tbl = calc.build_breakdown_table("Adjusted Revenue", months, scenario="Actual",
                                  dimension="entity", dimension_values=entities)
```

### Fallback behavior

| Condition | Path used |
|---|---|
| No `connection` passed to `Calculator` | Python resolver |
| `connection` set, but no `sql_expr` on any required base measure | Python resolver |
| `connection` set, at least one base measure has `sql_expr` | DuckDB |

The fallback is automatic and silent — no configuration needed.

---

## What This Library Does NOT Do

- Connect to databases or execute queries (on the Python path — that's the resolver's job)
- Cache source data between runs (only computed values are memoized)
- Aggregate across grains automatically (resolvers handle their own date ranges)
- Enumerate dimension values for breakdowns (the caller provides them)
- Produce reports, charts, or exports
- Handle forecasting, driver-based projections, or statistical models

These belong in a layer built on top of this library.
