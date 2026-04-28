"""
Baseline benchmarks for the FPA calculation engine.

Run from the project root:
    python benchmarks.py

Records wall-clock time (median of N runs) and SQL query count for each
scenario.  Save this output before making architecture changes so you have
a reference to diff against.
"""

import sys
import os
import time
import statistics
import runpy
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import duckdb
import fpa

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUNS = 10  # repetitions per benchmark


def bench(label: str, fn, runs: int = RUNS):
    """Run fn() `runs` times, report median + min wall-clock time."""
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    med = statistics.median(times) * 1000
    mn  = min(times) * 1000
    print(f"  {label:<55}  median={med:7.2f}ms  min={mn:7.2f}ms")
    return result


class QueryCounter:
    """Wraps a DuckDB connection and counts execute() calls."""

    def __init__(self, con):
        self._con = con
        self.count = 0

    def execute(self, sql, params=None):
        self.count += 1
        return self._con.execute(sql, params or [])

    def df(self):
        return self._con.df()

    def fetchone(self):
        return self._con.fetchone()

    def reset(self):
        self.count = 0

    # Delegate everything else to the real connection
    def __getattr__(self, name):
        return getattr(self._con, name)


def count_queries(label: str, fn):
    """Run fn() once, report how many SQL queries were issued."""
    result = fn()
    return result


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

sample_path = Path("sample_data/sample_gl.csv")
if not sample_path.exists():
    print("Generating sample GL data...")
    runpy.run_path("sample_data/generate_sample_gl.py", run_name="__main__")

employee_path = Path("sample_data/sample_employees.csv")
if not employee_path.exists():
    print("Generating sample employee data...")
    runpy.run_path("sample_data/generate_sample_employees.py", run_name="__main__")

_raw_con = duckdb.connect()
_raw_con.execute(f"CREATE TABLE gl AS SELECT * FROM read_csv_auto('{sample_path}')")
_raw_con.execute(f"CREATE TABLE employees AS SELECT * FROM read_csv_auto('{employee_path}')")

gl_rows  = _raw_con.execute("SELECT COUNT(*) FROM gl").fetchone()[0]
print(f"Loaded {gl_rows:,} GL rows into DuckDB.\n")

counter = QueryCounter(_raw_con)

registry = fpa.MeasureRegistry()
registry.register_many([
    fpa.Measure(
        name="Revenue",
        sql="""
            SELECT scenario, account_id, date, entity, description, source,
                   -amount AS amount
            FROM gl WHERE account_id IN ('4000', '4010')
        """,
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        scenario_col="scenario",
    ),
    fpa.Measure(
        name="COGS",
        sql="SELECT * FROM gl WHERE account_id IN ('5000', '5010')",
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        scenario_col="scenario",
    ),
    fpa.Measure(
        name="OpEx",
        sql="SELECT * FROM gl WHERE account_id IN ('6000','6010','6020','6030','6040')",
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        scenario_col="scenario",
    ),
    fpa.Measure(
        name="InterestExpense",
        sql="SELECT * FROM gl WHERE account_id = '7000'",
        value_col="amount",
        date_col="date",
        agg_type=fpa.AggType.SUM,
        scenario_col="scenario",
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
    fpa.Measure(
        name="Operating Income",
        dependencies=["Gross Profit", "OpEx"],
        formula=lambda v: v["Gross Profit"] - v["OpEx"],
    ),
    fpa.Measure(
        name="Net Income",
        dependencies=["Operating Income", "InterestExpense"],
        formula=lambda v: v["Operating Income"] - v["InterestExpense"],
    ),
    fpa.Measure(
        name="Revenue YoY %",
        dependencies=["Revenue"],
        formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100 if v["Revenue", -12] else 0.0,
    ),
])

calendar  = fpa.FiscalCalendar(fiscal_year_start_month=1)
fy2024    = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)
fy2025    = calendar.periods_for_fiscal_year(2025, fpa.Grain.MONTH)
fy2024_q  = calendar.periods_for_fiscal_year(2024, fpa.Grain.QUARTER)

