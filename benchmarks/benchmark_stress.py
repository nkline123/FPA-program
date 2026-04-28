"""
Stress benchmark — push the library to its limits.

Dimensions tested:
  1. Data scale       — rows 2M → 20M, pre-load time and memory
  2. Dimension scale  — customers 10K → 100K x 24/60 periods
  3. Period scale     — 12 → 60 months (5 years) at 10K dimensions
  4. Measure depth    — shallow (4) vs deep (20) DAG x large grids
  5. Multi-scenario   — 5 scenarios resolved back-to-back, memo reuse
  6. DuckDB at scale  — repeat key cases with DuckDBCalculator

Run from project root:
  python benchmark_stress.py
"""

import sys, os, time, gc, tracemalloc
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import psutil
import duckdb
import fpa
from datetime import date
from dateutil.relativedelta import relativedelta

rng = np.random.default_rng(42)
PROCESS = psutil.Process(os.getpid())

def section(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")

def mem_mb():
    return PROCESS.memory_info().rss / 1024 / 1024

def run(fn):
    gc.collect()
    t = time.perf_counter()
    result = fn()
    secs = time.perf_counter() - t
    return secs, result

def fmt(secs, cells):
    return f"{secs:>8.2f}s  {cells/secs:>12,.0f} cells/s"

calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

ACCOUNTS  = ["4000", "4010", "5000", "5010", "6000", "6010", "6020", "7000"]
SCENARIOS = ["Actual", "Budget", "Forecast", "Reforecast", "Plan"]

# ---------------------------------------------------------------------------
# 1. Data scale: pre-load cost and memory
# ---------------------------------------------------------------------------

section("1. Data scale: pre-load rows -> dict (plain Calculator setup cost)")

print(f"  {'Rows':>12}  {'Build time':>12}  {'RSS before':>12}  {'RSS after':>12}  {'Delta':>10}")
print("  " + "-" * 65)

for n_rows in [2_000_000, 5_000_000, 10_000_000, 20_000_000]:
    df = pd.DataFrame({
        "scenario":    rng.choice(SCENARIOS[:2], size=n_rows),
        "account_id":  rng.choice(ACCOUNTS,      size=n_rows),
        "month":       rng.choice(pd.date_range("2020-01", periods=60, freq="MS").strftime("%Y-%m"), size=n_rows),
        "customer_id": rng.integers(0, 50_000, size=n_rows).astype(str),
        "amount":      rng.normal(10_000, 3_000, size=n_rows),
    })
    gc.collect()
    before = mem_mb()
    t = time.perf_counter()
    lookup = (df.groupby(["scenario","account_id","month","customer_id"])["amount"].sum().to_dict())
    secs = time.perf_counter() - t
    after = mem_mb()
    print(f"  {n_rows:>12,}  {secs:>11.2f}s  {before:>11.0f}MB  {after:>11.0f}MB  {after-before:>9.0f}MB")
    del df, lookup; gc.collect()

# ---------------------------------------------------------------------------
# Shared setup for remaining tests
# ---------------------------------------------------------------------------

section("Setup: generating 10M rows, loading into DuckDB and dict")

N_ROWS      = 10_000_000
N_CUSTOMERS = 50_000

print(f"  Generating {N_ROWS:,} rows x {N_CUSTOMERS:,} customers...")
t0 = time.perf_counter()
df = pd.DataFrame({
    "scenario":    rng.choice(SCENARIOS[:2], size=N_ROWS),
    "account_id":  rng.choice(ACCOUNTS,      size=N_ROWS),
    "date":        rng.choice(pd.date_range("2020-01", periods=60, freq="MS"), size=N_ROWS),
    "customer_id": rng.integers(0, N_CUSTOMERS, size=N_ROWS).astype(str),
    "amount":      rng.normal(10_000, 3_000, size=N_ROWS),
})
df["date"] = pd.to_datetime(df["date"]).dt.date
df["month"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
print(f"  Generated in {time.perf_counter()-t0:.1f}s")

# DuckDB
t0 = time.perf_counter()
con = duckdb.connect()
con.execute("CREATE TABLE gl AS SELECT * FROM df")
print(f"  DuckDB loaded in {time.perf_counter()-t0:.1f}s")

# Dict lookup
t0 = time.perf_counter()
lookup = (
    df.groupby(["scenario","account_id","month","customer_id"])["amount"]
    .sum().to_dict()
)
print(f"  Dict built in {time.perf_counter()-t0:.1f}s  ({len(lookup):,} entries, {mem_mb():.0f}MB RSS)")
del df; gc.collect()

ALL_CUSTOMERS = [str(i) for i in range(N_CUSTOMERS)]

# ---------------------------------------------------------------------------
# Measure factories
# ---------------------------------------------------------------------------

def make_shallow_registry():
    """4 base + 4 derived (2 levels deep)."""
    def q(accts, ctx):
        m = ctx.period.start.strftime("%Y-%m")
        c = ctx.get("customer_id")
        return sum(lookup.get((ctx.scenario, a, m, c), 0.0) for a in accts)

    r = fpa.MeasureRegistry()
    r.register_many([
        fpa.BaseMeasure("Revenue",  lambda ctx: q(["4000","4010"], ctx),
            sql_expr="SUM(CASE WHEN account_id IN ('4000','4010') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"),
        fpa.BaseMeasure("COGS",     lambda ctx: q(["5000","5010"], ctx),
            sql_expr="SUM(CASE WHEN account_id IN ('5000','5010') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"),
        fpa.BaseMeasure("OpEx",     lambda ctx: q(["6000","6010","6020"], ctx),
            sql_expr="SUM(CASE WHEN account_id IN ('6000','6010','6020') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"),
        fpa.BaseMeasure("Interest", lambda ctx: q(["7000"], ctx),
            sql_expr="SUM(CASE WHEN account_id = '7000' AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"),
        fpa.Measure("Gross Profit",      ["Revenue","COGS"],         lambda v: v["Revenue"]-v["COGS"]),
        fpa.Measure("Gross Margin %",    ["Gross Profit","Revenue"],  lambda v: (v["Gross Profit"]/v["Revenue"]*100) if v["Revenue"] else 0.0),
        fpa.Measure("Operating Income",  ["Gross Profit","OpEx"],    lambda v: v["Gross Profit"]-v["OpEx"]),
        fpa.Measure("Net Income",        ["Operating Income","Interest"], lambda v: v["Operating Income"]-v["Interest"]),
    ])
    return r

def make_deep_registry(n_base=8, n_derived=12):
    """Deeper DAG: more derived measures stacked on the same base measures."""
    def q(accts, ctx):
        m = ctx.period.start.strftime("%Y-%m")
        c = ctx.get("customer_id")
        return sum(lookup.get((ctx.scenario, a, m, c), 0.0) for a in accts)

    r = fpa.MeasureRegistry()
    acct_groups = [ACCOUNTS[i:i+1] for i in range(n_base)]
    for i, accts in enumerate(acct_groups):
        r.register(fpa.BaseMeasure(
            f"Base{i}", lambda ctx, a=accts: q(a, ctx),
            sql_expr=f"SUM(CASE WHEN account_id = '{accts[0]}' AND date BETWEEN '{{start}}' AND '{{end}}' THEN amount ELSE 0 END)",
        ))
    # Layer 1: pairs
    for i in range(0, n_base-1, 2):
        r.register(fpa.Measure(f"L1_{i}", [f"Base{i}", f"Base{i+1}"],
                               lambda v, a=f"Base{i}", b=f"Base{i+1}": v[a]+v[b]))
    # Layer 2: sums of layer 1
    l1_names = [f"L1_{i}" for i in range(0, n_base-1, 2)]
    for i in range(min(n_derived - n_base//2, 4)):
        deps = l1_names[:2]
        r.register(fpa.Measure(f"L2_{i}", deps, lambda v, d=deps: sum(v[x] for x in d)))
    return r

# ---------------------------------------------------------------------------
# 2. Dimension scale
# ---------------------------------------------------------------------------

section("2. Dimension scale: build_breakdown_table (Revenue)")

shallow = make_shallow_registry()
plain   = fpa.Calculator(shallow)
ddb     = fpa.DuckDBCalculator(shallow, con, table="gl")

print(f"\n  {'Method':<20}  {'Dims':>8}  {'Periods':>8}  {'Cells':>10}  {'Time':>10}  {'Cells/s':>12}")
print("  " + "-" * 75)

for n_dims, n_periods in [
    (10_000, 12), (10_000, 24), (10_000, 60),
    (25_000, 12), (25_000, 24),
    (50_000, 12), (50_000, 24),
]:
    customers = ALL_CUSTOMERS[:n_dims]
    start_d = date(2020, 1, 1)
    periods = calendar.month_range(start_d, start_d + relativedelta(months=n_periods-1))

    for label, calc in [("Calculator", plain), ("DuckDBCalc", ddb)]:
        calc.clear_cache()
        secs, _ = run(lambda c=calc, p=periods, cust=customers: c.build_breakdown_table(
            "Revenue", p, scenario="Actual",
            dimensions="customer_id", dimension_values=cust,
        ))
        total = n_dims * n_periods
        print(f"  {label:<20}  {n_dims:>8,}  {n_periods:>8}  {total:>10,}  {fmt(secs, total)}")
    print()

# ---------------------------------------------------------------------------
# 3. Period scale: 5 years monthly
# ---------------------------------------------------------------------------

section("3. Period scale: 60 months (5 years) at varying dimension counts")

periods_60 = calendar.month_range(date(2020, 1, 1), date(2024, 12, 31))

print(f"\n  {'Method':<20}  {'Dims':>8}  {'Periods':>8}  {'Cells':>10}  {'Time':>10}  {'Cells/s':>12}")
print("  " + "-" * 75)

for n_dims in [1_000, 5_000, 10_000, 25_000]:
    customers = ALL_CUSTOMERS[:n_dims]
    for label, calc in [("Calculator", plain), ("DuckDBCalc", ddb)]:
        calc.clear_cache()
        secs, _ = run(lambda c=calc, cust=customers: c.build_breakdown_table(
            "Revenue", periods_60, scenario="Actual",
            dimensions="customer_id", dimension_values=cust,
        ))
        total = n_dims * 60
        print(f"  {label:<20}  {n_dims:>8,}  {60:>8}  {total:>10,}  {fmt(secs, total)}")
    print()

# ---------------------------------------------------------------------------
# 4. Measure depth: full income statement (8 measures) across large grids
# ---------------------------------------------------------------------------

section("4. Measure depth: build_table for full income statement")

measure_names = ["Revenue","COGS","OpEx","Interest",
                 "Gross Profit","Gross Margin %","Operating Income","Net Income"]

print(f"\n  {'Method':<20}  {'Measures':>10}  {'Periods':>8}  {'Cells':>10}  {'Time':>10}  {'Cells/s':>12}")
print("  " + "-" * 75)

for n_periods in [12, 60]:
    start_d = date(2020, 1, 1)
    periods = calendar.month_range(start_d, start_d + relativedelta(months=n_periods-1))
    for label, calc in [("Calculator", plain), ("DuckDBCalc", ddb)]:
        calc.clear_cache()
        secs, _ = run(lambda c=calc, p=periods: c.build_table(
            measure_names, p, scenario="Actual"
        ))
        total = len(measure_names) * n_periods
        print(f"  {label:<20}  {len(measure_names):>10}  {n_periods:>8}  {total:>10,}  {fmt(secs, total)}")
    print()

# ---------------------------------------------------------------------------
# 5. Multi-scenario: 5 scenarios, memo reuse within vs across scenarios
# ---------------------------------------------------------------------------

section("5. Multi-scenario: resolving 5 scenarios back-to-back")

periods_12 = calendar.month_range(date(2020, 1, 1), date(2020, 12, 31))
customers_5k = ALL_CUSTOMERS[:5_000]

print(f"\n  {'Method':<20}  {'Scenarios':>10}  {'Dims':>8}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}")
print("  " + "-" * 75)

for label, calc in [("Calculator", plain), ("DuckDBCalc", ddb)]:
    calc.clear_cache()
    t = time.perf_counter()
    for scenario in SCENARIOS:
        calc.build_breakdown_table(
            "Revenue", periods_12, scenario=scenario,
            dimensions="customer_id", dimension_values=customers_5k,
        )
    secs = time.perf_counter() - t
    total = len(SCENARIOS) * 5_000 * 12
    print(f"  {label:<20}  {len(SCENARIOS):>10}  {5_000:>8,}  {12:>8}  {total:>12,}  {secs:>9.2f}s")

# ---------------------------------------------------------------------------
# 6. Deep DAG: many derived measures
# ---------------------------------------------------------------------------

section("6. Deep measure DAG: 8 base + 12 derived measures")

deep = make_deep_registry(n_base=8, n_derived=12)
deep_plain = fpa.Calculator(deep)
deep_ddb   = fpa.DuckDBCalculator(deep, con, table="gl")
deep_measures = deep.names()

print(f"\n  {'Method':<20}  {'Measures':>10}  {'Dims':>8}  {'Periods':>8}  {'Cells':>10}  {'Time':>10}  {'Cells/s':>12}")
print("  " + "-" * 80)

for n_dims, n_periods in [(5_000, 12), (10_000, 12), (10_000, 24)]:
    customers = ALL_CUSTOMERS[:n_dims]
    start_d = date(2020, 1, 1)
    periods = calendar.month_range(start_d, start_d + relativedelta(months=n_periods-1))
    target = deep_measures[-1]  # deepest derived measure

    for label, calc in [("Calculator", deep_plain), ("DuckDBCalc", deep_ddb)]:
        calc.clear_cache()
        secs, _ = run(lambda c=calc, p=periods, cust=customers, tgt=target: c.build_breakdown_table(
            tgt, p, scenario="Actual",
            dimensions="customer_id", dimension_values=cust,
        ))
        total = n_dims * n_periods
        print(f"  {label:<20}  {len(deep_measures):>10}  {n_dims:>8,}  {n_periods:>8}  {total:>10,}  {fmt(secs, total)}")
    print()

# ---------------------------------------------------------------------------
# 7. Memory: DuckDBCalculator keeps data in DuckDB; Calculator holds dict
# ---------------------------------------------------------------------------

section("7. Memory profile: RSS at key points")

print(f"\n  Current RSS (dict + DuckDB in process): {mem_mb():.0f} MB")
print(f"  Dict entries: {len(lookup):,}")
print(f"  DuckDB rows:  {con.execute('SELECT COUNT(*) FROM gl').fetchone()[0]:,}")
print()
print("  If you dropped the dict and relied solely on DuckDBCalculator,")
print(f"  you would reclaim roughly {len(lookup) * 200 / 1024 / 1024:.0f}–{len(lookup) * 300 / 1024 / 1024:.0f} MB")
print("  (estimated 200–300 bytes per dict entry with Python object overhead).")

# ---------------------------------------------------------------------------
# 8. Breaking point: push until things get slow
# ---------------------------------------------------------------------------

section("8. Breaking point: DuckDBCalculator at maximum scale")

print(f"\n  {'Dims':>10}  {'Periods':>8}  {'Cells':>12}  {'Time':>10}  {'Cells/s':>14}  Note")
print("  " + "-" * 75)

for n_dims, n_periods in [
    (50_000, 12),
    (50_000, 60),
    (100_000, 12),
    (100_000, 60),
]:
    if n_dims > N_CUSTOMERS:
        print(f"  {n_dims:>10,}  {n_periods:>8}  {'—':>12}  {'—':>10}  {'—':>14}  exceeds {N_CUSTOMERS:,} customers in dataset")
        continue
    customers = ALL_CUSTOMERS[:n_dims]
    start_d = date(2020, 1, 1)
    periods = calendar.month_range(start_d, start_d + relativedelta(months=n_periods-1))
    ddb.clear_cache()
    secs, _ = run(lambda p=periods, cust=customers: ddb.build_breakdown_table(
        "Revenue", p, scenario="Actual",
        dimensions="customer_id", dimension_values=cust,
    ))
    total = n_dims * n_periods
    note = "slow" if secs > 10 else "ok"
    print(f"  {n_dims:>10,}  {n_periods:>8}  {total:>12,}  {fmt(secs, total)}  {note}")

con.close()
print(f"\n  Final RSS: {mem_mb():.0f} MB")
print("\nDone.")
