"""
Regression tests for issues identified in the initial code review.

Each test verifies that the fix is in place and the corrected behavior holds.
"""
from __future__ import annotations
from datetime import date
from pathlib import Path

import pytest
import duckdb

from fpa import (
    BaseMeasure, Measure, MeasureRegistry, Calculator,
    CalculationContext, FiscalCalendar, AggType,
)
from fpa.engine.calculator import Calculator as _Calculator


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE gl (
            scenario VARCHAR, account_id VARCHAR,
            date DATE, entity VARCHAR, amount DOUBLE
        )
    """)
    c.executemany("INSERT INTO gl VALUES (?, ?, ?, ?, ?)", [
        ("Actual", "4000", date(2024, 1, 15), "North", 1000.0),
        ("Actual", "4000", date(2024, 1, 20), "South",  500.0),
        ("Actual", "5000", date(2024, 1, 10), "North",  300.0),
        ("Actual", "5000", date(2024, 1, 25), "South",  100.0),
    ])
    c.execute("""
        CREATE TABLE hc (
            scenario VARCHAR, date DATE, entity VARCHAR, headcount DOUBLE
        )
    """)
    c.executemany("INSERT INTO hc VALUES (?, ?, ?, ?)", [
        ("Actual", date(2024, 1, 15), "North", 10.0),
        ("Actual", date(2024, 1, 31), "North", 12.0),
    ])
    yield c
    c.close()


@pytest.fixture
def calendar():
    return FiscalCalendar(fiscal_year_start_month=1)


def make_context(calendar, scenario="Actual", month=(2024, 1), **filters):
    period = calendar.month_period(date(*month, 1))
    return CalculationContext.make(period=period, scenario=scenario, **filters)


# ---------------------------------------------------------------------------
# Issue 1 — _ensure_dag_current compares name-sets, not lengths
# ---------------------------------------------------------------------------

def test_stale_dag_detected_when_measure_replaced_at_same_count(calendar):
    """
    Replacing a measure (same registry length) is detected via name-set diff
    and triggers a DAG rebuild, raising a descriptive ValueError.
    """
    r = MeasureRegistry()
    r.register(BaseMeasure(name="A", resolver=lambda ctx: 1.0))
    r.register(Measure(name="B", dependencies=["A"], formula=lambda v: v["A"] * 2))
    calc = Calculator(r)

    # Swap "A" for "C" — length unchanged, but name-set differs
    r._measures.pop("A")
    r._measures["C"] = BaseMeasure(name="C", resolver=lambda ctx: 5.0)

    with pytest.raises(ValueError, match="not registered"):
        calc.resolve("B", make_context(calendar))


# ---------------------------------------------------------------------------
# Issue 2 — generate_sample_employees.py (was a false alarm — file exists)
# ---------------------------------------------------------------------------

def test_sample_employee_generator_exists():
    assert Path("sample_data/generate_sample_employees.py").exists()


# ---------------------------------------------------------------------------
# Issue 3 — AggType.CALCULATED is rejected at BaseMeasure construction time
# ---------------------------------------------------------------------------

def test_base_measure_rejects_calculated_agg_type():
    with pytest.raises(ValueError, match="CALCULATED"):
        BaseMeasure(
            name="Bad",
            sql="SELECT * FROM gl",
            value_col="amount",
            agg_type=AggType.CALCULATED,
        )


# ---------------------------------------------------------------------------
# Issue 4 — BaseMeasure is frozen (immutable after construction)
# ---------------------------------------------------------------------------

def test_base_measure_is_immutable():
    m = BaseMeasure(name="Revenue", resolver=lambda ctx: 1.0)
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        m.resolver = lambda ctx: 999.0  # type: ignore[misc]


def test_two_base_measures_with_same_name_are_equal_despite_different_sql():
    """Name-based equality is intentional — the registry relies on it for deduplication."""
    m1 = BaseMeasure(name="Revenue", sql="SELECT * FROM gl WHERE account_id='4000'", value_col="amount")
    m2 = BaseMeasure(name="Revenue", sql="SELECT * FROM gl WHERE account_id='9999'", value_col="amount")

    assert m1 == m2
    assert hash(m1) == hash(m2)

    s = {m1, m2}
    assert len(s) == 1  # m2 is deduplicated by name


# ---------------------------------------------------------------------------
# Issue 5 — Dead `table` parameter removed from Calculator.__init__
# ---------------------------------------------------------------------------

def test_table_parameter_raises_type_error(con):
    """Passing the removed `table` kwarg now raises TypeError immediately."""
    r = MeasureRegistry()
    r.register(BaseMeasure(
        name="Revenue",
        sql="SELECT * FROM gl WHERE account_id = '4000'",
        value_col="amount",
        date_col="date",
    ))
    with pytest.raises(TypeError):
        Calculator(r, connection=con, table="does_not_exist")


# ---------------------------------------------------------------------------
# Issue 6 — Empty fpa.scenarios and fpa.config packages deleted
# ---------------------------------------------------------------------------

def test_scenarios_package_deleted():
    with pytest.raises(ModuleNotFoundError):
        import fpa.scenarios  # noqa: F401


def test_config_package_deleted():
    with pytest.raises(ModuleNotFoundError):
        import fpa.config  # noqa: F401


# ---------------------------------------------------------------------------
# Issue 7 — LAST_DAY agg expression is now COALESCE-wrapped (null-safe)
# ---------------------------------------------------------------------------

def test_last_day_agg_expr_returns_zero_with_no_matching_rows(con, calendar):
    """
    All three agg types now return 0.0 (not NULL) directly from their SQL
    expression when no rows match the period filter.
    """
    period = calendar.month_period(date(2024, 3, 1))  # no hc rows in March

    last_day_expr = _Calculator._agg_expr(AggType.LAST_DAY, "headcount", "date", period)
    sum_expr      = _Calculator._agg_expr(AggType.SUM,      "headcount", "date", period)
    avg_expr      = _Calculator._agg_expr(AggType.AVERAGE,  "headcount", "date", period)

    last_day_result = con.execute(f"SELECT {last_day_expr} FROM hc").fetchone()[0]
    sum_result      = con.execute(f"SELECT {sum_expr} FROM hc").fetchone()[0]
    avg_result      = con.execute(f"SELECT {avg_expr} FROM hc").fetchone()[0]

    assert sum_result      == pytest.approx(0.0)
    assert avg_result      == pytest.approx(0.0)
    assert last_day_result == pytest.approx(0.0)
