# FPA Library — Usage Guide

A Python library for resolving financial measures across time periods and scenarios.
It handles fiscal calendars, measure dependencies, scenario awareness, and memoization.
It does **not** handle data access, forecasting, or reporting — those belong in the layer above.

---

## Quickstart

```python
import fpa
from datetime import date

# 1. Configure the fiscal calendar
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# 2. Define measures
#    BaseMeasure — you provide a resolver that fetches the value from your data source
#    Measure     — calculated from other measures via a formula
registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        resolver=lambda ctx: my_db.get_revenue(ctx.period.start, ctx.period.end, ctx.scenario),
    ),
    fpa.BaseMeasure(
        name="COGS",
        resolver=lambda ctx: my_db.get_cogs(ctx.period.start, ctx.period.end, ctx.scenario),
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

# 3. Calculate
calc = fpa.Calculator(registry)
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    ["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    months,
    scenario="Actual",
)
# Returns a pandas DataFrame — measures as rows, periods as columns
print(table)
```

For a fully working example using sample data, see `smoke_test.py` in the repo root.

---

## Installation

```bash
pip install git+https://github.com/you/fpa.git
```

Dependencies are installed automatically. To install manually:

```bash
pip install pandas python-dateutil networkx
```

```python
import fpa
```

---

## Core Concepts

### The flow

1. Configure a **FiscalCalendar** — tells the library where your fiscal year starts
2. Define **Periods** — the time slices you want to calculate (months, quarters, years)
3. Define **Measures** — what to calculate and how
4. Register measures in a **MeasureRegistry**
5. Create a **Calculator** and call `build_table()` or `build_breakdown_table()`

---

## FiscalCalendar

Configure once at startup.

```python
# Calendar year (Jan–Dec)
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# Fiscal year starting July, labelled by the year it ends
calendar = fpa.FiscalCalendar(fiscal_year_start_month=7, year_label_convention="ending")
# → Jul 2024 – Jun 2025 is labelled FY2025
```

### Getting periods

```python
from datetime import date

# A single period
jan = calendar.month_period(date(2024, 1, 1))   # Period(Jan 2024)
q1  = calendar.quarter_period(date(2024, 1, 1)) # Period(FY2024 Q1)
fy  = calendar.year_period(date(2024, 1, 1))    # Period(FY2024)

# Ranges
months   = calendar.month_range(date(2024, 1, 1), date(2024, 12, 31))  # 12 months
quarters = calendar.quarter_range(date(2024, 1, 1), date(2024, 12, 31)) # 4 quarters

# All periods in a fiscal year
months   = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)
quarters = calendar.periods_for_fiscal_year(2024, fpa.Grain.QUARTER)
year     = calendar.periods_for_fiscal_year(2024, fpa.Grain.YEAR)
```

### Navigating periods

```python
prior_month   = calendar.prior_period(jan)        # Dec 2023
prior_year    = calendar.prior_year_period(jan)    # Jan 2023
ytd           = calendar.ytd_periods(march)        # [Jan, Feb, Mar]
last_12       = calendar.rolling_periods(aug, 12)  # Sep 2023 – Aug 2024
```

### Period attributes

```python
period.start            # date — first day (inclusive)
period.end              # date — last day (inclusive)
period.label            # str  — "Jan 2024", "FY2024 Q1", "FY2024"
period.grain            # Grain.MONTH / QUARTER / YEAR
period.fiscal_year      # int  — e.g. 2024
period.fiscal_period_num # int — month 1-12, quarter 1-4, year always 1
```

---

## Measures

There are two kinds of measures.

### BaseMeasure — fetches a value from a data source

You provide a **resolver**: any callable that accepts a `CalculationContext` and returns a `float`.
The library calls it when it needs a value. How the value is obtained is entirely up to you.

```python
fpa.BaseMeasure(
    name="Revenue",
    resolver=lambda ctx: my_db.query(
        accounts=["4000", "4010"],
        start=ctx.period.start,
        end=ctx.period.end,
        scenario=ctx.scenario,
    ),
    agg_type=fpa.AggType.SUM,   # flow measure — sums across a date range
    tags=["income_statement"],
    description="Total revenue from product and service accounts",
)
```

**AggType** is metadata that tells the layer above how this measure aggregates over time:

| AggType | Meaning | Examples |
|---|---|---|
| `SUM` | Flow — sums over the period | Revenue, Expenses |
| `LAST_DAY` | Stock — point-in-time at period end | Headcount, Cash Balance |
| `AVERAGE` | Rate — average over the period | Average Price |
| `CALCULATED` | Ratio — must always be recalculated | Gross Margin % |

The library itself does not use `agg_type` for aggregation — resolvers are responsible for
interpreting `ctx.period.start` and `ctx.period.end` correctly for the grain requested.

### Measure — calculated from other measures

```python
fpa.Measure(
    name="Gross Profit",
    dependencies=["Revenue", "COGS"],
    formula=lambda v: v["Revenue"] - v["COGS"],
    tags=["income_statement"],
)

fpa.Measure(
    name="Gross Margin %",
    dependencies=["Gross Profit", "Revenue"],
    formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0,
)
```

The formula receives a dict of `{measure_name: float}` for every declared dependency.
Dependencies can themselves depend on other measures — the library resolves them in the
correct order automatically and raises an error if a circular dependency is detected.

---

## MeasureRegistry

