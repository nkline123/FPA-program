"""
Head-to-head: Calculator (pre-load dict) vs DuckDBCalculator (one SQL query).

Uses the same measure definitions and data. The only difference is:
  - Calculator:       BaseMeasure resolvers do dict lookups against pre-loaded data
  - DuckDBCalculator: BaseMeasures have sql_expr; one SQL query per build_* call

Run from project root:
  python benchmark_duckdb_calc.py
"""

import sys, os, time, gc
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import duckdb
import fpa
from datetime import date
from dateutil.relativedelta import relativedelta

rng = np.random.default_rng(42)

N_ROWS      = 2_000_000
N_CUSTOMERS = 10_000
ACCOUNTS    = ["4000", "4010", "5000", "5010"]
SCENARIOS   = ["Actual", "Budget"]
MONTHS      = pd.date_range("2023-01", periods=24, freq="MS")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ---------------------------------------------------------------------------
# Generate data
# ---------------------------------------------------------------------------

print("Generating data...")
df = pd.DataFrame({
    "scenario":    rng.choice(SCENARIOS, size=N_ROWS),
    "account_id":  rng.choice(ACCOUNTS,  size=N_ROWS),
    "date":        rng.choice(MONTHS, size=N_ROWS),
    "customer_id": rng.integers(0, N_CUSTOMERS, size=N_ROWS).astype(str),
    "amount":      rng.normal(10_000, 2_000, size=N_ROWS),
})
df["date"] = pd.to_datetime(df["date"]).dt.date

con = duckdb.connect()
con.execute("CREATE TABLE gl AS SELECT * FROM df")
print(f"Loaded {N_ROWS:,} rows into DuckDB.")

calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)
ALL_CUSTOMERS = [str(i) for i in range(N_CUSTOMERS)]

# ---------------------------------------------------------------------------
# Setup: Calculator with pre-loaded dict (current approach)
# ---------------------------------------------------------------------------

section("Setup: pre-loading data for plain Calculator")

t = time.perf_counter()
raw = con.execute("""
    SELECT scenario, account_id,
           strftime(date, '%Y-%m') AS month,
           customer_id, SUM(amount) AS amount
    FROM gl
    GROUP BY scenario, account_id, month, customer_id
""").df()
lookup = {(r.scenario, r.account_id, r.month, r.customer_id): r.amount for r in raw.itertuples()}
print(f"  Pre-load time: {time.perf_counter() - t:.2f}s  |  Entries: {len(lookup):,}")
del raw; gc.collect()

def q(accounts, ctx):
    month = ctx.period.start.strftime("%Y-%m")
    cust  = ctx.get("customer_id")
    return sum(lookup.get((ctx.scenario, a, month, cust), 0.0) for a in accounts)

