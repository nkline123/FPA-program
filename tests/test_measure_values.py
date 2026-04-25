"""
Tests for MeasureValues and time-shifted measure resolution.

MeasureValues is passed to every Measure formula.  Plain string keys return
the pre-computed dependency value for the current period.  Tuple keys shift
to a different period and re-resolve via the Calculator.
"""
from datetime import date
import pytest
import duckdb
from fpa import (
    BaseMeasure, Measure, MeasureRegistry, Calculator,
    CalculationContext, FiscalCalendar, Grain, AggType, MeasureValues,
)


@pytest.fixture
def calendar():
    return FiscalCalendar(fiscal_year_start_month=1)


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE gl (
            scenario VARCHAR,
            date     DATE,
            amount   DOUBLE
        )
    """)
    c.executemany("INSERT INTO gl VALUES (?, ?, ?)", [
        ("Actual", date(2023, 1, 15), 800.0),
        ("Actual", date(2024, 1, 15), 1000.0),
        ("Actual", date(2024, 2, 15), 1200.0),
    ])
    yield c
    c.close()


def make_ctx(calendar, year=2024, month=1, scenario="Actual"):
    period = calendar.month_period(date(year, month, 1))
    return CalculationContext.make(period=period, scenario=scenario)


# ---------------------------------------------------------------------------
# MeasureValues — plain key lookup
# ---------------------------------------------------------------------------

class TestMeasureValuesPlainKey:
    def test_plain_key_returns_dep_value(self, calendar):
        ctx = make_ctx(calendar)
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Revenue", resolver=lambda c: 500.0))
        calc = Calculator(r, calendar=calendar)
        mv = MeasureValues({"Revenue": 500.0}, calc, ctx)
        assert mv["Revenue"] == 500.0

    def test_plain_key_missing_raises_key_error(self, calendar):
        ctx = make_ctx(calendar)
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Revenue", resolver=lambda c: 0.0))
        calc = Calculator(r, calendar=calendar)
        mv = MeasureValues({"Revenue": 0.0}, calc, ctx)
        with pytest.raises(KeyError):
            _ = mv["COGS"]

    def test_period_property(self, calendar):
        ctx = make_ctx(calendar, year=2024, month=3)
        mv = MeasureValues({}, Calculator(MeasureRegistry()), ctx)
        assert mv.period.label == "Mar 2024"

    def test_scenario_property(self, calendar):
        ctx = make_ctx(calendar, scenario="Budget")
        mv = MeasureValues({}, Calculator(MeasureRegistry()), ctx)
        assert mv.scenario == "Budget"

    def test_invalid_tuple_length_raises(self, calendar):
        ctx = make_ctx(calendar)
        mv = MeasureValues({}, Calculator(MeasureRegistry()), ctx)
        with pytest.raises(ValueError, match="Expected"):
            _ = mv["Revenue", -12, Grain.MONTH, "extra"]


# ---------------------------------------------------------------------------
# MeasureValues — time-shifted key (requires calendar on Calculator)
# ---------------------------------------------------------------------------

class TestMeasureValuesTimeShift:
    def test_shift_requires_calendar(self, calendar):
        ctx = make_ctx(calendar)
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Revenue", resolver=lambda c: 0.0))
        calc = Calculator(r)  # no calendar
        mv = MeasureValues({"Revenue": 0.0}, calc, ctx)
        with pytest.raises(RuntimeError, match="FiscalCalendar"):
            _ = mv["Revenue", -1]

    def test_shift_resolves_prior_period(self, calendar):
        """v["Revenue", -1] from Feb returns Jan's value."""
        values = {date(2024, 1, 1): 100.0, date(2024, 2, 1): 200.0}
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: values[ctx.period.start],
        ))
        calc = Calculator(r, calendar=calendar)
        feb_ctx = make_ctx(calendar, month=2)
        mv = MeasureValues({"Revenue": 200.0}, calc, feb_ctx)
        assert mv["Revenue", -1] == 100.0

    def test_shift_resolves_prior_year(self, calendar):
        """v["Revenue", -12] from Jan 2024 returns Jan 2023's value."""
        values = {date(2023, 1, 1): 800.0, date(2024, 1, 1): 1000.0}
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: values.get(ctx.period.start, 0.0),
        ))
        calc = Calculator(r, calendar=calendar)
        ctx = make_ctx(calendar, year=2024, month=1)
        mv = MeasureValues({"Revenue": 1000.0}, calc, ctx)
        assert mv["Revenue", -12] == 800.0

    def test_shift_cross_grain(self, calendar):
        """v["Revenue", 1, Grain.MONTH] from a quarterly period returns month 2."""
        values = {
            date(2024, 4, 1): 400.0,
            date(2024, 5, 1): 500.0,
            date(2024, 6, 1): 600.0,
        }
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: values.get(ctx.period.start, 0.0),
        ))
        calc = Calculator(r, calendar=calendar)
        q2_ctx = CalculationContext.make(
            period=calendar.quarter_period(date(2024, 4, 1)),
            scenario="Actual",
        )
        mv = MeasureValues({"Revenue": 1500.0}, calc, q2_ctx)
        assert mv["Revenue", 1, Grain.MONTH] == 500.0   # May


