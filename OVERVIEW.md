# FPA — Concepts and Design

A DuckDB-centric Python calculation engine for financial measures across time
periods and scenarios.  The library handles fiscal calendars, measure
dependency graphs, scenario awareness, dimensional filtering, memoization, and
high-throughput SQL execution for both summary tables and dimension breakdowns.

---

## What This Library Does

- Maintains a **registry** of measure definitions
- Resolves measure values in **dependency order** (via a directed acyclic graph)
- Scopes every resolution to a **period + scenario + optional filters**
- **Memoizes** results so repeated calls are free
- Produces **pandas DataFrames** for P&L tables and dimension breakdowns
- Routes SQL measure resolution through **DuckDB**: builds CTE-chained queries
  wrapping each measure's SQL, generates `FILTER (WHERE date_col …)` per
  period, and executes one query per terminal SQL measure — with or without
  `GROUP BY`
- Supports **SQL composition** via `measure.<name>` references: a measure's SQL
  can query another measure's result set without re-scanning source tables
- Supports **time-shifted lookups** in formula measures: `v["Revenue", -12]`
  fetches a dependency from a different period directly inside the formula,
  enabling YoY, QoQ, and lag-based measures without external glue code

## What This Library Does NOT Do

- Produce reports, charts, or exports
- Handle forecasting or driver-based projections
- Aggregate across grains automatically — the period's `start`/`end` dates
  define the exact range; the SQL `FILTER` clause enforces it

These belong in a layer built on top of this library.

---

## Core Concepts

### Fiscal Calendar

Configure once with the month your fiscal year starts.  The calendar converts
dates into typed `Period` objects and supports period navigation.

```python
# Calendar year (Jan–Dec)
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# Fiscal year starting July, labelled by the year it ends
calendar = fpa.FiscalCalendar(fiscal_year_start_month=7, year_label_convention="ending")
# Jul 2024–Jun 2025 → "FY2025"
```

From any period you can navigate to the prior period, the same period last
fiscal year, YTD months, and rolling windows.

Periods come in three grains: **Month**, **Quarter**, and **Year**.  Monthly
and quarterly periods can be mixed in the same `build_table` or
`build_breakdown_table` call — each period gets its own `FILTER` clause.

### Periods

A `Period` is a frozen, hashable dataclass representing a time slice.

```python
period.start             # date — first day (inclusive)
period.end               # date — last day (inclusive)
period.label             # "Jan 2024", "FY2024 Q1", "FY2024"
period.grain             # Grain.MONTH / QUARTER / YEAR
period.fiscal_year       # 2024
period.fiscal_period_num # 1–12 (month), 1–4 (quarter), 1 (year)
```

The engine embeds `period.start` and `period.end` as ISO date literals in the
`FILTER` clause of each generated SQL column.  They come from `FiscalCalendar`
code — not user input — so embedding them as literals is safe.

### Measures

There is a single `Measure` class with three execution paths determined by
which fields are set.

**Leaf SQL measure** — reads directly from a source table.  You write the
business-logic filter; the engine appends date range, scenario, and any extra
dimension filters automatically.

```python
fpa.Measure(
    name="Revenue",
    sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
    value_col="amount",        # column to aggregate
    date_col="period_enddate", # column to filter by date range
    agg_type=fpa.AggType.SUM,
)
```

Every column in the SQL result other than `value_col` and `date_col` is a
potential dimension — it can be used as a `GROUP BY` target in
`build_breakdown_table` without any additional configuration.

**Composed SQL measure** — filters or joins on top of another measure using
`measure.<name>` syntax.  The engine builds a CTE chain so the parent's data
is available without re-scanning the source table.  `value_col`, `date_col`,
`agg_type`, and `scenario_col` are inherited from the nearest SQL ancestor
that defines them; override on the composed measure only when the aggregation
genuinely changes.

> **Constraint:** Measure names used in `measure.<name>` SQL references must
> contain only word characters (`[a-zA-Z0-9_]`).  Names with spaces or special
> characters can be registered and used as formula `dependencies`, but cannot be
> composed via SQL.

```python
fpa.Measure(
    name="Sales & Marketing",
    sql="SELECT * FROM measure.Expense WHERE department IN ('Sales', 'Marketing')",
    # value_col / date_col / agg_type / scenario_col inherited from Expense
)
```

