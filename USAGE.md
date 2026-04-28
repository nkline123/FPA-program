# FPA Library — Usage Guide

A DuckDB-centric Python library for resolving financial measures across time
periods and scenarios. Measures are defined as SQL queries or Python formulas;
the engine handles date filtering, scenario filtering, CTE chaining, and GROUP
BY automatically.

For concepts and design rationale, see [OVERVIEW.md](OVERVIEW.md).
For a terse cheatsheet, see [REFERENCE.md](REFERENCE.md).

---

## Installation

```bash
pip install git+https://github.com/nkline123/FPA-program.git
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
    fpa.Measure(
        name="Revenue",
        sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
        value_col="amount",
        date_col="period_enddate",
        agg_type=fpa.AggType.SUM,
        scenario_col="scenario",
    ),
    fpa.Measure(
        name="COGS",
        sql="SELECT * FROM general_ledger WHERE account_type = 'COGS'",
        value_col="amount",
        date_col="period_enddate",
        agg_type=fpa.AggType.SUM,
        scenario_col="scenario",
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
calc = fpa.Calculator(registry, connection=con, calendar=calendar)
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

All measures use the single `fpa.Measure` class. The execution path is
determined by which fields are set.

### Leaf SQL measure — data from DuckDB

```python
fpa.Measure(
    name="Revenue",
    sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
    value_col="amount",           # column to aggregate (required for leaf measures)
    date_col="period_enddate",    # column for date-range filtering
    agg_type=fpa.AggType.SUM,    # how value_col is aggregated per period
    scenario_col="scenario",      # column holding the scenario label (defaults to Calculator.scenario_col)
    tags=["income_statement"],
    description="Total product revenue",
)
```

**sql** is your business-logic filter. Use `SELECT *` so all dimension
columns (entity, department, account_id, …) are available for GROUP BY. Do
NOT include WHERE conditions for date range or dimension values — those are
appended by the engine automatically. Trailing semicolons are stripped
automatically.

**value_col** is the numeric column to aggregate. Required on leaf measures
(those without `measure.<name>` references in their SQL).

**date_col** is the date column used for period filtering. Defaults to the
Calculator's `date_col` argument if not set on the measure.

**agg_type** controls the SQL aggregation function:

| AggType            | SQL                                                              | Use for                 |
| ------------------ | ---------------------------------------------------------------- | ----------------------- |
| `SUM`              | `COALESCE(SUM(value_col) FILTER (WHERE …), 0)`                   | Revenue, Expenses       |
| `AVERAGE`          | `COALESCE(AVG(value_col) FILTER (WHERE …), 0)`                   | Average Price           |
| `LAST_DAY`         | `arg_max(value_col, date_col) FILTER (WHERE …)`                  | Headcount, ARR snapshot |
| `CUMULATIVE_END`   | `COALESCE(SUM(value_col) FILTER (WHERE date <= period_end), 0)`  | Balance sheet closing   |
| `CUMULATIVE_START` | `COALESCE(SUM(value_col) FILTER (WHERE date < period_start), 0)` | Balance sheet opening   |
| `CALCULATED`       | not valid for SQL measures                                       | —                       |

`CUMULATIVE_END` and `CUMULATIVE_START` accumulate all transactions from the
beginning of history to the period boundary — use them for balance sheet
accounts sourced from a GL transaction table. The invariant
`CUMULATIVE_END - CUMULATIVE_START == SUM` holds for any period.

**scenario_col** names the column that holds the scenario label. Defaults to
the Calculator's `scenario_col` argument (`"scenario"`). Set this when your
table uses a different column name. The engine injects
`WHERE "scenario_col" = ?` automatically using the value passed to
`build_table`.

**scenario** is used when the source data has no scenario column — typically
because the SQL already filters to one scenario. Set it as a label so the
engine knows what scenario this measure represents; it will not inject a
scenario WHERE clause. Cannot be combined with `scenario_col`.

### Composed SQL measure — filtering on top of another measure

```python
fpa.Measure(
    name="Sales & Marketing Expense",
    sql="SELECT * FROM measure.Expense WHERE department IN ('Sales', 'Marketing')",
    # value_col / date_col / agg_type / scenario_col inherited from Expense
)
```

Use `measure.<name>` in the SQL to reference another measure. The engine
replaces these references with quoted CTE identifiers and builds a `WITH`
chain, so the parent measure's data is available without re-scanning the
source table.

> **Constraint:** Measure names used in `measure.<name>` references must
> contain only word characters (`[a-zA-Z0-9_]`). Names with spaces or special
> characters can be registered and used as formula `dependencies`, but cannot
> be referenced via `measure.<name>` in SQL.

Composed measures inherit `value_col`, `date_col`, `agg_type`, and
`scenario_col` from the nearest SQL ancestor that defines them. Override any
field on the composed measure when the aggregation genuinely changes (e.g.
switching from `SUM` to `LAST_DAY`).

You can add dimensions via join in a composed measure:

```python
fpa.Measure(
    name="Expense with Region",
    sql="""
        SELECT e.*, d.region
        FROM measure.Expense e
        JOIN dim_department d ON e.department = d.department
    """,
)
```

### Python formula measure — calculated from other measures

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],
    formula=lambda v: v["Revenue"] - v["COGS"],
)

fpa.Measure(
    name="Gross Margin %",
    dependencies=["Gross Profit", "Revenue"],
    formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0,
)
```

