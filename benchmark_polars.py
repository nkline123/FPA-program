"""
Polars vs Pandas benchmark.

Tests three things:
  1. Pre-load speed: groupby + to_dict (one-time startup cost)
  2. Resolver throughput: cell-by-cell dict lookup (the actual bottleneck)
  3. Output DataFrame construction: pandas vs polars for build_table output

Run from project root:
  python benchmark_polars.py
"""

import sys, os, time, gc
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import polars as pl
import fpa
from datetime import date
from dateutil.relativedelta import relativedelta

rng = np.random.default_rng(42)

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ---------------------------------------------------------------------------
# 1. Pre-load speed: pandas groupby vs polars groupby -> dict
# ---------------------------------------------------------------------------

section("1. Pre-load speed: pandas vs polars groupby -> dict")

SCENARIOS   = ["Actual", "Budget"]
ACCOUNTS    = ["4000", "4010"]
MONTHS      = pd.date_range("2022-01", periods=24, freq="MS").strftime("%Y-%m").tolist()
N_CUSTOMERS = 10_000

print(f"{'Rows':>12}  {'Pandas':>10}  {'Polars':>10}  {'Speedup':>10}")
print("-" * 48)

for n_rows in [500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]:
    data = {
        "scenario":    rng.choice(SCENARIOS, size=n_rows),
        "account_id":  rng.choice(ACCOUNTS,  size=n_rows),
        "month":       rng.choice(MONTHS,    size=n_rows),
        "customer_id": rng.integers(0, N_CUSTOMERS, size=n_rows).astype(str),
        "amount":      rng.normal(10_000, 2_000, size=n_rows),
    }

    # Pandas
    df_pd = pd.DataFrame(data)
    t = time.perf_counter()
    lookup_pd = (
        df_pd.groupby(["scenario", "account_id", "month", "customer_id"])["amount"]
        .sum()
        .to_dict()
    )
    pandas_time = time.perf_counter() - t
    del df_pd, lookup_pd; gc.collect()

    # Polars
    df_pl = pl.DataFrame(data)
    t = time.perf_counter()
    agg = (
        df_pl.group_by(["scenario", "account_id", "month", "customer_id"])
        .agg(pl.col("amount").sum())
    )
    # Build equivalent dict for resolver use
    lookup_pl = {
        (row[0], row[1], row[2], row[3]): row[4]
        for row in agg.iter_rows()
    }
    polars_time = time.perf_counter() - t
    del df_pl, agg, lookup_pl; gc.collect()

    speedup = pandas_time / polars_time
    print(f"{n_rows:>12,}  {pandas_time:>9.2f}s  {polars_time:>9.2f}s  {speedup:>9.1f}x")


# ---------------------------------------------------------------------------
# 2. Resolver throughput: the bottleneck is Python, not pandas
#    Show that switching lookup backend doesn't change cells/sec
# ---------------------------------------------------------------------------

section("2. Resolver throughput: dict lookup is the ceiling regardless")

N_ROWS      = 2_000_000
N_CUSTOMERS = 10_000

data = {
    "scenario":    rng.choice(SCENARIOS, size=N_ROWS),
    "account_id":  rng.choice(ACCOUNTS,  size=N_ROWS),
    "month":       rng.choice(MONTHS,    size=N_ROWS),
    "customer_id": rng.integers(0, N_CUSTOMERS, size=N_ROWS).astype(str),
    "amount":      rng.normal(10_000, 2_000, size=N_ROWS),
}

# Build both lookups
df_pd = pd.DataFrame(data)
lookup = (
    df_pd.groupby(["scenario", "account_id", "month", "customer_id"])["amount"]
    .sum()
    .to_dict()
)
del df_pd; gc.collect()

def resolver(ctx):
    month = ctx.period.start.strftime("%Y-%m")
    customer = ctx.get("customer_id")
    return sum(lookup.get(("Actual", acct, month, customer), 0.0) for acct in ACCOUNTS)

registry = fpa.MeasureRegistry()
registry.register(fpa.BaseMeasure(name="Revenue", resolver=resolver))
calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 60)
print("  (resolver uses a plain Python dict — same regardless of how it was built)")
print()

for n_dims in [1_000, 5_000, 10_000]:
    for n_periods in [12, 24]:
        customers = [str(i) for i in range(n_dims)]
        periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))
        calc = fpa.Calculator(registry)
        t = time.perf_counter()
        calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="customer_id", dimension_values=customers,
        )
        secs = time.perf_counter() - t
        total = n_dims * n_periods
        print(f"{n_dims:>12,}  {n_periods:>8}  {total:>12,}  {secs:>9.2f}s  {total/secs:>12,.0f}")

del lookup, registry


# ---------------------------------------------------------------------------
# 3. What WOULD help: vectorized resolvers
#    Instead of one resolver call per cell, resolve an entire column at once
# ---------------------------------------------------------------------------

section("3. The real fix: vectorized resolution (all dims x 1 period at once)")

data = {
    "scenario":    rng.choice(SCENARIOS, size=N_ROWS),
    "account_id":  rng.choice(ACCOUNTS,  size=N_ROWS),
    "month":       rng.choice(MONTHS,    size=N_ROWS),
    "customer_id": rng.integers(0, N_CUSTOMERS, size=N_ROWS).astype(str),
    "amount":      rng.normal(10_000, 2_000, size=N_ROWS),
}

df_pl = pl.DataFrame(data)

def vectorized_revenue(period, scenario, customer_ids):
    """Return a Series of revenue values for all customers at once."""
    month = period.start.strftime("%Y-%m")
    filtered = (
        df_pl
        .filter(
            (pl.col("scenario") == scenario) &
            (pl.col("account_id").is_in(ACCOUNTS)) &
            (pl.col("month") == month) &
            (pl.col("customer_id").is_in(customer_ids))
        )
        .group_by("customer_id")
        .agg(pl.col("amount").sum())
    )
    result = dict(zip(filtered["customer_id"].to_list(), filtered["amount"].to_list()))
    return [result.get(c, 0.0) for c in customer_ids]

calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)
customers = [str(i) for i in range(N_CUSTOMERS)]

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 60)
print("  (one Polars query per period column, not per cell)")
print()

for n_dims in [1_000, 5_000, 10_000]:
    for n_periods in [12, 24]:
        subset = customers[:n_dims]
        periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))
        t = time.perf_counter()
        results = {}
        for period in periods:
            col = vectorized_revenue(period, "Actual", subset)
            results[period.label] = col
        secs = time.perf_counter() - t
        total = n_dims * n_periods
        print(f"{n_dims:>12,}  {n_periods:>8}  {total:>12,}  {secs:>9.2f}s  {total/secs:>12,.0f}")

print("\nDone.")
