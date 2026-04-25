# FPA — Financial Planning & Analysis Library

A Python library for resolving financial measures across time periods and scenarios.

It handles fiscal calendars, measure dependency graphs, scenario awareness, dimensional
filtering, memoization, and — when DuckDB is available — high-throughput SQL execution
for dimension breakdowns. Data access for the Python path is left entirely to the caller
via resolver callables.

## Documentation

| File | What's in it |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | Concepts, design philosophy, when to use each feature |
| [USAGE.md](USAGE.md) | Full API reference with examples |
| [AI_CONTEXT.md](AI_CONTEXT.md) | Terse reference for AI assistants and code generation |
| [smoke_test.py](smoke_test.py) | Working end-to-end example with sample data |

## Install

```bash
pip install git+https://github.com/you/fpa.git
```

Core dependencies (installed automatically):

```
pandas
python-dateutil
networkx
```

DuckDB is optional — only needed for the high-throughput SQL path:

```bash
pip install duckdb
```

## Quickstart

```python
import fpa
from datetime import date

# 1. Fiscal calendar
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

# 2. Define measures
registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        resolver=lambda ctx: my_db.sum(accounts=["4000"], start=ctx.period.start,
                                        end=ctx.period.end, scenario=ctx.scenario),
    ),
    fpa.BaseMeasure(
        name="COGS",
        resolver=lambda ctx: my_db.sum(accounts=["5000"], start=ctx.period.start,
                                        end=ctx.period.end, scenario=ctx.scenario),
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

# P&L table — measures as rows, months as columns
table = calc.build_table(
    ["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    months,
    scenario="Actual",
)
print(table)
```

## With DuckDB (fast dimension breakdowns)

Add `sql_expr` to your base measures and pass a connection at construction:

```python
import duckdb

con = duckdb.connect("warehouse.duckdb")

registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        resolver=lambda ctx: 0.0,   # fallback — used by build_table
        sql_expr="SUM(CASE WHEN account_id IN ('4000') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)",
    ),
    ...
])

calc = fpa.Calculator(registry, connection=con, table="gl")

# build_breakdown_table uses one SQL query for all periods × dimension values
by_region = calc.build_breakdown_table(
    "Gross Profit",
    months,
    scenario="Actual",
    dimension="entity",
    dimension_values=["North", "South", "West"],
)
```

See [USAGE.md](USAGE.md) for the full DuckDB API and resolver patterns.

## Running Tests

```bash
python -m pytest tests/ -v
```

142 tests covering the calendar, measures, DAG, registry, Python resolver path, and
DuckDB execution path.