**Python formula measure** — calculated from other resolved measures.

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],
    formula=lambda v: v["Revenue"] - v["COGS"],
)
```

The `v` argument is a `MeasureValues` object.  Plain string keys return the
current period value.  Tuple keys shift to a different period:

```python
v["Revenue", -12]                    # Revenue 12 months prior, same grain
v["Revenue", 0, fpa.Grain.MONTH]    # Revenue for the first month of the current period
v.period                             # the Period being resolved
```

Measures can depend on other measures to any depth.  The library resolves them
in topological order and raises `ValueError` at construction if a cycle is
detected.

### AggType

Controls how `value_col` is aggregated within a period's date range.

| AggType | SQL generated | Examples |
|---|---|---|
| `SUM` | `COALESCE(SUM(value_col) FILTER (WHERE date BETWEEN start AND end), 0)` | Revenue, Expenses |
| `AVERAGE` | `COALESCE(AVG(value_col) FILTER (WHERE …), 0)` | Average Price |
| `LAST_DAY` | `arg_max(value_col, date_col) FILTER (WHERE …)` | Headcount, ARR snapshot |
| `CUMULATIVE_END` | `COALESCE(SUM(value_col) FILTER (WHERE date <= period_end), 0)` | Balance sheet closing |
| `CUMULATIVE_START` | `COALESCE(SUM(value_col) FILTER (WHERE date < period_start), 0)` | Balance sheet opening |
| `CALCULATED` | n/a — derived from other measures | Gross Margin %, Growth Rate |

`CUMULATIVE_END` and `CUMULATIVE_START` are for balance sheet accounts sourced
from a GL transaction table.  They accumulate all matching transactions from
the beginning of history to the period boundary, rather than scoping to the
period window.  The invariant `CUMULATIVE_END - CUMULATIVE_START == SUM` holds
for any period.

`LAST_DAY` uses DuckDB's `arg_max` aggregate to return `value_col` from the
row with the latest `date_col` within the period — correct for stock measures
(headcount, ARR) where you want the period-end snapshot.

### Scenario Filtering

The engine always injects `WHERE "scenario_col" = ?` into every SQL query
using the scenario passed to `build_table` / `build_breakdown_table`.

**Measure-level scenario** — set `scenario="Actual"` directly on a `Measure`
to lock it to a specific scenario regardless of what the caller passes:

```python
fpa.Measure(
    name="Actual Revenue",
    sql="SELECT * FROM gl WHERE account_type = 'Income'",
    value_col="amount", date_col="date", agg_type=fpa.AggType.SUM,
    scenario="Actual",   # always filters to Actual; caller's scenario is ignored
)
```

This lets Actual and Budget measures coexist in the same `build_table` call.

**Custom scenario column** — set `scenario_col` on a `Measure` when the column
is not named `"scenario"`:

```python
fpa.Measure(
    name="Revenue",
    sql="SELECT * FROM gl",
    value_col="amount", date_col="date", agg_type=fpa.AggType.SUM,
    scenario_col="version",   # queries WHERE "version" = ?
)
```

Composed measures inherit `scenario_col` from their SQL ancestor.

### Calculation Context

Every Python-path resolution is scoped to a `CalculationContext`:

- **period** — the time period being resolved
- **scenario** — the data version ("Actual", "Budget", "Forecast", etc.)
- **filters** — arbitrary key/value pairs for slicing data

Contexts are frozen dataclasses, hashable, and used as memo cache keys.  On
the DuckDB path, scenario and filters become parameterized `WHERE` clauses;
on the Python path they are passed to the resolver callable.

List filter values are normalised to tuples for hashability and generate
`IN (?, ?, …)` clauses on the DuckDB path:

```python
calc.build_table(["Revenue"], periods, scenario="Actual", entity=["North", "South"])
# → WHERE "scenario" = ? AND "entity" IN (?, ?)
```

### Memoization

Each `(measure_name, CalculationContext)` pair is computed once per
`Calculator` instance on the Python path.  Call `calc.clear_cache()` if the
underlying data changes.  The DuckDB path does not use the memo cache (the
query itself is the source of truth).

---

## Execution Paths

### DuckDB path (primary)

Active when `connection` is provided to `Calculator` and at least one measure
in the dependency chain declares a `sql` query.

For each terminal SQL measure the engine:

1. Walks up the `measure.<name>` reference graph to collect all SQL ancestors
2. Builds a `WITH` clause — one CTE per ancestor in evaluation order
3. Replaces `measure.<name>` references with quoted CTE identifiers
4. Generates `FILTER (WHERE …)` aggregation columns — one per period
5. Appends `WHERE`, `GROUP BY` for scenario, extra filters, and dimension

```sql
WITH "Expense" AS (
    SELECT * FROM gl WHERE account_id IN ('6000','6010')
),
"S&M Expense" AS (
    SELECT * FROM "Expense" WHERE department IN ('Sales', 'Marketing')
)
SELECT
    COALESCE(SUM(amount) FILTER (WHERE date BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    ...
FROM "S&M Expense"
WHERE "scenario" = ?
```

One query is executed per terminal SQL measure — measures that are only
referenced via `measure.<name>` by another SQL measure are covered by that
measure's CTE chain and not queried separately.

Python formula measures are then computed as vectorized pandas operations on
the returned DataFrame.

#### High-cardinality dimensions

When `dimension_values=None`, there is no `IN (?, …)` clause — DuckDB groups
and returns all distinct values natively.  A dimension with 200K distinct
values is handled in the same single-scan query as one with 10.  Pass
`dimension_values` only when you want to restrict the output to a known subset.

### Python path (fallback)

Active when no `connection` is provided, or when no measure in the dependency
chain has a `sql` query.

Requires `resolver` on every SQL-less measure.  Each resolver is called once
per `(measure, period, dimension_value)` cell and its result is memoized.
Useful for unit testing without a database.

`build_breakdown_table` on the Python path requires explicit `dimension_values`
— the library cannot enumerate them without a database.

---

## Data Flow

```
FiscalCalendar  ──► Period objects (hashable, grain-aware)
                         │
                         ▼
     CalculationContext (period + scenario + filters)
                         │
              ┌──────────┴──────────────────────────────┐
              │ Python path                              │ DuckDB path
              │ (no connection / resolver-only measures) │ (connection + sql measures)
              │                                          │
              ▼                                          ▼
  Measure.resolver(ctx) → float          CTE-chained query per terminal SQL measure
              │                          (WITH ancestors + FILTER per period
              │                           + optional GROUP BY dimension)
              ▼                                          │
  Measure.formula({dep: float}) → float                 ▼
              │                          Vectorized pandas for formula measures
              └──────────────┬────────────────────────────┘
                             │
                             ▼
              pd.DataFrame (measures × periods  OR  dimension values × periods)
```

---

## Design Decisions

**One class for all measures.**  A single `Measure` class covers leaf SQL,
composed SQL, Python formula, and resolver-only measures.  The execution path
is determined by which fields are set (`sql`, `formula`, `resolver`), not by
the class.

**SQL is the primary data interface.**  Leaf measures express business logic
as SQL filter queries.  The engine handles the mechanical parts (date range,
scenario, dimension grouping) so measures stay focused on meaning, not
plumbing.

**SQL composition via CTEs.**  A composed measure's SQL can reference another
measure by name (`measure.X`).  The engine builds a `WITH` chain so the parent
data is available without re-scanning the source table.  This mirrors how
analysts naturally write multi-step SQL.

**Every column is a potential dimension.**  Because the SQL uses `SELECT *`,
any column in the source table — `entity`, `department`, `cost_center`,
`customer_id` — can be used as a breakdown axis without changing the measure.

**Metadata inheritance.**  `value_col`, `date_col`, `agg_type`, and
`scenario_col` flow down the SQL ancestor chain.  A composed measure only
needs to declare the fields that genuinely change.

**Period dates are SQL literals; everything else is parameterized.**  Dates
come from `FiscalCalendar` code, not user input, so embedding them as ISO
literals is safe.  Scenario, filter values, and dimension values are always
bound as `?` parameters.

**Circular dependencies fail at construction time.**  The DAG is validated
when `Calculator` is instantiated, not lazily.
