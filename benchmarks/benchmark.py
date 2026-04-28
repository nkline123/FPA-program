"""
Benchmark: find where the library and the pre-load pattern start to break.

Tests:
  1. Pre-load memory — at what row count does the lookup dict get expensive?
  2. Resolver throughput — at what dimension count does build_breakdown_table slow down?
  3. Memo dict size — at what (measures x periods x dimensions) does the cache get heavy?
  4. Large measure graph — at what measure count does DAG build / resolution slow?

Run from the project root:
  python benchmark.py
"""

import sys, os, time, gc
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import fpa

rng = np.random.default_rng(42)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def elapsed(start):
    return f"{time.perf_counter() - start:.2f}s"

def mem_mb(obj):
    import pickle
    return len(pickle.dumps(obj)) / 1024 / 1024

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ---------------------------------------------------------------------------
# 1. Pre-load memory: rows → lookup dict size and build time
# ---------------------------------------------------------------------------

section("1. Pre-load: rows -> memory and groupby time")

print(f"{'Rows':>12}  {'Build time':>12}  {'Dict entries':>14}  {'Dict MB':>10}")
print("-" * 55)

for n_rows in [100_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]:
    n_customers = 10_000
    n_accounts  = 5

    df = pd.DataFrame({
        "scenario":    rng.choice(["Actual", "Budget"], size=n_rows),
        "account_id":  rng.choice([f"{4000 + i*10}" for i in range(n_accounts)], size=n_rows),
        "month":       rng.choice(pd.date_range("2023-01", periods=24, freq="MS").strftime("%Y-%m"), size=n_rows),
        "customer_id": rng.integers(0, n_customers, size=n_rows).astype(str),
        "amount":      rng.normal(10_000, 2_000, size=n_rows),
    })

    t = time.perf_counter()
    lookup = (
        df.groupby(["scenario", "account_id", "month", "customer_id"])["amount"]
        .sum()
        .to_dict()
    )
    build_time = time.perf_counter() - t
    dict_mb = mem_mb(lookup)

    print(f"{n_rows:>12,}  {build_time:>11.2f}s  {len(lookup):>14,}  {dict_mb:>9.1f}MB")
    del df, lookup
    gc.collect()

# ---------------------------------------------------------------------------
# 2. Dimension scale: build_breakdown_table at 10K dimensions, varying periods
# ---------------------------------------------------------------------------

section("2. Dimension scale: build_breakdown_table throughput")

N_ROWS = 2_000_000
N_CUSTOMERS = 10_000

df = pd.DataFrame({
    "scenario":    rng.choice(["Actual", "Budget"], size=N_ROWS),
    "account_id":  rng.choice(["4000", "4010"], size=N_ROWS),
    "month":       rng.choice(pd.date_range("2023-01", periods=24, freq="MS").strftime("%Y-%m"), size=N_ROWS),
    "customer_id": rng.integers(0, N_CUSTOMERS, size=N_ROWS).astype(str),
    "amount":      rng.normal(10_000, 2_000, size=N_ROWS),
})

lookup = (
    df.groupby(["scenario", "account_id", "month", "customer_id"])["amount"]
    .sum()
    .to_dict()
)
del df; gc.collect()

def revenue_resolver(ctx):
    month = ctx.period.start.strftime("%Y-%m")
    customer = ctx.get("customer_id")
    return sum(lookup.get(("Actual", acct, month, customer), 0.0) for acct in ["4000", "4010"])

registry = fpa.MeasureRegistry()
registry.register(fpa.BaseMeasure(name="Revenue", resolver=revenue_resolver))
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 60)

for n_dims in [100, 1_000, 5_000, 10_000]:
    for n_periods in [1, 12, 24]:
        customers = [str(i) for i in range(n_dims)]
        from datetime import date
        from dateutil.relativedelta import relativedelta
        start_d = date(2023, 1, 1)
        end_d = start_d + relativedelta(months=n_periods - 1)
        periods = calendar.month_range(start_d, end_d)
        calc = fpa.Calculator(registry)
        t = time.perf_counter()
        calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimensions="customer_id", dimension_values=customers,
        )
        secs = time.perf_counter() - t
        total_cells = n_dims * n_periods
        cps = total_cells / secs if secs > 0 else float("inf")
        print(f"{n_dims:>12,}  {n_periods:>8}  {total_cells:>12,}  {secs:>9.2f}s  {cps:>12,.0f}")

del lookup, registry

# ---------------------------------------------------------------------------
# 3. Memo dict growth: measures × periods × scenario combinations
# ---------------------------------------------------------------------------

section("3. Memo dict growth: cache size at scale")

def make_static_registry(n_base, n_derived):
    r = fpa.MeasureRegistry()
    for i in range(n_base):
        r.register(fpa.BaseMeasure(name=f"Base{i}", resolver=lambda ctx, i=i: float(i)))
    for i in range(n_derived):
        dep = f"Base{i % n_base}"
        r.register(fpa.Measure(name=f"Derived{i}", dependencies=[dep], formula=lambda v, k=dep: v[k] * 2))
    return r

calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

print(f"{'Measures':>10}  {'Periods':>8}  {'Scenarios':>10}  {'Cache entries':>15}  {'Time':>10}")
print("-" * 60)

for n_measures in [50, 200, 500]:
    for n_periods in [12, 60]:
        for n_scenarios in [1, 5]:
            r = make_static_registry(n_base=n_measures // 2, n_derived=n_measures // 2)
            calc = fpa.Calculator(r)
            from datetime import date
            from dateutil.relativedelta import relativedelta
            start_d = date(2020, 1, 1)
            end_d = start_d + relativedelta(months=n_periods - 1)
            periods = calendar.month_range(start_d, end_d)
            names = r.names()
            t = time.perf_counter()
            for scenario in [f"S{i}" for i in range(n_scenarios)]:
                calc.build_table(names, periods, scenario=scenario)
            secs = time.perf_counter() - t
            cache_entries = len(calc._memo)
            print(f"{n_measures:>10}  {n_periods:>8}  {n_scenarios:>10}  {cache_entries:>15,}  {secs:>9.2f}s")

# ---------------------------------------------------------------------------
# 4. DAG build time at large measure count
# ---------------------------------------------------------------------------

section("4. DAG build time: large measure graphs")

from fpa.measures.dag import MeasureDAG

print(f"{'Measures':>10}  {'DAG build':>12}  {'Topo sort':>12}")
print("-" * 40)

for n in [100, 500, 1_000, 5_000, 10_000]:
    r = fpa.MeasureRegistry()
    # chain: Base0 <- Derived0 <- Derived1 <- ... wide fan-in at each level
    for i in range(n // 2):
        r.register(fpa.BaseMeasure(name=f"B{i}", resolver=lambda ctx: 1.0))
    for i in range(n // 2):
        dep = f"B{i % (n // 2)}"
        r.register(fpa.Measure(name=f"D{i}", dependencies=[dep], formula=lambda v, k=dep: v[k]))

    t = time.perf_counter()
    dag = MeasureDAG(r)
    dag_time = time.perf_counter() - t

    t = time.perf_counter()
    dag.evaluation_order()
    sort_time = time.perf_counter() - t

    print(f"{n:>10,}  {dag_time:>11.3f}s  {sort_time:>11.3f}s")

print("\nDone.")
