"""
Tests for scenario filtering.

Two supported patterns:
  1. Scenario at call time (default) — engine injects WHERE "scenario" = ?
  2. Scenario locked on the Measure — set scenario="Actual" on the Measure;
     overrides whatever is passed to build_table for that specific measure.
"""
from datetime import date
import pytest
import duckdb
from fpa import (
    Measure, MeasureRegistry, Calculator,
    CalculationContext, FiscalCalendar, AggType,
)


@pytest.fixture
def con():
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
        ("Actual", "4000", date(2024, 1, 15), "North", 1000.0),
        ("Actual", "4000", date(2024, 1, 15), "South",  500.0),
        ("Budget", "4000", date(2024, 1, 15), "North", 1100.0),
        ("Budget", "4000", date(2024, 1, 15), "South",  600.0),
    ])
    yield c
    c.close()


@pytest.fixture
def calendar():
    return FiscalCalendar(fiscal_year_start_month=1)


def jan(cal):
    return cal.month_period(date(2024, 1, 1))


# ---------------------------------------------------------------------------
# Scenario at call time (default behaviour)
# ---------------------------------------------------------------------------

class TestScenarioColumn:
    def test_scenario_column_filters_correctly(self, con, calendar):
        r = MeasureRegistry()
        r.register(Measure(
            name="Revenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_different_scenarios_return_different_values(self, con, calendar):
        r = MeasureRegistry()
        r.register(Measure(
            name="Revenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
        ))
        calc = Calculator(r, connection=con)
        actual = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual")
        budget = calc.build_table(["Revenue"], [jan(calendar)], scenario="Budget")
        assert actual.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)
        assert budget.loc["Revenue", "Jan 2024"] == pytest.approx(1700.0)


# ---------------------------------------------------------------------------
# Measure-level scenario field
# ---------------------------------------------------------------------------

class TestMeasureScenarioField:
    def test_measure_scenario_filters_correctly(self, con, calendar):
        """Measure with scenario= always returns that scenario's data."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["ActualRevenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_measure_scenario_overrides_call_scenario(self, con, calendar):
        """Measure locked to Actual returns Actual even when call asks for Budget."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["ActualRevenue"], [jan(calendar)], scenario="Budget")
        assert tbl.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_measure_scenario_budget(self, con, calendar):
        """scenario='Budget' on Measure returns Budget values."""
        r = MeasureRegistry()
        r.register(Measure(
            name="BudgetRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Budget",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["BudgetRevenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["BudgetRevenue", "Jan 2024"] == pytest.approx(1700.0)

    def test_measure_scenarios_coexist(self, con, calendar):
        """Actual and Budget measures can coexist in the same build_table call."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        r.register(Measure(
            name="BudgetRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Budget",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(
            ["ActualRevenue", "BudgetRevenue"], [jan(calendar)], scenario="Actual"
        )
        assert tbl.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1500.0)
        assert tbl.loc["BudgetRevenue", "Jan 2024"] == pytest.approx(1700.0)

    def test_measure_scenario_with_entity_filter(self, con, calendar):
        """Measure-level scenario and call-level entity filter are ANDed."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(
            ["ActualRevenue"], [jan(calendar)], scenario="Actual", entity="North"
        )
        assert tbl.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1000.0)

    def test_measure_scenario_resolve(self, con, calendar):
        """resolve() respects measure-level scenario."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        ctx = CalculationContext.make(period=jan(calendar), scenario="Actual")
        assert calc.resolve("ActualRevenue", ctx) == pytest.approx(1500.0)

    def test_measure_no_scenario_uses_call_scenario(self, con, calendar):
        """Measure without scenario= still respects the call-level scenario."""
        r = MeasureRegistry()
        r.register(Measure(
            name="Revenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)
