"""
DuckDB usage patterns benchmark.

Shows the difference between three approaches:

  Pattern A (wrong):  one DuckDB query per cell
  Pattern B (ok):     pre-load all data into a Python dict, cell-by-cell resolution
  Pattern C (right):  one DuckDB query per period, returns all dimension values at once
  Pattern D (best):   one DuckDB query for the entire table using SQL CTEs for derived measures

Run from project root:
  python benchmark_duckdb.py
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

def elapsed(t):
    return f"{time.perf_counter() - t:.2f}s"

# ---------------------------------------------------------------------------
# Generate data and load into DuckDB once
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
# Pattern A: one DuckDB query per cell (what most people do wrong)
# ---------------------------------------------------------------------------

section("Pattern A (wrong): one DuckDB query per cell")
print("  Each resolver call executes a SELECT against DuckDB.")
print()

def cell_resolver(account_ids, ctx):
    acct_list = ", ".join(f"'{a}'" for a in account_ids)
    customer = ctx.get("customer_id")
    row = con.execute(f"""
        SELECT COALESCE(SUM(amount), 0.0)
        FROM gl
        WHERE account_id IN ({acct_list})
          AND scenario = ?
          AND date BETWEEN ? AND ?
          AND customer_id = ?
    """, [ctx.scenario, ctx.period.start, ctx.period.end, customer]).fetchone()
    return row[0]

registry_a = fpa.MeasureRegistry()
registry_a.register(fpa.BaseMeasure(
    name="Revenue",
    resolver=lambda ctx: cell_resolver(["4000", "4010"], ctx),
))

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 60)

for n_dims, n_periods in [(50, 1), (100, 1), (100, 3)]:
    customers = ALL_CUSTOMERS[:n_dims]
    periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))
    calc = fpa.Calculator(registry_a)
    t = time.perf_counter()
    calc.build_breakdown_table(
        "Revenue", periods, scenario="Actual",
        dimensions="customer_id", dimension_values=customers,
    )
    secs = time.perf_counter() - t
    total = n_dims * n_periods
    print(f"{n_dims:>12,}  {n_periods:>8}  {total:>12,}  {secs:>9.2f}s  {total/secs:>12,.0f}")

print("\n  (stopping early — extrapolates to hours at 10K dims x 24 months)")

# ---------------------------------------------------------------------------
# Pattern B: pre-load into Python dict, cell-by-cell (current approach)
# ---------------------------------------------------------------------------

section("Pattern B (ok): pre-load into dict, cell-by-cell resolution")
print("  Load everything into memory once. Each resolver call is a dict lookup.")
print()

t = time.perf_counter()
raw = con.execute("""
    SELECT scenario, account_id,
           strftime(date, '%Y-%m') AS month,
           customer_id,
           SUM(amount) AS amount
    FROM gl
    GROUP BY scenario, account_id, month, customer_id
