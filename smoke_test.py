"""
Smoke test — runs end-to-end through the full DuckDB-centric pipeline.

Loads sample GL and headcount CSVs into an in-memory DuckDB database, defines
measures using SQL filter queries, and exercises build_table,
build_breakdown_table (with and without explicit dimension_values), and the
Python resolver fallback path.

Run from the project root:
  python smoke_test.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import duckdb
import pandas as pd
from datetime import date
from pathlib import Path
import runpy
import fpa

# --- 1. Generate sample data if needed ---
sample_path = Path("sample_data/sample_gl.csv")
if not sample_path.exists():
    print("Generating sample GL data...")
    runpy.run_path("sample_data/generate_sample_gl.py", run_name="__main__")

employee_path = Path("sample_data/sample_employees.csv")
if not employee_path.exists():
    print("Generating sample employee data...")
    runpy.run_path("sample_data/generate_sample_employees.py", run_name="__main__")

# --- 2. Load data into DuckDB ---
con = duckdb.connect()

con.execute(f"""
    CREATE TABLE gl AS
    SELECT * FROM read_csv_auto('{sample_path}')
""")
con.execute(f"""
    CREATE TABLE employees AS
    SELECT * FROM read_csv_auto('{employee_path}')
""")

gl_rows = con.execute("SELECT COUNT(*) FROM gl").fetchone()[0]
emp_rows = con.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
print(f"Loaded {gl_rows:,} GL rows and {emp_rows:,} employee rows into DuckDB.")

# Inspect columns so the measure definitions reference the right names
gl_cols = [r[0] for r in con.execute("DESCRIBE gl").fetchall()]
print(f"GL columns: {gl_cols}")

# --- 3. Define measures ---
registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.Measure(
        name="Revenue",
        # Revenue accounts use credit convention (negative amounts in GL).
        # Negate in the subquery so SUM returns a positive revenue figure.
        sql="""
            SELECT scenario, account_id, date, entity, description, source,
                   -amount AS amount
            FROM gl WHERE account_id IN ('4000', '4010')
        """,
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="COGS",
        sql="SELECT * FROM gl WHERE account_id IN ('5000', '5010')",
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="OpEx",
        sql="SELECT * FROM gl WHERE account_id IN ('6000','6010','6020','6030','6040')",
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="InterestExpense",
        sql="SELECT * FROM gl WHERE account_id = '7000'",
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="Gross Profit",
        dependencies=["Revenue", "COGS"],
        formula=lambda v: v["Revenue"] - v["COGS"],
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="Gross Margin %",
        dependencies=["Gross Profit", "Revenue"],
        formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0,
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="Operating Income",
        dependencies=["Gross Profit", "OpEx"],
        formula=lambda v: v["Gross Profit"] - v["OpEx"],
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="Net Income",
        dependencies=["Operating Income", "InterestExpense"],
        formula=lambda v: v["Operating Income"] - v["InterestExpense"],
        tags=["income_statement"],
    ),
    fpa.Measure(
        name="Revenue YoY %",
        dependencies=["Revenue"],
        formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100 if v["Revenue", -12] else 0.0,
        tags=["income_statement"],
    ),
])

# --- 4. Set up calendar and calculator ---
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)
calc = fpa.Calculator(registry, connection=con, calendar=calendar)

fy2024_months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

measure_names = [
    "Revenue", "COGS", "Gross Profit", "Gross Margin %",
    "OpEx", "Operating Income", "Net Income",
]

# --- 5. P&L summary table ---
print("\n--- FY2024 Actuals (DuckDB path, no dimension) ---")
actuals = calc.build_table(measure_names, fy2024_months, scenario="Actual")

def fmt(val, name):
    if "%" in name:
        return f"{val:>10.1f}%"
    return f"{val:>12,.0f}"

header = f"{'Measure':<22}" + "".join(f"{p.label:>12}" for p in fy2024_months)
print(header)
print("-" * len(header))
for name in measure_names:
    row = f"{name:<22}" + "".join(fmt(actuals.loc[name, p.label], name) for p in fy2024_months)
    print(row)

# --- 6. YoY Revenue Growth (FY2025 vs FY2024) ---
print("\n--- FY2025 Budget Revenue YoY % vs FY2024 Budget (Python resolver path) ---")
fy2025_months = calendar.periods_for_fiscal_year(2025, fpa.Grain.MONTH)
yoy_table = calc.build_table(["Revenue", "Revenue YoY %"], fy2025_months, scenario="Budget")

yoy_header = f"{'Measure':<22}" + "".join(f"{p.label:>12}" for p in fy2025_months)
print(yoy_header)
print("-" * len(yoy_header))
for name in ["Revenue", "Revenue YoY %"]:
    row = f"{name:<22}" + "".join(fmt(yoy_table.loc[name, p.label], name) for p in fy2025_months)
    print(row)

# --- 7. Revenue breakdown by entity — explicit dimension_values ---
print("\n--- FY2024 Revenue by Entity (explicit dimension_values) ---")
entities = ["North", "South", "West"]
rev_breakdown = calc.build_breakdown_table(
    "Revenue", fy2024_months, scenario="Actual",
    dimension="entity", dimension_values=entities,
)

header = f"{'Entity':<10}" + "".join(f"{p.label:>12}" for p in fy2024_months)
print(header)
print("-" * len(header))
for entity in entities:
    row = f"{entity:<10}" + "".join(fmt(rev_breakdown.loc[entity, p.label], "") for p in fy2024_months)
    print(row)

# --- 8. Gross Margin breakdown — all entities, no dimension_values ---
print("\n--- FY2024 Q1 Gross Margin % by Entity (all groups, no dimension_values) ---")
q1_periods = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)[:3]
gm_breakdown = calc.build_breakdown_table(
    "Gross Margin %", q1_periods, scenario="Actual",
    dimension="entity",
    # dimension_values omitted — DuckDB returns every entity in the data
)

header = f"{'Entity':<10}" + "".join(f"{p.label:>12}" for p in q1_periods)
print(header)
print("-" * len(header))
for entity in gm_breakdown.index:
    row = f"{str(entity):<10}" + "".join(fmt(gm_breakdown.loc[entity, p.label], "%") for p in q1_periods)
    print(row)

# --- 9. Verify Python resolver fallback path ---
print("\n--- Python resolver fallback (no DuckDB connection) ---")
py_registry = fpa.MeasureRegistry()
py_registry.register_many([
    fpa.Measure(name="Revenue",  resolver=lambda ctx: 850_000.0),
    fpa.Measure(name="COGS",     resolver=lambda ctx: 340_000.0),
    fpa.Measure(name="Gross Profit", dependencies=["Revenue", "COGS"],
                formula=lambda v: v["Revenue"] - v["COGS"]),
])
py_calc = fpa.Calculator(py_registry)  # no connection
jan = calendar.month_period(date(2024, 1, 1))
ctx = fpa.CalculationContext.make(period=jan, scenario="Actual")
print(f"  Revenue:      {py_calc.resolve('Revenue', ctx):>12,.0f}")
print(f"  Gross Profit: {py_calc.resolve('Gross Profit', ctx):>12,.0f}")

print("\nSmoke test passed.")