# Same measures defined twice:
# - resolver   for plain Calculator
# - sql_expr   for DuckDBCalculator
def make_registry():
    r = fpa.MeasureRegistry()
    r.register_many([
        fpa.BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: q(["4000", "4010"], ctx),
            sql_expr="SUM(CASE WHEN account_id IN ('4000','4010') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)",
        ),
        fpa.BaseMeasure(
            name="COGS",
            resolver=lambda ctx: q(["5000", "5010"], ctx),
            sql_expr="SUM(CASE WHEN account_id IN ('5000','5010') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)",
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
    return r

registry = make_registry()
plain_calc  = fpa.Calculator(registry)
duckdb_calc = fpa.DuckDBCalculator(registry, con, table="gl")

# ---------------------------------------------------------------------------
# Test 1: build_table (measures x periods, no dimension breakdown)
# ---------------------------------------------------------------------------

section("Test 1: build_table — income statement across 12 months")

measure_names = ["Revenue", "COGS", "Gross Profit", "Gross Margin %"]
periods_12 = calendar.month_range(date(2023, 1, 1), date(2023, 12, 31))

print(f"\n  {'Method':<22}  {'Time':>10}  {'Cells':>10}  {'Cells/sec':>12}")
print("  " + "-" * 58)

for label, calc in [("Calculator (dict)", plain_calc), ("DuckDBCalculator", duckdb_calc)]:
    calc.clear_cache()
    t = time.perf_counter()
    tbl = calc.build_table(measure_names, periods_12, scenario="Actual")
    secs = time.perf_counter() - t
    total = len(measure_names) * len(periods_12)
    print(f"  {label:<22}  {secs:>9.3f}s  {total:>10,}  {total/secs:>12,.0f}")

# ---------------------------------------------------------------------------
# Test 2: build_breakdown_table — single measure across dimensions
# ---------------------------------------------------------------------------

section("Test 2: build_breakdown_table — Revenue by customer x periods")

print(f"\n  {'Method':<22}  {'Dimensions':>12}  {'Periods':>8}  {'Time':>10}  {'Cells/sec':>12}")
print("  " + "-" * 72)

for n_dims, n_periods in [(1_000, 12), (5_000, 12), (10_000, 12), (10_000, 24)]:
    customers = ALL_CUSTOMERS[:n_dims]
    periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))

    for label, calc in [("Calculator (dict)", plain_calc), ("DuckDBCalculator", duckdb_calc)]:
        calc.clear_cache()
        t = time.perf_counter()
        calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="customer_id", dimension_values=customers,
        )
        secs = time.perf_counter() - t
        total = n_dims * n_periods
        print(f"  {label:<22}  {n_dims:>12,}  {n_periods:>8}  {secs:>9.3f}s  {total/secs:>12,.0f}")
    print()

# ---------------------------------------------------------------------------
# Test 3: build_breakdown_table with derived measure (Gross Margin %)
# ---------------------------------------------------------------------------

section("Test 3: breakdown on a derived measure (Gross Margin %)")
print("  Gross Margin % depends on Gross Profit depends on Revenue and COGS.")
print("  DuckDBCalculator fetches Revenue+COGS in one query; derives GP and GM% in Python.")
print()

print(f"  {'Method':<22}  {'Dimensions':>12}  {'Periods':>8}  {'Time':>10}  {'Cells/sec':>12}")
print("  " + "-" * 72)

for n_dims, n_periods in [(1_000, 12), (10_000, 12), (10_000, 24)]:
    customers = ALL_CUSTOMERS[:n_dims]
    periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))

    for label, calc in [("Calculator (dict)", plain_calc), ("DuckDBCalculator", duckdb_calc)]:
        calc.clear_cache()
        t = time.perf_counter()
        calc.build_breakdown_table(
            "Gross Margin %", periods, scenario="Actual",
            dimension="customer_id", dimension_values=customers,
        )
        secs = time.perf_counter() - t
        total = n_dims * n_periods
        print(f"  {label:<22}  {n_dims:>12,}  {n_periods:>8}  {secs:>9.3f}s  {total/secs:>12,.0f}")
    print()

# ---------------------------------------------------------------------------
# Test 4: verify correctness — both calculators produce the same numbers
# ---------------------------------------------------------------------------

section("Test 4: correctness check — both calculators agree")

customers_sample = ALL_CUSTOMERS[:20]
periods_check = calendar.month_range(date(2023, 1, 1), date(2023, 3, 31))

plain_calc.clear_cache()
duckdb_calc.clear_cache()

tbl_plain  = plain_calc.build_breakdown_table(
    "Gross Margin %", periods_check, scenario="Actual",
    dimension="customer_id", dimension_values=customers_sample,
)
tbl_duckdb = duckdb_calc.build_breakdown_table(
    "Gross Margin %", periods_check, scenario="Actual",
    dimension="customer_id", dimension_values=customers_sample,
)

diff = (tbl_plain - tbl_duckdb).abs().max().max()
print(f"\n  Max absolute difference between calculators: {diff:.6f}")
print(f"  {'PASS' if diff < 0.01 else 'FAIL'} — results match within floating-point tolerance")

con.close()
print("\nDone.")
