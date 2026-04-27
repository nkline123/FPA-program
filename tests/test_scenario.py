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
            scenario_col="scenario",
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
            scenario_col="scenario",
        ))
        calc = Calculator(r, connection=con)
        actual = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual")
        budget = calc.build_table(["Revenue"], [jan(calendar)], scenario="Budget")
        assert actual.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)
        assert budget.loc["Revenue", "Jan 2024"] == pytest.approx(1700.0)


# ---------------------------------------------------------------------------
# Measure-level scenario field
# Use scenario= when the source data has no scenario column — the SQL
# pre-scopes the data and scenario= is just a label; no WHERE is injected.
# ---------------------------------------------------------------------------

class TestMeasureScenarioField:
    def test_measure_scenario_no_where_injected(self, con, calendar):
        """Measure with scenario= does not inject a scenario WHERE clause."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["ActualRevenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_measure_scenario_call_scenario_irrelevant(self, con, calendar):
        """Measure with scenario= returns the same data regardless of call scenario."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        actual_call = calc.build_table(["ActualRevenue"], [jan(calendar)], scenario="Actual")
        budget_call = calc.build_table(["ActualRevenue"], [jan(calendar)], scenario="Budget")
        assert actual_call.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1500.0)
        assert budget_call.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_measure_scenario_budget(self, con, calendar):
        """scenario='Budget' on a pre-filtered measure returns Budget values."""
        r = MeasureRegistry()
        r.register(Measure(
            name="BudgetRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Budget' AND account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Budget",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["BudgetRevenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["BudgetRevenue", "Jan 2024"] == pytest.approx(1700.0)

    def test_measure_scenarios_coexist(self, con, calendar):
        """Actual and Budget measures with scenario= can coexist in one build_table call."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        r.register(Measure(
            name="BudgetRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Budget' AND account_id = '4000'",
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
        """Call-level filters are still applied alongside scenario= measures."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(
            ["ActualRevenue"], [jan(calendar)], scenario="Actual", entity="North"
        )
        assert tbl.loc["ActualRevenue", "Jan 2024"] == pytest.approx(1000.0)

    def test_measure_scenario_resolve(self, con, calendar):
        """resolve() works correctly for a measure with scenario=."""
        r = MeasureRegistry()
        r.register(Measure(
            name="ActualRevenue",
            sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario="Actual",
        ))
        calc = Calculator(r, connection=con)
        ctx = CalculationContext.make(period=jan(calendar), scenario="Actual")
        assert calc.resolve("ActualRevenue", ctx) == pytest.approx(1500.0)

    def test_measure_no_scenario_uses_call_scenario(self, con, calendar):
        """Measure with scenario_col= respects the call-level scenario."""
        r = MeasureRegistry()
        r.register(Measure(
            name="Revenue",
            sql="SELECT * FROM gl WHERE account_id = '4000'",
            value_col="amount", date_col="date", agg_type=AggType.SUM,
            scenario_col="scenario",
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual")
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_cannot_set_both_scenario_col_and_scenario(self):
        """Setting both scenario_col and scenario raises a ValueError."""
        with pytest.raises(ValueError, match="cannot set both"):
            Measure(
                name="Bad",
                sql="SELECT * FROM gl",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
                scenario_col="scenario",
                scenario="Actual",
            )
