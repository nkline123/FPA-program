# FPA — Concepts and Design

A Python calculation engine for financial measures across time periods and scenarios.
It handles the calculation graph — fiscal calendars, measure dependencies, scenario
awareness, dimensional filtering, and memoization. Data access is the responsibility
of the calling code.

---

## What This Library Does

- Maintains a **registry** of measure definitions
- Resolves measure values in **dependency order** (via a directed acyclic graph)
- Scopes every resolution to a **period + scenario + optional filters**
- **Memoizes** results so repeated calls are free
- Produces **pandas DataFrames** for P&L tables and dimension breakdowns
- Optionally routes high-cardinality dimension breakdowns through **DuckDB** — one SQL
  query covering all periods and dimension values, with derived measures computed as
  vectorized pandas operations

## What This Library Does NOT Do

- Connect to databases or execute queries (on the Python path — the caller provides resolvers)
- Store or cache data between runs (only computed values are memoized, not source data)
- Aggregate across grains automatically — resolvers handle their own date ranges
- Enumerate dimension values for breakdowns — the caller provides them
- Produce reports, charts, or exports
- Handle forecasting or driver-based projections

These belong in a layer built on top of this library.

---

## Core Concepts

### Fiscal Calendar

Configure once with the month your fiscal year starts. The calendar converts dates
into typed `Period` objects and supports navigation between periods.

```python
# Calendar year (Jan–Dec)
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# Fiscal year starting July, labelled by the year it ends
calendar = fpa.FiscalCalendar(fiscal_year_start_month=7, year_label_convention="ending")
# Jul 2024–Jun 2025 → "FY2025"
```

From any period you can navigate to:
- The **prior period** (same grain, one step back)
- The **same period in the prior fiscal year**
- All months **year-to-date** through a given month
- A **rolling window** of the last N months

Periods come in three grains: **Month**, **Quarter**, and **Year**.

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

Resolvers receive `ctx.period.start` and `ctx.period.end` and are responsible for
interpreting them correctly — whether that means summing within the range (revenue),
reading the last value in the range (headcount), or accumulating all history through
the end date (balance sheet).

### Measures

There are two kinds:

**BaseMeasure** — fetches a raw value from your data source. You provide a resolver
callable that accepts a `CalculationContext` and returns a float. The library calls it;
how it gets the float is entirely up to you.

Optionally, a `BaseMeasure` can declare a `sql_expr` — a SQL fragment with `{start}`
and `{end}` placeholders. When the DuckDB path is active, the library uses `sql_expr`
instead of calling the resolver, enabling one query to cover all periods and dimension
values simultaneously.

**Measure** — calculated from other measures via a formula callable. For example:
```
Gross Profit  = Revenue − COGS
Gross Margin  = Gross Profit ÷ Revenue × 100
Net Income    = Gross Profit − OpEx − Interest − Taxes
```

Measures can depend on other measures to any depth. The library resolves them in the
correct order automatically using a dependency graph and raises an error immediately if
a circular dependency is detected.

### AggType

Each measure carries an `agg_type` as metadata for the layer above. The library does
not use it to perform aggregation — that is the resolver's job.

| AggType | Meaning | Examples |
|---|---|---|
| `SUM` | Flow — accumulates over the period | Revenue, Expenses |
| `LAST_DAY` | Stock — point-in-time at period end | Headcount, Cash Balance |
| `AVERAGE` | Rate — average over the period | Average Price, Average Headcount |
| `CALCULATED` | Ratio — must always be recalculated | Gross Margin %, Growth Rate |

### Calculation Context

Every resolution is scoped to a `CalculationContext`:
- **period** — the time period being resolved
- **scenario** — the data version ("Actual", "Budget", "Forecast", etc.)
- **filters** — arbitrary key/value pairs for slicing data (`entity="North"`,
  `department="Engineering"`, `customer_id=42`)

Contexts are frozen dataclasses. They are hashable and used as memo cache keys.
Resolvers read only the filter keys they care about via `ctx.get("key")`.

### Memoization

