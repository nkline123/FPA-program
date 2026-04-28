# FPA — Financial Planning & Analysis Library

A DuckDB-centric Python library for resolving financial measures across time
periods and scenarios.

Measures are defined as SQL queries or Python formulas. SQL measures compose
on top of each other via `measure.<name>` references — the engine builds a
CTE chain so parent measures are available without re-scanning source tables.
The engine injects date and dimension filters automatically and executes one
CTE-chained query per SQL measure covering all periods at once. Scenario
filtering is applied via `scenario_col` (engine injects the WHERE clause) or
handled in the SQL itself when `scenario=` is set.

## Documentation

| File                           | What's in it                                 |
| ------------------------------ | -------------------------------------------- |
| [OVERVIEW.md](OVERVIEW.md)     | Concepts, design philosophy, execution paths |
| [USAGE.md](USAGE.md)           | Full API reference with examples             |
| [REFERENCE.md](REFERENCE.md)   | Terse cheatsheet for quick lookup            |
| [smoke_test.py](smoke_test.py) | Working end-to-end example with sample data  |

## Install

```bash
pip install git+https://github.com/nkline123/FPA-program.git
pip install duckdb
```

Core dependencies (installed automatically):

```
pandas
python-dateutil
networkx
```

## Quickstart

```python
import fpa
import duckdb

con = duckdb.connect("warehouse.duckdb")
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

registry = fpa.MeasureRegistry()
registry.register_many([
    # Leaf SQL measure — reads directly from a table
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
    # Composed SQL measure — filters on top of Revenue without re-scanning gl
    fpa.Measure(
        name="North Revenue",
        sql="SELECT * FROM measure.Revenue WHERE entity = 'North'",
        # value_col / date_col / agg_type / scenario_col inherited from Revenue
    ),
    # Python formula measures — calculated from resolved values
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

calc = fpa.Calculator(registry, connection=con)
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

# P&L table — measures as rows, months as columns
table = calc.build_table(
    ["Revenue", "North Revenue", "Gross Profit", "Gross Margin %"],
    months,
    scenario="Actual",
)

# Dimension breakdown — GROUP BY entity, all periods in one query
by_entity = calc.build_breakdown_table(
    "Gross Profit",
    months,
    scenario="Actual",
    dimensions="entity",
)

# Multi-dimension breakdown — GROUP BY entity AND department
by_entity_dept = calc.build_breakdown_table(
    "Gross Profit",
    months,
    scenario="Actual",
    dimensions=["entity", "department"],  # returns a MultiIndex DataFrame
)
```

## How it works

For each leaf SQL measure, the engine builds a CTE chain. Composed measures
reference parent CTEs via `"MeasureName"` identifiers — all in one `WITH`
clause. One query covers every requested period via `FILTER` aggregations:

```sql
WITH "Revenue" AS (
    SELECT * FROM general_ledger WHERE account_type = 'Income'
),
"North Revenue" AS (
    SELECT * FROM "Revenue" WHERE entity = 'North'
)
SELECT
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024",
    ...
FROM "North Revenue"
WHERE "scenario" = ?
```

## Running Tests

```bash
python -m pytest tests/ -v
```