ALL_PL = ["Revenue", "COGS", "Gross Profit", "Gross Margin %",
          "OpEx", "Operating Income", "Net Income"]
ENTITIES = ["North", "South", "West"]

# ---------------------------------------------------------------------------
# Benchmark suite
# ---------------------------------------------------------------------------

print("=" * 80)
print("BENCHMARK SUITE — baseline before architecture changes")
print("=" * 80)

# -- 1. build_table: full P&L, 12 months -----------------------------------------
print("\n[1] build_table — full P&L (7 measures × 12 months)")

def make_calc():
    return fpa.Calculator(registry, connection=counter, calendar=calendar)

counter.reset()
calc = make_calc()
bench("cold (first call, no cache)",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_table(ALL_PL, fy2024, scenario="Actual"))

calc = make_calc()
calc.build_table(ALL_PL, fy2024, scenario="Actual")  # warm cache
bench("warm (memo populated)",
      lambda: calc.build_table(ALL_PL, fy2024, scenario="Actual"))

counter.reset()
calc = make_calc()
calc.build_table(ALL_PL, fy2024, scenario="Actual")
print(f"  {'SQL queries issued':<55}  count={counter.count}")

# -- 2. build_table: quarterly P&L -----------------------------------------------
print("\n[2] build_table — full P&L (7 measures × 4 quarters)")
bench("quarterly P&L",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_table(ALL_PL, fy2024_q, scenario="Actual"))

# -- 3. build_breakdown_table: revenue by entity, 12 months ----------------------
print("\n[3] build_breakdown_table — Revenue × entity (3 values explicit, 12 months)")
bench("explicit dimension_values",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_breakdown_table(
                  "Revenue", fy2024, scenario="Actual",
                  dimensions="entity", dimension_values=ENTITIES))

bench("no dimension_values (DuckDB enumerates)",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_breakdown_table(
                  "Revenue", fy2024, scenario="Actual",
                  dimensions="entity"))

# -- 4. build_breakdown_table: derived measure (Gross Margin %) ------------------
print("\n[4] build_breakdown_table — Gross Margin % × entity (derived, 12 months)")
bench("Gross Margin % by entity",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_breakdown_table(
                  "Gross Margin %", fy2024, scenario="Actual",
                  dimensions="entity"))

# -- 5. Time-shifted YoY: Revenue YoY % across FY2025 ---------------------------
print("\n[5] build_table — Revenue YoY % (time-shifted lookup, 12 months)")
counter.reset()
bench("Revenue YoY % FY2025 vs FY2024",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_table(["Revenue", "Revenue YoY %"], fy2025, scenario="Budget"))
calc_yoy = fpa.Calculator(registry, connection=counter, calendar=calendar)
calc_yoy.build_table(["Revenue", "Revenue YoY %"], fy2025, scenario="Budget")
print(f"  {'SQL queries issued':<55}  count={counter.count}")

# -- 6. Single resolve() call ----------------------------------------------------
print("\n[6] resolve() — single measure × single period")
jan = calendar.month_period(__import__("datetime").date(2024, 1, 1))
ctx = fpa.CalculationContext.make(period=jan, scenario="Actual")
calc_single = make_calc()
bench("Revenue (cold)",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .resolve("Revenue", ctx))
bench("Net Income (cold, 4 deps)",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .resolve("Net Income", ctx))

# -- 7. Many measures sharing a base: simulate future CTE benefit ----------------
print("\n[7] build_table — 4 independent base measures × 12 months")
print("    (simulates current cost of separate scans per base measure)")
counter.reset()
calc_base = make_calc()
calc_base.build_table(["Revenue", "COGS", "OpEx", "InterestExpense"], fy2024, scenario="Actual")
print(f"  {'SQL queries issued (4 base measures)':<55}  count={counter.count}")
bench("4 base measures × 12 months",
      lambda: fpa.Calculator(registry, connection=counter, calendar=calendar)
              .build_table(["Revenue", "COGS", "OpEx", "InterestExpense"],
                           fy2024, scenario="Actual"))

print("\n" + "=" * 80)
print("Done. Save this output as your baseline.")
print("=" * 80)