Each `(measure_name, CalculationContext)` pair is computed once per `Calculator` instance.
If multiple measures share a dependency (e.g. both Gross Margin and Operating Margin
depend on Revenue), Revenue is resolved once and reused. Call `calc.clear_cache()` if
the underlying data changes.

---

## Execution Paths

### Python path (always available)

`build_table` always uses the Python path. For each `(period, measure)` cell:
1. Construct a `CalculationContext`
2. Walk the dependency graph in topological order
3. Call each `BaseMeasure.resolver(ctx)` for leaf nodes
4. Compute each `Measure.formula(values)` for derived nodes
5. Cache the result

This path fires one resolver call per base measure per period. Performance depends
entirely on how fast your resolvers are. For a 4-measure × 12-month P&L, that is at
most 48 calls (often fewer, because shared dependencies are memoized). For resolver
implementations that do a live database call per invocation, pre-loading data into a
dict first is strongly recommended — see [USAGE.md](USAGE.md#performance--pre-load-data-once).

### DuckDB path (opt-in, for breakdowns)

`build_breakdown_table` routes to DuckDB when:
1. A `connection` and `table` were passed to `Calculator`, **and**
2. At least one base measure in the dependency chain declares a `sql_expr`

Instead of one resolver call per cell, the library:
1. Builds one SQL query with all base measures as window expressions across all periods,
   grouped by the breakdown dimension
2. Executes it once — DuckDB scans the table once and returns a wide DataFrame
3. Fills any remaining base measure columns (those without `sql_expr`) via the Python
   resolver
4. Computes all derived measures as vectorized pandas operations

At 10M rows, 50K customers, 60 periods: ~2 seconds vs. ~25 seconds for the Python path.
The crossover point where DuckDB wins is roughly 500+ cells (50 dimension values × 10
periods or equivalent).

---

## Data Flow

```
FiscalCalendar  ──► Period objects (hashable, grain-aware)
                         │
                         ▼
     CalculationContext (period + scenario + filters)
                         │
              ┌──────────┴──────────────────────────┐
              │ Python path                          │ DuckDB path
              │ (build_table, fallback breakdown)    │ (build_breakdown_table)
              │                                      │
              ▼                                      ▼
  BaseMeasure.resolver(ctx) → float      One SQL query (GROUP BY dimension)
              │                          returning all base measures × periods
              ▼                                      │
  Measure.formula({dep: float}) → float             ▼
              │                          Vectorized pandas for derived measures
              └──────────────┬──────────────────────┘
                             │
                             ▼
              pd.DataFrame (measures × periods  OR  dimension values × periods)
```

---

## Design Decisions

**Resolvers own data access.** The library never touches a database on the Python path.
This makes it testable with any data source — a dict, a CSV, a REST API, a database.

**CalculationContext is the single interface between the engine and the outside world.**
Resolvers receive exactly one argument. Adding a new filter dimension requires no
library changes — just pass a new keyword argument and read it with `ctx.get("key")`.

**Filters have no schema.** `entity`, `customer`, `department`, `product`, `region` —
any key/value pair works. Resolvers ignore keys they don't care about.

**AggType is documentation, not behavior.** The library does not know whether a measure
is a flow or a stock. Resolvers decide by how they interpret `ctx.period.start` and
`ctx.period.end`. AggType tells the layer above how to present and aggregate values.

**Memoization is keyed on the frozen context.** Because `CalculationContext` is frozen
and hashable, the same `(measure, context)` pair can never be computed twice in one
session. This makes it safe to call `resolve()` recursively from inside a resolver
(e.g. for prior-period comparisons).

**Circular dependencies fail at construction time.** The DAG is validated when
`Calculator` is instantiated, not lazily. Bad measure definitions are caught immediately.

**DuckDB is opt-in and transparent.** Passing `connection` and `table` to `Calculator`
enables the SQL path for breakdowns. Removing them falls back to the Python path
with no other changes required. `build_table` always uses the Python path regardless
of whether a connection is present — SQL offers no advantage for summary tables with
no GROUP BY dimension.