**formula** receives a `MeasureValues` object `v` for every declared
dependency. It behaves like a dict for plain lookups and adds two extra
capabilities:

```python
v["Revenue"]                          # current period value
v["Revenue", -12]                     # Revenue 12 months prior, same grain
v["Revenue", 0, fpa.Grain.MONTH]     # Revenue for start-of-period month (cross-grain)
v.period                              # the Period being resolved
v.scenario                            # the scenario string
```

**Python conditional formulas** work naturally. On the DuckDB path the
library attempts vectorized pandas arithmetic first and falls back to
row-wise `.apply()` automatically for formulas that use conditionals,
time-shifted lookups, or `v.period`.

### Balance sheet measures (CUMULATIVE_END / CUMULATIVE_START)

For accounts like Assets or Liabilities sourced from GL transactions:

```python
fpa.Measure(
    name="Assets",
    sql="SELECT * FROM gl WHERE account_id = '1000'",
    value_col="amount",
    date_col="date",
    agg_type=fpa.AggType.CUMULATIVE_END,   # closing balance at end of period
    scenario_col="scenario",
)

fpa.Measure(
    name="Assets Opening",
    sql="SELECT * FROM gl WHERE account_id = '1000'",
    value_col="amount",
    date_col="date",
    agg_type=fpa.AggType.CUMULATIVE_START,  # opening balance at start of period
    scenario_col="scenario",
)
```

### Pre-scoped measures (no scenario column)

Use `scenario=` when your SQL already filters down to a single scenario —
for example, when reading from a view or table that contains only one
scenario's data. The engine will not inject a scenario WHERE clause; the
`scenario=` field is just a label.

```python
fpa.Measure(
    name="Actual Revenue",
    sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_type = 'Income'",
    value_col="amount", date_col="date", agg_type=fpa.AggType.SUM,
    scenario="Actual",   # label only — no WHERE injected by the engine
)

fpa.Measure(
    name="Budget Revenue",
    sql="SELECT * FROM gl WHERE scenario = 'Budget' AND account_type = 'Income'",
    value_col="amount", date_col="date", agg_type=fpa.AggType.SUM,
    scenario="Budget",
)

# Both can coexist in the same build_table call regardless of call scenario
calc.build_table(["Actual Revenue", "Budget Revenue"], months, scenario="Actual")
```

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()
registry.register(revenue)                   # one measure
registry.register_many([rev, cogs, gp, gm])  # many at once

measure  = registry.get("Revenue")           # → Measure (KeyError if missing)
names    = registry.names()                  # → List[str]
all_m    = registry.all_measures()           # → List[Measure]
sql_m    = registry.sql_measures()           # → List[Measure] — those with sql set
formula  = registry.formula_measures()       # → List[Measure] — those with formula set
tagged   = registry.by_tag("income_statement")  # → List[Measure]

"Revenue" in registry                        # → bool
len(registry)                                # → int
```

---

## Calculator

```python
import duckdb

con = duckdb.connect("warehouse.duckdb")
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

calc = fpa.Calculator(
    registry,
    connection=con,          # open duckdb.DuckDBPyConnection
    date_col="date",         # default date column (overridden per-measure by Measure.date_col)
    scenario_col="scenario", # default scenario column (overridden per-measure by Measure.scenario_col)
    calendar=calendar,       # required for time-shifted lookups in formula measures
)
```

### Resolve a single value

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",
)
value = calc.resolve("Revenue", ctx)   # → float (memoized)
```

### build_table — measures × periods

