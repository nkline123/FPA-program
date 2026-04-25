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
- Routes base measure resolution through **DuckDB**: wraps each measure's SQL
  as a subquery, generates `FILTER (WHERE date_col BETWEEN … AND …)` per
  period, and executes one query per base measure — with or without `GROUP BY`

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

There are two kinds:

**BaseMeasure** — fetches raw values from a DuckDB table via a SQL filter
query.  You write the business-logic WHERE condition; the engine appends the
date range, scenario, and any extra dimension filters automatically.

```python
fpa.BaseMeasure(
    name="Revenue",
    sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
    value_col="amount",       # column to aggregate
    date_col="period_enddate",# column to filter by date range
    agg_type=fpa.AggType.SUM,
)
```

Every column in the SQL result other than `value_col` and `date_col` is a
dimension — it can be used as a `GROUP BY` target in `build_breakdown_table`
without any additional configuration.

**Measure** — calculated from other measures via a formula callable:

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],
    formula=lambda v: v["Revenue"] - v["COGS"],
)
```

Measures can depend on other measures to any depth.  The library resolves them
in topological order and raises `ValueError` at construction if a cycle is
detected.

### AggType

Controls how `value_col` is aggregated within a period's date range.

| AggType | SQL generated | Examples |
|---|---|---|
| `SUM` | `COALESCE(SUM(value_col) FILTER (WHERE …), 0)` | Revenue, Expenses |
| `AVERAGE` | `COALESCE(AVG(value_col) FILTER (WHERE …), 0)` | Average Price |
| `LAST_DAY` | `arg_max(value_col, date_col) FILTER (WHERE …)` | Headcount, ARR, Balance |
| `CALCULATED` | n/a — derived from other measures | Gross Margin %, Growth Rate |

`LAST_DAY` uses DuckDB's `arg_max` aggregate to return `value_col` from the
row with the latest `date_col` within the period — correct for stock measures
(headcount, cash balance) where you want the period-end snapshot.

### Calculation Context

Every Python-path resolution is scoped to a `CalculationContext`:

- **period** — the time period being resolved
- **scenario** — the data version ("Actual", "Budget", "Forecast", etc.)
- **filters** — arbitrary key/value pairs for slicing data

Contexts are frozen dataclasses, hashable, and used as memo cache keys.  On
the DuckDB path, scenario and filters become parameterized `WHERE` clauses;
on the Python path they are passed to the resolver callable.

### Memoization

Each `(measure_name, CalculationContext)` pair is computed once per
`Calculator` instance on the Python path.  Call `calc.clear_cache()` if the
underlying data changes.  The DuckDB path does not use the memo cache (the
query itself is the source of truth).

---

## Execution Paths

### DuckDB path (primary)

Active when `connection` is provided to `Calculator` and at least one base
measure in the dependency chain declares a `sql` query.

For each `BaseMeasure` with `sql`, the engine builds:

```sql
SELECT {dimension},                          -- omitted for build_table
    {agg_func} FILTER (WHERE {date_col} BETWEEN '{start}' AND '{end}') AS "{period.label}",
    ...                                      -- one column per period
FROM ({BaseMeasure.sql}) __base
WHERE {scenario_col} = ?                     -- parameterized
  [AND {extra_filter} = ? ...]
  [AND {dimension} IN (?, …)]               -- omitted if dimension_values is None
GROUP BY {dimension}                         -- omitted for build_table
```

One query per base measure is executed.  Measures with `sql` are fetched via
SQL; measures with only a `resolver` are filled via Python after the query
returns.  Derived measures are then computed as vectorized pandas operations.

#### Why not one query for all base measures?

Different base measures can query different tables or apply different filters,
so they cannot always be combined into a single `SELECT`.  One query per base
measure is still highly efficient: DuckDB scans each filtered subquery once
and returns a wide result covering all periods simultaneously.

#### High-cardinality dimensions

Because there is no `IN (?, …)` clause when `dimension_values=None`, DuckDB
groups and returns all distinct values natively.  A dimension with 200K
distinct values is handled in the same single-scan query as one with 10.
Pass `dimension_values` only when you want to restrict the output to a known
subset.

### Python path (fallback)

Active when no `connection` is provided, or when no base measure in the
dependency chain has a `sql` query.

Requires `resolver` on every `BaseMeasure`.  Each resolver is called once per
`(measure, period, dimension_value)` cell and its result is memoized.  Useful
for unit testing without a database.

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
              │ (no connection / resolver-only measures) │ (connection + sql on measures)
              │                                          │
              ▼                                          ▼
  BaseMeasure.resolver(ctx) → float        One SQL query per BaseMeasure
              │                            (subquery + FILTER per period
              │                             + optional GROUP BY dimension)
              ▼                                          │
  Measure.formula({dep: float}) → float                 ▼
              │                            Vectorized pandas for derived measures
              └──────────────┬────────────────────────────┘
                             │
                             ▼
              pd.DataFrame (measures × periods  OR  dimension values × periods)
```

---

## Design Decisions

**SQL is the primary data interface.**  BaseMeasure expresses the business
logic filter in SQL.  The engine handles the mechanical parts (date range,
scenario, dimension grouping) so measures stay focused on what they mean, not
how to query them.

**Every column is a potential dimension.**  Because the SQL uses `SELECT *`,
any column in the source table — `entity`, `department`, `cost_center`,
`customer_id` — can be used as a breakdown axis without changing the measure
definition.

**Period dates are SQL literals; everything else is parameterized.**  Dates
come from `FiscalCalendar` code, not user input, so embedding them as ISO
literals is safe.  Scenario, filter values, and dimension values are always
bound as `?` parameters.

**AggType drives the SQL aggregation function.**  `SUM`, `AVG`, and `arg_max`
are generated automatically from the measure's `agg_type`.

**Circular dependencies fail at construction time.**  The DAG is validated
when `Calculator` is instantiated, not lazily.

**The Python resolver is optional.**  Provide one if you want the library to
work without a DuckDB connection (e.g. unit tests, offline environments).
