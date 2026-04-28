"""
Tests for AggType.CUMULATIVE_END and AggType.CUMULATIVE_START.

CUMULATIVE_END  — sum of all transactions up to and including period_end.
                  Closing balance. "What are total assets at end of January?"

CUMULATIVE_START — sum of all transactions strictly before period_start.
                   Opening balance. "What were total assets at start of January?"

Invariant: CUMULATIVE_END - CUMULATIVE_START == SUM for the same period.
"""
from datetime import date
import pytest
import duckdb
from fpa import (
    Measure, MeasureRegistry, Calculator,
    FiscalCalendar, AggType,
)
from fpa.engine.calculator import Calculator as _Calculator


@pytest.fixture
def con():
    """
    GL with asset transactions across three months.

    Jan: +1000 (North), +500 (South)
    Feb: +300 (North), -200 (South)
    Mar: +400 (North)

    Running totals by entity:
        North:  Jan=1000  Feb=1300  Mar=1700
        South:  Jan=500   Feb=300   Mar=300
        Total:  Jan=1500  Feb=1600  Mar=2000
    """
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE gl (
            scenario VARCHAR,
            account_id VARCHAR,
            date DATE,
            entity VARCHAR,
            amount DOUBLE
        )
    """)
    c.executemany("INSERT INTO gl VALUES (?,?,?,?,?)", [
        ("Actual", "1000", date(2024, 1, 15), "North",  1000.0),
        ("Actual", "1000", date(2024, 1, 20), "South",   500.0),
        ("Actual", "1000", date(2024, 2, 10), "North",   300.0),
        ("Actual", "1000", date(2024, 2, 15), "South",  -200.0),
        ("Actual", "1000", date(2024, 3, 5),  "North",   400.0),
    ])
    yield c
    c.close()


@pytest.fixture
def calendar():
    return FiscalCalendar(fiscal_year_start_month=1)


def make_registry(agg_type):
    r = MeasureRegistry()
    r.register(Measure(
        name="Assets",
        sql="SELECT * FROM gl WHERE account_id = '1000'",
        value_col="amount",
        date_col="date",
        agg_type=agg_type,
        scenario_col="scenario",
    ))
    return r


# ---------------------------------------------------------------------------
# _agg_expr — generated SQL is correct
# ---------------------------------------------------------------------------

class TestAggExprGeneration:
    def test_cumulative_end_uses_lte_period_end(self, calendar):
        period = calendar.month_period(date(2024, 2, 1))
        expr = _Calculator._agg_expr(AggType.CUMULATIVE_END, "amount", "date", period)
        assert "<= '2024-02-29'" in expr
        assert "BETWEEN" not in expr

    def test_cumulative_start_uses_lt_period_start(self, calendar):
        period = calendar.month_period(date(2024, 2, 1))
        expr = _Calculator._agg_expr(AggType.CUMULATIVE_START, "amount", "date", period)
        assert "< '2024-02-01'" in expr
        assert "BETWEEN" not in expr

    def test_cumulative_end_is_coalesce_wrapped(self, calendar):
        period = calendar.month_period(date(2024, 2, 1))
        expr = _Calculator._agg_expr(AggType.CUMULATIVE_END, "amount", "date", period)
        assert expr.startswith("COALESCE(")

    def test_cumulative_start_is_coalesce_wrapped(self, calendar):
        period = calendar.month_period(date(2024, 2, 1))
        expr = _Calculator._agg_expr(AggType.CUMULATIVE_START, "amount", "date", period)
        assert expr.startswith("COALESCE(")

    def test_cumulative_end_returns_zero_with_no_rows(self, con, calendar):
        period = calendar.month_period(date(2023, 1, 1))  # before any data
        expr = _Calculator._agg_expr(AggType.CUMULATIVE_END, "amount", "date", period)
        result = con.execute(f"SELECT {expr} FROM gl").fetchone()[0]
        assert result == pytest.approx(0.0)

    def test_cumulative_start_returns_zero_with_no_prior_rows(self, con, calendar):
        period = calendar.month_period(date(2024, 1, 1))  # no rows before Jan
        expr = _Calculator._agg_expr(AggType.CUMULATIVE_START, "amount", "date", period)
        result = con.execute(f"SELECT {expr} FROM gl").fetchone()[0]
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CUMULATIVE_END — closing balance
# ---------------------------------------------------------------------------

class TestCumulativeEnd:
    def test_jan_closing_balance(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_END), connection=con)
        jan = calendar.month_period(date(2024, 1, 1))
        tbl = calc.build_table(["Assets"], [jan], scenario="Actual")
        assert tbl.loc["Assets", "Jan 2024"] == pytest.approx(1500.0)

    def test_feb_closing_balance_includes_jan(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_END), connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        tbl = calc.build_table(["Assets"], [feb], scenario="Actual")
        assert tbl.loc["Assets", "Feb 2024"] == pytest.approx(1600.0)

    def test_mar_closing_balance_includes_all(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_END), connection=con)
        mar = calendar.month_period(date(2024, 3, 1))
        tbl = calc.build_table(["Assets"], [mar], scenario="Actual")
        assert tbl.loc["Assets", "Mar 2024"] == pytest.approx(2000.0)

    def test_balances_increase_across_periods(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_END), connection=con)
        periods = [
            calendar.month_period(date(2024, 1, 1)),
            calendar.month_period(date(2024, 2, 1)),
            calendar.month_period(date(2024, 3, 1)),
        ]
        tbl = calc.build_table(["Assets"], periods, scenario="Actual")
        assert tbl.loc["Assets", "Jan 2024"] < tbl.loc["Assets", "Feb 2024"]
        assert tbl.loc["Assets", "Feb 2024"] < tbl.loc["Assets", "Mar 2024"]

    def test_closing_balance_by_entity(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_END), connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        tbl = calc.build_breakdown_table(
            "Assets", [feb], scenario="Actual",
            dimensions="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Feb 2024"] == pytest.approx(1300.0)  # 1000+300
        assert tbl.loc["South", "Feb 2024"] == pytest.approx(300.0)   # 500-200

    def test_entity_filter(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_END), connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        tbl = calc.build_table(["Assets"], [feb], scenario="Actual", entity="North")
        assert tbl.loc["Assets", "Feb 2024"] == pytest.approx(1300.0)


# ---------------------------------------------------------------------------
# CUMULATIVE_START — opening balance
# ---------------------------------------------------------------------------

class TestCumulativeStart:
    def test_jan_opening_balance_is_zero(self, con, calendar):
        """No transactions before Jan — opening balance is 0."""
        calc = Calculator(make_registry(AggType.CUMULATIVE_START), connection=con)
        jan = calendar.month_period(date(2024, 1, 1))
        tbl = calc.build_table(["Assets"], [jan], scenario="Actual")
        assert tbl.loc["Assets", "Jan 2024"] == pytest.approx(0.0)

    def test_feb_opening_balance_equals_jan_close(self, con, calendar):
        """Opening balance of Feb = closing balance of Jan."""
        calc_end   = Calculator(make_registry(AggType.CUMULATIVE_END),   connection=con)
        calc_start = Calculator(make_registry(AggType.CUMULATIVE_START), connection=con)
        jan = calendar.month_period(date(2024, 1, 1))
        feb = calendar.month_period(date(2024, 2, 1))
        jan_close  = calc_end.build_table(  ["Assets"], [jan], scenario="Actual")
        feb_open   = calc_start.build_table(["Assets"], [feb], scenario="Actual")
        assert jan_close.loc["Assets", "Jan 2024"] == pytest.approx(
            feb_open.loc["Assets", "Feb 2024"]
        )

    def test_mar_opening_balance_equals_feb_close(self, con, calendar):
        calc_end   = Calculator(make_registry(AggType.CUMULATIVE_END),   connection=con)
        calc_start = Calculator(make_registry(AggType.CUMULATIVE_START), connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        mar = calendar.month_period(date(2024, 3, 1))
        feb_close = calc_end.build_table(  ["Assets"], [feb], scenario="Actual")
        mar_open  = calc_start.build_table(["Assets"], [mar], scenario="Actual")
        assert feb_close.loc["Assets", "Feb 2024"] == pytest.approx(
            mar_open.loc["Assets", "Mar 2024"]
        )

    def test_opening_balance_by_entity(self, con, calendar):
        calc = Calculator(make_registry(AggType.CUMULATIVE_START), connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        tbl = calc.build_breakdown_table(
            "Assets", [feb], scenario="Actual",
            dimensions="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Feb 2024"] == pytest.approx(1000.0)
        assert tbl.loc["South", "Feb 2024"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Invariant: CUMULATIVE_END - CUMULATIVE_START == SUM
# ---------------------------------------------------------------------------

class TestCumulativeInvariant:
    def test_close_minus_open_equals_sum_jan(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(name="AssetsClose", sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.CUMULATIVE_END,
                    scenario_col="scenario"),
            Measure(name="AssetsOpen",  sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.CUMULATIVE_START,
                    scenario_col="scenario"),
            Measure(name="AssetsFlow",  sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.SUM,
                    scenario_col="scenario"),
            Measure(name="NetChange",
                    dependencies=["AssetsClose", "AssetsOpen"],
                    formula=lambda v: v["AssetsClose"] - v["AssetsOpen"]),
        ])
        calc = Calculator(r, connection=con)
        periods = [
            calendar.month_period(date(2024, 1, 1)),
            calendar.month_period(date(2024, 2, 1)),
            calendar.month_period(date(2024, 3, 1)),
        ]
        tbl = calc.build_table(["NetChange", "AssetsFlow"], periods, scenario="Actual")
        for period in periods:
            assert tbl.loc["NetChange", period.label] == pytest.approx(
                tbl.loc["AssetsFlow", period.label], rel=1e-6
            ), f"Invariant failed for {period.label}"

    def test_invariant_holds_by_entity(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(name="Close", sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.CUMULATIVE_END,
                    scenario_col="scenario"),
            Measure(name="Open",  sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.CUMULATIVE_START,
                    scenario_col="scenario"),
            Measure(name="Flow",  sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.SUM,
                    scenario_col="scenario"),
            Measure(name="NetChange",
                    dependencies=["Close", "Open"],
                    formula=lambda v: v["Close"] - v["Open"]),
        ])
        calc = Calculator(r, connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        net = calc.build_breakdown_table("NetChange", [feb], scenario="Actual",
                                         dimensions="entity", dimension_values=["North", "South"])
        flow = calc.build_breakdown_table("Flow", [feb], scenario="Actual",
                                          dimensions="entity", dimension_values=["North", "South"])
        for entity in ["North", "South"]:
            assert net.loc[entity, "Feb 2024"] == pytest.approx(
                flow.loc[entity, "Feb 2024"], rel=1e-6
            )


# ---------------------------------------------------------------------------
# CUMULATIVE measures work with SQL composition
# ---------------------------------------------------------------------------

class TestCumulativeWithComposition:
    def test_composed_measure_inherits_cumulative_end(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(name="Assets",
                    sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.CUMULATIVE_END,
                    scenario_col="scenario"),
            Measure(name="NorthAssets",
                    sql="SELECT * FROM measure.Assets WHERE entity = 'North'"),
        ])
        calc = Calculator(r, connection=con)
        _, _, at = calc._resolve_metadata("NorthAssets")
        assert at == AggType.CUMULATIVE_END

    def test_composed_cumulative_correct_value(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(name="Assets",
                    sql="SELECT * FROM gl WHERE account_id = '1000'",
                    value_col="amount", date_col="date", agg_type=AggType.CUMULATIVE_END,
                    scenario_col="scenario"),
            Measure(name="NorthAssets",
                    sql="SELECT * FROM measure.Assets WHERE entity = 'North'"),
        ])
        calc = Calculator(r, connection=con)
        feb = calendar.month_period(date(2024, 2, 1))
        tbl = calc.build_table(["NorthAssets"], [feb], scenario="Actual")
        assert tbl.loc["NorthAssets", "Feb 2024"] == pytest.approx(1300.0)
