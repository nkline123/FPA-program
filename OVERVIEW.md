# FPA — Financial Planning & Analysis Library

A Python library that resolves financial measures across time periods and scenarios.
It handles the calculation logic — fiscal calendars, measure dependencies, scenario
awareness, and memoization — but does not handle data access. Data access is the
responsibility of the calling code via resolver callables.

This library is intended to be the foundation for a higher-level layer that adds
database connectivity, SQL-based measure definitions, automatic dimension enumeration,
and reporting on top.

---

## What It Does NOT Do

- Connect to databases or execute queries
- Store or cache data between runs
- Aggregate across grains automatically — resolvers handle their own date ranges
- Enumerate dimension values for breakdowns — the caller provides them
- Produce reports, charts, or exports
- Handle forecasting or driver-based projections

---

## Core Concepts

### Fiscal Calendar

Configure once with the month your fiscal year starts. The calendar converts dates
into typed `Period` objects and supports navigation between periods.

From any period you can navigate to:
- The **prior month**, **prior quarter**, or **prior year**
- The **same period in the prior year**
- All months **year-to-date** through a given month
- A **rolling window** of the last N months

Periods come in three grains: **Month**, **Quarter**, and **Year**.

### Measures

There are two kinds of measures:

**BaseMeasure** — fetches a value via a resolver callable you provide. The library
calls `resolver(context)` and expects a float back. How that float is obtained —
database query, API call, in-memory lookup — is entirely up to the caller.

**Measure** — calculated from other measures via a formula. For example:
- Gross Profit = Revenue − COGS
- Gross Margin % = Gross Profit ÷ Revenue

Measures can depend on other measures to any depth. The library resolves them in
the correct order automatically using a DAG (directed acyclic graph) and raises an
error immediately if a circular dependency is detected.

### AggType

Each measure declares how it aggregates across time — this is metadata for the layer
above, not logic enforced by the library. Resolvers are responsible for interpreting
`ctx.period.start` and `ctx.period.end` correctly for the grain requested.

| AggType | Meaning | Examples |
|---|---|---|
| `SUM` | Flow — accumulates over the period | Revenue, Expenses |
| `LAST_DAY` | Stock — point-in-time at period end | Headcount, Cash Balance |
| `AVERAGE` | Rate — average over the period | Average Price |
| `CALCULATED` | Ratio — must always be recalculated | Gross Margin % |

### Calculation Context

Every resolution is scoped to a `CalculationContext` containing:
- **Period** — the time period being resolved
- **Scenario** — the data version (e.g. "Actual", "Budget")
- **Filters** — arbitrary key/value pairs for slicing data (e.g. `entity="North"`,
  `department="Engineering"`). The resolver reads only the keys it cares about.

### Memoization

Each `(measure, context)` combination is computed only once per `Calculator` instance.
Repeated calls return the cached value immediately, making it efficient to resolve
large grids of measures × periods.

---

## Summary of the Flow

```
FiscalCalendar  ──► Period objects
                         │
                         ▼
BaseMeasure resolvers  ──► raw values (fetched by caller)
                         │
                         ▼
Measure formulas  ──► derived values (computed by library, in DAG order)
                         │
                         ▼
Calculator.build_table()           ──► DataFrame (measures × periods)
Calculator.build_breakdown_table() ──► DataFrame (dimension values × periods)
```