```python
registry = fpa.MeasureRegistry()

# Register one at a time
registry.register(revenue_measure)

# Register many at once
registry.register_many([revenue, cogs, gross_profit, gross_margin])

# Look up a measure
measure = registry.get("Revenue")

# Filter by tag
income_statement = registry.by_tag("income_statement")
```

---

## Calculator

```python
calc = fpa.Calculator(registry)
```

### Resolve a single value

```python
from datetime import date

ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 1, 1)),
    scenario="Actual",
)
value = calc.resolve("Revenue", ctx)  # float
```

### Build a table — measures × periods

Returns a pandas DataFrame with measures as rows and period labels as columns.

```python
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

table = calc.build_table(
    measure_names=["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    periods=months,
    scenario="Actual",
)
# DataFrame shape: (4 measures, 12 months)
# Access: table.loc["Revenue", "Jan 2024"]
```

### Filters — slice by any dimension

Pass any keyword arguments to `build_table` as filters. They are forwarded to every
resolver via `ctx.get("key")`. Each resolver reads only the keys it cares about.

```python
# All entities
table = calc.build_table(["Revenue"], months, scenario="Actual")

# North entity only
table = calc.build_table(["Revenue"], months, scenario="Actual", entity="North")

# Multiple filters
table = calc.build_table(["Revenue"], months, scenario="Actual", entity="North", department="Sales")
```

In the resolver:

```python
def query_revenue(ctx):
    entity = ctx.get("entity")        # None if not filtered
    department = ctx.get("department") # None if not filtered
    # apply filters to your data source as needed
```

### Breakdown table — one measure × dimension values × periods

Returns a DataFrame with dimension values as rows and period labels as columns.
You provide the list of dimension values — the library iterates over them, passing
each as a filter to the resolver.

```python
entities = ["North", "South", "West"]

breakdown = calc.build_breakdown_table(
    measure_name="Revenue",
    periods=months,
    scenario="Actual",
    dimension="entity",
    dimension_values=entities,
)
# DataFrame shape: (3 entities, 12 months)
# Access: breakdown.loc["North", "Jan 2024"]
```

Additional fixed filters can be combined with the dimension:

```python
breakdown = calc.build_breakdown_table(
    "Revenue", months, scenario="Actual",
    dimension="customer", dimension_values=customers,
    entity="North",  # fixed filter applied to every cell
)
```

### Memoization

Each `(measure_name, context)` combination is computed only once per Calculator instance.
If multiple measures share a dependency, the shared dependency is resolved once and reused.

```python
calc.clear_cache()  # call if the underlying data changes
```

---

## CalculationContext

The object passed to every resolver. Frozen (hashable) so it can be used as a cache key.

```python
ctx.period    # Period object
ctx.scenario  # str — "Actual", "Budget", etc.
ctx.get("entity")      # filter value, or None if not set
ctx.get("department")  # filter value, or None if not set
```

Build one directly when calling `resolve()`:

```python
ctx = fpa.CalculationContext.make(
    period=calendar.month_period(date(2024, 3, 1)),
    scenario="Budget",
    entity="North",
)
```

---

## Resolver Patterns

### Flow measure (income statement)

Sum transactions within the period range. Works for any grain.

```python
def query_revenue(ctx):
    return db.execute("""
        SELECT SUM(amount) FROM gl
        WHERE account_id IN ('4000', '4010')
        AND scenario = ?
        AND date BETWEEN ? AND ?
    """, ctx.scenario, ctx.period.start, ctx.period.end)
```

### Stock measure (balance sheet / headcount)

Point-in-time at period end. Sum all history through `ctx.period.end`.

```python
def query_total_assets(ctx):
    return db.execute("""
        SELECT SUM(amount) FROM gl
        WHERE account_id IN ('1000', '1010', '1020')
        AND scenario = ?
        AND date <= ?
    """, ctx.scenario, ctx.period.end)
```

### Prior period reference

Call back into the calculator to resolve a measure for a different period.

```python
def query_mom_growth(ctx):
    prior_ctx = fpa.CalculationContext.make(
        period=calendar.prior_period(ctx.period),
        scenario=ctx.scenario,
        **dict(ctx.filters),
    )
    current = calc.resolve("Revenue", ctx)
    prior   = calc.resolve("Revenue", prior_ctx)
    return (current - prior) / prior if prior else 0.0
```

### YTD measure

```python
def query_revenue_ytd(ctx):
    ytd_start = calendar._fiscal_year_start_date(ctx.period.fiscal_year)
    return db.execute("""
        SELECT SUM(amount) FROM gl
        WHERE account_id IN ('4000', '4010')
        AND scenario = ?
        AND date BETWEEN ? AND ?
    """, ctx.scenario, ytd_start, ctx.period.end)
```

### Performance — pre-load data once

For large date ranges, pre-load all data into memory and slice in the resolver
instead of making one database call per cell.

```python
# Load once
data = db.execute("SELECT scenario, account_id, date, amount FROM gl WHERE ...")
lookup = build_lookup(data)  # dict keyed however your resolver needs

# Resolver slices the in-memory lookup — no database call per cell
def query_revenue(ctx):
    return sum(lookup.get((ctx.scenario, acct, ctx.period.start, ctx.period.end), 0.0)
               for acct in ["4000", "4010"])
```

---

## What this library does NOT do

- Connect to databases or execute queries
- Store or cache data between runs
- Aggregate across grains automatically (resolvers handle their own date ranges)
- Enumerate dimension values for breakdowns (the caller provides them)
- Produce reports, charts, or exports
- Handle forecasting or driver-based projections

These belong in the layer built on top of this library.