Returns a `pd.DataFrame` with measures as rows and period labels as columns.

```python
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,
    scenario="Actual",
    entity="North",              # optional scalar filter → WHERE "entity" = ?
    department=["Sales", "Mktg"] # optional IN filter → WHERE "department" IN (?, ?)
)
# table.loc["Revenue", "Jan 2024"]  → float
```

### build_breakdown_table — dimension value(s) × periods

Returns a `pd.DataFrame` with dimension value(s) as rows and period labels as
columns.  The `dimensions` argument accepts a single column name (string) or a
list of column names for multi-dimension grouping.

#### Single dimension

```python
# Without dimension_values — DuckDB enumerates every group found in the data.
# Safe for high-cardinality dimensions (100K+ distinct values, same one query).
breakdown = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimensions="department",
)
# breakdown.loc["Engineering", "Jan 2024"]  → float

# With explicit dimension_values — output is restricted and reindexed to those
# values, in the order supplied.  Combinations absent from the data return 0.0.
breakdown = calc.build_breakdown_table(
    measure_name="Gross Profit",
    periods=months,
    scenario="Actual",
    dimensions="department",
    dimension_values=["Engineering", "Sales", "Marketing"],
)
# breakdown.loc["Engineering", "Jan 2024"]  → float
# Missing dimension values return 0.0 (no KeyError)
```

#### Multiple dimensions

Pass a list of column names to `dimensions`.  The result has a pandas
`MultiIndex` on the rows, with one level per dimension.

```python
# Without dimension_values — DuckDB returns every (entity, department) pair.
breakdown = calc.build_breakdown_table(
    measure_name="Expense",
    periods=months,
    scenario="Actual",
    dimensions=["entity", "department"],
)
# breakdown.index is a MultiIndex with names ["entity", "department"]
# breakdown.loc[("North", "Sales"), "Jan 2024"]  → float

# With explicit dimension_values — list of tuples, one per row.
# Absent combinations return 0.0.
breakdown = calc.build_breakdown_table(
    measure_name="Expense",
    periods=months,
    scenario="Actual",
    dimensions=["entity", "department"],
    dimension_values=[
        ("North", "Sales"),
        ("North", "Marketing"),
        ("South", "Sales"),
    ],
)
# breakdown.loc[("North", "Sales"), "Jan 2024"]  → float
```

#### Fixed filters alongside a dimension breakdown

Extra keyword arguments are applied as fixed `WHERE` filters before the GROUP
BY — they narrow the data but do not become row labels.

```python
breakdown = calc.build_breakdown_table(
    "Revenue", months, scenario="Actual",
    dimensions="entity",
    region="West",                  # WHERE "region" = ?
    account_id=["4000", "4010"],    # WHERE "account_id" IN (?, ?)
)
```

#### dimension_values: when to use it

`dimension_values` serves two purposes:

1. **Python path (resolver-only measures, no DuckDB connection):** required.
   The library cannot enumerate dimension combinations without a database.
   For multiple dimensions pass a list of tuples.

2. **DuckDB path:** optional.  Omit it to get every group DuckDB finds
   (efficient for high cardinality).  Supply it when you need a fixed set of
   rows in a specific order, or to include combinations that may have no data
   (they will appear as 0.0).

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
    entity="North",            # any keyword args become filters
    department=["Sales", "Mktg"],  # list values → tuple, generates IN clause
)

# Inside a resolver
ctx.period    # Period object
ctx.scenario  # str
ctx.get("entity")         # → "North"
ctx.get("department")     # → ("Sales", "Mktg")  — lists stored as tuples
ctx.get("missing", 0)     # → 0
ctx.filters               # tuple of sorted (key, value) pairs — use .get()
```

---

## Generated SQL

For a CTE-chained query the engine builds a `WITH` clause walking up the
`measure.<name>` reference graph, then selects `FILTER` aggregations per
period from the terminal measure.

**build_table (no breakdown):**

```sql
-- build_table(["S&M Expense"], months, scenario="Actual", entity="North")

WITH "Expense" AS (
    SELECT * FROM general_ledger WHERE account_id IN ('6000','6010')
),
"S&M Expense" AS (
    SELECT * FROM "Expense" WHERE department IN ('Sales', 'Marketing')
)
SELECT
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024",
    ...
FROM "S&M Expense"
WHERE "scenario" = ?
  AND "entity" = ?
```

**build_breakdown_table — single dimension:**

```sql
-- build_breakdown_table("S&M Expense", months, scenario="Actual", dimensions="entity")