# ---------------------------------------------------------------------------
# Time-shifted formulas via Calculator (Python path)
# ---------------------------------------------------------------------------

class TestTimeShiftedFormulaPython:
    def test_yoy_growth_formula(self, calendar):
        values = {
            (2023, 1): 800.0,
            (2024, 1): 1000.0,
        }
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: values.get(
                (ctx.period.start.year, ctx.period.start.month), 0.0
            ),
        ))
        r.register(Measure(
            name="Revenue YoY %",
            dependencies=["Revenue"],
            formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100
                              if v["Revenue", -12] else 0.0,
        ))
        calc = Calculator(r, calendar=calendar)
        ctx = make_ctx(calendar, year=2024, month=1)
        result = calc.resolve("Revenue YoY %", ctx)
        assert result == pytest.approx(25.0)  # (1000/800 - 1) * 100

    def test_yoy_zero_prior_returns_zero(self, calendar):
        """Guard condition: if prior-year value is 0, formula returns 0.0."""
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Revenue", resolver=lambda ctx: 0.0))
        r.register(Measure(
            name="Revenue YoY %",
            dependencies=["Revenue"],
            formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100
                              if v["Revenue", -12] else 0.0,
        ))
        calc = Calculator(r, calendar=calendar)
        result = calc.resolve("Revenue YoY %", make_ctx(calendar))
        assert result == 0.0

    def test_build_table_with_time_shift(self, calendar):
        """build_table produces correct YoY values across multiple periods."""
        values = {
            (2023, 1): 500.0, (2023, 2): 600.0,
            (2024, 1): 1000.0, (2024, 2): 900.0,
        }
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: values.get(
                (ctx.period.start.year, ctx.period.start.month), 0.0
            ),
        ))
        r.register(Measure(
            name="YoY %",
            dependencies=["Revenue"],
            formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100
                              if v["Revenue", -12] else 0.0,
        ))
        calc = Calculator(r, calendar=calendar)
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 2, 29))
        tbl = calc.build_table(["YoY %"], periods, scenario="Actual")
        assert tbl.loc["YoY %", "Jan 2024"] == pytest.approx(100.0)  # (1000/500-1)*100
        assert tbl.loc["YoY %", "Feb 2024"] == pytest.approx(50.0)   # (900/600-1)*100


# ---------------------------------------------------------------------------
# Time-shifted formulas via Calculator (DuckDB path)
# ---------------------------------------------------------------------------

class TestTimeShiftedFormulaDuckDB:
    def test_yoy_growth_duckdb_path(self, con, calendar):
        """Time-shifted lookup on the DuckDB path executes a scalar query for the
        prior-year period and memoizes the result."""
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            sql="SELECT * FROM gl",
            value_col="amount",
            date_col="date",
            agg_type=AggType.SUM,
        ))
        r.register(Measure(
            name="Revenue YoY %",
            dependencies=["Revenue"],
            formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100
                              if v["Revenue", -12] else 0.0,
        ))
        calc = Calculator(r, connection=con, calendar=calendar)
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_table(["Revenue", "Revenue YoY %"], periods, scenario="Actual")
        # Jan 2024 = 1000, Jan 2023 = 800 → (1000/800 - 1) * 100 = 25%
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1000.0)
        assert tbl.loc["Revenue YoY %", "Jan 2024"] == pytest.approx(25.0)

    def test_time_shift_result_is_memoized(self, con, calendar):
        """After resolving a time-shifted formula, the prior-year Revenue entry
        must be in the memo cache — proving the scalar DuckDB result was stored."""
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            sql="SELECT * FROM gl",
            value_col="amount",
            date_col="date",
            agg_type=AggType.SUM,
        ))
        r.register(Measure(
            name="YoY %",
            dependencies=["Revenue"],
            formula=lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100
                              if v["Revenue", -12] else 0.0,
        ))
        calc = Calculator(r, connection=con, calendar=calendar)
        ctx = make_ctx(calendar, year=2024, month=1)
        calc.resolve("YoY %", ctx)

        # The prior-year Revenue context should be in the memo
        jan_2023 = calendar.month_period(date(2023, 1, 1))
        prior_ctx = CalculationContext.make(period=jan_2023, scenario="Actual")
        assert ("Revenue", prior_ctx) in calc._memo
        assert calc._memo[("Revenue", prior_ctx)] == pytest.approx(800.0)