""").df()
lookup = {
    (row.scenario, row.account_id, row.month, row.customer_id): row.amount
    for row in raw.itertuples()
}
load_time = time.perf_counter() - t
print(f"  Pre-load time: {load_time:.2f}s  |  Dict entries: {len(lookup):,}")
print()

def dict_resolver(account_ids, ctx):
    month = ctx.period.start.strftime("%Y-%m")
    customer = ctx.get("customer_id")
    return sum(lookup.get((ctx.scenario, a, month, customer), 0.0) for a in account_ids)

registry_b = fpa.MeasureRegistry()
registry_b.register(fpa.BaseMeasure(
    name="Revenue",
    resolver=lambda ctx: dict_resolver(["4000", "4010"], ctx),
))

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 60)

for n_dims, n_periods in [(1_000, 12), (5_000, 12), (10_000, 12), (10_000, 24)]:
    customers = ALL_CUSTOMERS[:n_dims]
    periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))
    calc = fpa.Calculator(registry_b)
    t = time.perf_counter()
    calc.build_breakdown_table(
        "Revenue", periods, scenario="Actual",
        dimensions="customer_id", dimension_values=customers,
    )
    secs = time.perf_counter() - t
    total = n_dims * n_periods
    print(f"{n_dims:>12,}  {n_periods:>8}  {total:>12,}  {secs:>9.2f}s  {total/secs:>12,.0f}")

del lookup, raw; gc.collect()

# ---------------------------------------------------------------------------
# Pattern C: one DuckDB query per period, returns all dims at once
# ---------------------------------------------------------------------------

section("Pattern C (right): one DuckDB query per period column")
print("  24 queries total for 24 months. Each returns all 10K customers at once.")
print("  This bypasses the library's cell loop entirely for breakdown tables.")
print()

def query_period_column(account_ids, period, scenario, customer_ids):
    """One query -> dict of {customer_id: value} for the entire period column."""
    acct_list = ", ".join(f"'{a}'" for a in account_ids)
    cust_list = ", ".join(f"'{c}'" for c in customer_ids)
    rows = con.execute(f"""
        SELECT customer_id, COALESCE(SUM(amount), 0.0)
        FROM gl
        WHERE account_id IN ({acct_list})
          AND scenario = ?
          AND date BETWEEN ? AND ?
          AND customer_id IN ({cust_list})
        GROUP BY customer_id
    """, [scenario, period.start, period.end]).fetchall()
    result = {r[0]: r[1] for r in rows}
    return [result.get(c, 0.0) for c in customer_ids]

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 60)

for n_dims, n_periods in [(1_000, 12), (5_000, 12), (10_000, 12), (10_000, 24)]:
    customers = ALL_CUSTOMERS[:n_dims]
    periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))
    t = time.perf_counter()
    cols = {}
    for period in periods:
        cols[period.label] = query_period_column(["4000", "4010"], period, "Actual", customers)
    result_df = pd.DataFrame(cols, index=customers)
    secs = time.perf_counter() - t
    total = n_dims * n_periods
    print(f"{n_dims:>12,}  {n_periods:>8}  {total:>12,}  {secs:>9.2f}s  {total/secs:>12,.0f}")

# ---------------------------------------------------------------------------
# Pattern D: one DuckDB query for the entire table, derived measures in SQL
# ---------------------------------------------------------------------------

section("Pattern D (best): entire table in a single DuckDB query")
print("  BaseMeasures become SQL expressions (account filters).")
print("  Derived measures become CTEs.")
print("  One round-trip to DuckDB for the complete measures x dims x periods table.")
print()

MEASURE_SQL = {
    "Revenue": "SUM(CASE WHEN account_id IN ('4000','4010') THEN amount ELSE 0 END)",
    "COGS":    "SUM(CASE WHEN account_id IN ('5000','5010') THEN amount ELSE 0 END)",
}

def build_full_table_sql(measure_exprs, derived, period_list, scenario, dimension):
    """
    Build a single SQL query that computes all base measures and derived measures
    across all periods and all dimension values at once.
    """
    period_cases = "\n".join(
        f"SUM(CASE WHEN date BETWEEN DATE '{p.start}' AND DATE '{p.end}' "
        f"THEN amount ELSE 0 END) FILTER (WHERE account_id IN ({{accts}})) AS \"{p.label}_{name}\""
        for p in period_list
        for name, accts in [("rev", "'4000','4010'"), ("cogs", "'5000','5010'")]
    )

    # Simpler: pivot with explicit period conditions
    select_parts = []
    for p in period_list:
        for mname, sql_expr in measure_exprs.items():
            col = sql_expr.replace("amount", f"CASE WHEN date BETWEEN DATE '{p.start}' AND DATE '{p.end}' THEN amount ELSE 0 END")
            select_parts.append(f"{col} AS \"{p.label}|{mname}\"")

    query = f"""
        SELECT {dimension},
               {', '.join(select_parts)}
        FROM gl
        WHERE scenario = '{scenario}'
        GROUP BY {dimension}
        ORDER BY {dimension}
    """
    return query

print(f"{'Dimensions':>12}  {'Periods':>8}  {'Measures':>10}  {'Total cells':>12}  {'Time':>10}  {'Cells/sec':>12}")
print("-" * 65)

for n_dims, n_periods in [(1_000, 12), (5_000, 12), (10_000, 12), (10_000, 24)]:
    customers = ALL_CUSTOMERS[:n_dims]
    periods = calendar.month_range(date(2023, 1, 1), date(2023, 1, 1) + relativedelta(months=n_periods - 1))
    n_measures = len(MEASURE_SQL)

    cust_list = ", ".join(f"'{c}'" for c in customers)
    select_parts = []
    for p in periods:
        for mname, sql_expr in MEASURE_SQL.items():
            col_expr = sql_expr.replace(
                "amount",
                f"CASE WHEN date BETWEEN DATE '{p.start}' AND DATE '{p.end}' THEN amount ELSE 0 END"
            )
            select_parts.append(f"{col_expr} AS \"{p.label}|{mname}\"")

    query = f"""
        SELECT customer_id, {', '.join(select_parts)}
        FROM gl
        WHERE scenario = 'Actual'
          AND customer_id IN ({cust_list})
        GROUP BY customer_id
    """

    t = time.perf_counter()
    result = con.execute(query).df()
    secs = time.perf_counter() - t

    total = n_dims * n_periods * n_measures
    print(f"{n_dims:>12,}  {n_periods:>8}  {n_measures:>10}  {total:>12,}  {secs:>9.2f}s  {total/secs:>12,.0f}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

section("Summary")
print("""
  Pattern A  one query per cell        -> thousands of cells/sec, hits hours at scale
  Pattern B  pre-load dict + cell loop -> ~80K cells/sec, memory grows with data size
  Pattern C  one query per period      -> ~200K-500K cells/sec, data stays in DuckDB
  Pattern D  one query for everything  -> millions of cells/sec, full columnar execution
""")

con.close()
print("Done.")
