"""
Smoke test — runs end-to-end through the core pipeline without any
database dependency. Resolvers return values from a simple in-memory
dict built from the sample CSV.

In real use, resolvers would call into a database layer built on top
of this library.

Run from the project root:
  python smoke_test.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from pathlib import Path
import fpa

# --- 1. Generate and load sample data into memory ---
import runpy

sample_path = Path("sample_data/sample_gl.csv")
if not sample_path.exists():
    print("Generating sample GL data...")
    runpy.run_path("sample_data/generate_sample_gl.py", run_name="__main__")

employee_path = Path("sample_data/sample_employees.csv")
if not employee_path.exists():
    print("Generating sample employee data...")
    runpy.run_path("sample_data/generate_sample_employees.py", run_name="__main__")

df = pd.read_csv(sample_path, dtype={"account_id": str})
df["date"] = pd.to_datetime(df["date"])
print(f"Loaded {len(df):,} GL rows.")

emp = pd.read_csv(employee_path)
emp["start_date"] = pd.to_datetime(emp["start_date"])
emp["end_date"] = pd.to_datetime(emp["end_date"])  # NaT for active employees
print(f"Loaded {len(emp):,} employee rows.")

# --- 2. Build a lookup: (scenario, account_id, "YYYY-MM") → total amount ---
# This simulates what a database layer would do.
df["month"] = df["date"].dt.strftime("%Y-%m")

# Two lookups: one with entity, one without (for all-entity totals)
monthly = (
    df.groupby(["scenario", "account_id", "month"])["amount"]
    .sum()
    .to_dict()
)
monthly_by_entity = (
    df.groupby(["scenario", "account_id", "month", "entity"])["amount"]
    .sum()
    .to_dict()
)

def query_accounts(account_ids, ctx, sign=1):
    """Sum amounts for a list of account IDs for the given context."""
    month_key = ctx.period.start.strftime("%Y-%m")
    entity = ctx.get("entity")
    if entity is not None:
        return sign * sum(
            monthly_by_entity.get((ctx.scenario, acct, month_key, entity), 0.0)
            for acct in account_ids
        )
    return sign * sum(
        monthly.get((ctx.scenario, acct, month_key), 0.0)
        for acct in account_ids
    )

def query_headcount(ctx):
    """Count employees active at period end, optionally filtered by entity."""
    period_end = pd.Timestamp(ctx.period.end)
    period_start = pd.Timestamp(ctx.period.start)
    mask = (
        (emp["start_date"] <= period_end) &
        (emp["end_date"].isna() | (emp["end_date"] >= period_start))
    )
    entity = ctx.get("entity")
    if entity is not None:
        mask &= emp["entity"] == entity
    return float(mask.sum())

# --- 3. Define measures ---
registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.BaseMeasure(
        name="Revenue",
        resolver=lambda ctx: query_accounts(
            ["4000", "4010"], ctx, sign=-1
        ),
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.BaseMeasure(
        name="COGS",
        resolver=lambda ctx: query_accounts(
            ["5000", "5010"], ctx
        ),
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.BaseMeasure(
        name="OpEx",
        resolver=lambda ctx: query_accounts(
            ["6000", "6010", "6020", "6030", "6040"], ctx
        ),
        agg_type=fpa.AggType.SUM,
        tags=["income_statement"],
    ),
    fpa.BaseMeasure(
        name="InterestExpense",
        resolver=lambda ctx: query_accounts(
            ["7000"], ctx
        ),
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
    fpa.BaseMeasure(
        name="Headcount",
        resolver=query_headcount,
        agg_type=fpa.AggType.LAST_DAY,
        tags=["headcount"],
        description="Active employees at end of period",
    ),
    fpa.Measure(
        name="Revenue per Employee",
        dependencies=["Revenue", "Headcount"],
        formula=lambda v: v["Revenue"] / v["Headcount"] if v["Headcount"] else 0.0,
        tags=["headcount"],
    ),
    fpa.Measure(
        name="Cost per Employee",
        dependencies=["OpEx", "Headcount"],
        formula=lambda v: v["OpEx"] / v["Headcount"] if v["Headcount"] else 0.0,
        tags=["headcount"],
    ),
])

# --- 4. Set up calendar and calculator ---
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)
calc = fpa.Calculator(registry)

fy2024_months = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

measure_names = [
    "Revenue", "COGS", "Gross Profit", "Gross Margin %",
    "OpEx", "Operating Income", "Net Income",
]

# --- 5. Calculate and display ---
print("\n--- FY2024 Actuals ---")
actuals = calc.build_table(measure_names, fy2024_months, scenario="Actual")

def fmt(val, name):
    if "%" in name:
        return f"{val:>10.1f}%"
    return f"{val:>12,.0f}"

header = f"{'Measure':<20}" + "".join(f"{p.label:>12}" for p in fy2024_months)
print(header)
print("-" * len(header))
for name in measure_names:
    row = f"{name:<20}" + "".join(fmt(actuals.loc[name, p.label], name) for p in fy2024_months)
    print(row)

print("\n--- FY2024 Headcount ---")
hc_measures = ["Headcount", "Revenue per Employee", "Cost per Employee"]
hc_table = calc.build_table(hc_measures, fy2024_months, scenario="Actual")

header = f"{'Measure':<22}" + "".join(f"{p.label:>12}" for p in fy2024_months)
print(header)
print("-" * len(header))
for name in hc_measures:
    row = f"{name:<22}" + "".join(fmt(hc_table.loc[name, p.label], name) for p in fy2024_months)
    print(row)

print("\n--- FY2024 Headcount by Entity ---")
entities = ["North", "South", "West"]
hc_breakdown = calc.build_breakdown_table(
    "Headcount", fy2024_months, scenario="Actual",
    dimension="entity", dimension_values=entities,
)

header = f"{'Entity':<10}" + "".join(f"{p.label:>12}" for p in fy2024_months)
print(header)
print("-" * len(header))
for entity in entities:
    row = f"{entity:<10}" + "".join(fmt(hc_breakdown.loc[entity, p.label], "") for p in fy2024_months)
    print(row)

print("\n--- FY2024 Revenue by Entity ---")
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

print("\nSmoke test passed.")
