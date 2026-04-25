# FPA — Financial Planning & Analysis Library

A DuckDB-centric Python library for resolving financial measures across time
periods and scenarios.

Base measures are defined as SQL filter queries.  The engine wraps them as
subqueries, appends date / scenario / dimension filters automatically, and
executes one query per base measure — enabling high-cardinality GROUP BY
breakdowns without parameter-list explosion.  Derived measures (ratios,
subtotals) are computed as vectorized pandas operations on the query result.

## Documentation

| File | What's in it |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | Concepts, design philosophy, execution paths |
| [USAGE.md](USAGE.md) | Full API reference with examples |
| [AI_CONTEXT.md](AI_CONTEXT.md) | Terse reference for AI assistants and code generation |
| [smoke_test.py](smoke_test.py) | Working end-to-end example with sample data |

## Install

```bash
pip install git+https://github.com/you/fpa.git
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

# Define measures as SQL filter queries
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

calc = fpa.Calculator(registry, connection=con)
months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

# P&L table — measures as rows, months as columns
# Runs via DuckDB: one query per base measure, all periods in one scan
table = calc.build_table(
    ["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
    months,
    scenario="Actual",
)

# Dimension breakdown — one query per base measure, GROUP BY dimension
# Omit dimension_values to return every group in the data (no IN clause —
# safe for high-cardinality dimensions with 100K+ distinct values)
by_dept = calc.build_breakdown_table(
    "Gross Margin %",
    months,
    scenario="Actual",
    dimension="department",
)
```

## How it works

For each `BaseMeasure`, the engine generates SQL like this — one query
covers all periods via `FILTER (WHERE date_col BETWEEN … AND …)`:

```sql
SELECT department,
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-01-01' AND '2024-01-31'), 0.0) AS "Jan 2024",
    COALESCE(SUM(amount) FILTER (WHERE period_enddate BETWEEN '2024-02-01' AND '2024-02-29'), 0.0) AS "Feb 2024",
    ...
FROM (SELECT * FROM general_ledger WHERE account_type = 'Income') __base
WHERE scenario = ?
GROUP BY department
```

DuckDB's columnar `GROUP BY` handles 100K+ distinct dimension values natively —
no IN-clause explosion, no Python loop over dimension values.

Derived measures (`Gross Profit`, `Gross Margin %`) are computed as vectorized
pandas operations on the returned DataFrame.

## Running Tests

```bash
python -m pytest tests/ -v
```