WITH "Expense" AS ( ... ),
"S&M Expense" AS ( ... )
SELECT "entity",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024",
    ...
FROM "S&M Expense"
WHERE "scenario" = ?
GROUP BY "entity"
```

With `dimension_values=["North", "South"]` an additional `WHERE` clause is
injected before the GROUP BY:

```sql
WHERE "scenario" = ?
  AND "entity" IN (?, ?)
GROUP BY "entity"
```

**build_breakdown_table — multiple dimensions:**

```sql
-- build_breakdown_table("Expense", months, scenario="Actual",
--                        dimensions=["entity", "department"])

WITH "Expense" AS ( ... )
SELECT "entity", "department",
    COALESCE(SUM(amount) FILTER (WHERE date BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    ...
FROM "Expense"
WHERE "scenario" = ?
GROUP BY "entity", "department"
```

With `dimension_values=[("North", "Sales"), ("South", "Marketing")]` DuckDB's
row-value IN syntax is used:

```sql
WHERE "scenario" = ?
  AND ("entity", "department") IN ((?, ?), (?, ?))
GROUP BY "entity", "department"
```

**CUMULATIVE_END** uses `<=` instead of `BETWEEN`:

```sql
COALESCE(SUM(amount) FILTER (WHERE date <= '2024-01-31'), 0.0) AS "Jan 2024"
```

**CUMULATIVE_START** uses `<`:

```sql
COALESCE(SUM(amount) FILTER (WHERE date < '2024-01-01'), 0.0) AS "Jan 2024"
```

**LAST_DAY** uses `arg_max`:

```sql
COALESCE(arg_max(headcount, snapshot_date) FILTER (WHERE snapshot_date BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024"
```

Period start/end dates are embedded as ISO literals (they come from
`FiscalCalendar` — not user input). Scenario, filter values, and dimension
values are always parameterized (`?`).

---

## Time-Shifted Measures

`Measure` formulas can look up dependency values from a different period using
tuple indexing on `v`. This requires passing `calendar=` to `Calculator`.

### Indexing syntax

```python
v["Revenue"]                          # current period (plain lookup)
v["Revenue", -12]                     # 12 months prior, same grain
v["Revenue", -3]                      # 3 months prior
v["Revenue", -1]                      # 1 month prior
v["Revenue", 0,  fpa.Grain.MONTH]    # first month of current period (cross-grain)
v["Revenue", 1,  fpa.Grain.MONTH]    # second month of current period
v["Revenue", -1, fpa.Grain.MONTH]    # month before current period starts
```

The offset is always in **months**, applied to `period.start`. Grain is
preserved unless you supply a third element.

### Common growth measures

```python
fpa.Measure(
    name="Revenue YoY %",
    dependencies=["Revenue"],
    formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100
                      if v["Revenue", -12] else 0.0,
)

fpa.Measure(
    name="Revenue MoM %",
    dependencies=["Revenue"],
    formula=lambda v: (v["Revenue"] / v["Revenue", -1] - 1) * 100
                      if v["Revenue", -1] else 0.0,
)
```

---

## Python Resolver Patterns

Provide a `resolver` alongside `sql` if you need the library to work without
a DuckDB connection.

```python
def query_revenue(ctx):
    return float(db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM general_ledger
        WHERE account_type = 'Income'
          AND scenario = ?
          AND period_enddate BETWEEN ? AND ?
    """, [ctx.scenario, ctx.period.start, ctx.period.end]).scalar())

fpa.Measure(
    name="Revenue",
    sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
    value_col="amount",
    date_col="period_enddate",
    agg_type=fpa.AggType.SUM,
    scenario_col="scenario",
    resolver=query_revenue,   # used when no DuckDB connection
)
```

---

## Fallback Rules

| Condition                                           | Path used                         |
| --------------------------------------------------- | --------------------------------- |
| No `connection` on Calculator                       | Python resolver                   |
| `connection` set, no SQL measure in chain           | Python resolver                   |
| `connection` set, at least one SQL measure in chain | DuckDB                            |
| Measure has both `sql` and `resolver`               | DuckDB (sql wins with connection) |

---

## What This Library Does NOT Do

- Connect to databases on the Python path — that's the resolver's job
- Cache source data between runs — only computed values are memoized
- Aggregate across grains — `period.start` / `period.end` define the range
- Produce reports, charts, or exports
- Handle forecasting, driver-based projections, or statistical models
