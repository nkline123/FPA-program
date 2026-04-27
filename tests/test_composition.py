"""
Tests for SQL measure.X composition — CTE chaining, metadata inheritance,
multi-level chains, and interaction with Python formula measures.
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
            scenario   VARCHAR,
            account_id VARCHAR,
            date       DATE,
            entity     VARCHAR,
            department VARCHAR,
            amount     DOUBLE
        )
    """)
    c.executemany("INSERT INTO gl VALUES (?, ?, ?, ?, ?, ?)", [
        # Actual, Jan 2024
        ("Actual", "6000", date(2024, 1, 15), "North", "Sales",     500.0),
        ("Actual", "6000", date(2024, 1, 15), "North", "Marketing", 300.0),
        ("Actual", "6000", date(2024, 1, 15), "North", "R&D",       400.0),
        ("Actual", "6000", date(2024, 1, 15), "South", "Sales",     200.0),
        ("Actual", "6000", date(2024, 1, 15), "South", "Marketing", 100.0),
        ("Actual", "6000", date(2024, 1, 15), "South", "R&D",       150.0),
        # Actual, Feb 2024
        ("Actual", "6000", date(2024, 2, 10), "North", "Sales",     600.0),
        ("Actual", "6000", date(2024, 2, 10), "North", "Marketing", 350.0),
        ("Actual", "6000", date(2024, 2, 10), "North", "R&D",       420.0),
        # Budget, Jan 2024
        ("Budget", "6000", date(2024, 1, 15), "North", "Sales",     550.0),
    ])
    yield c
    c.close()


@pytest.fixture
def calendar():
    return FiscalCalendar(fiscal_year_start_month=1)


def jan(cal):
    return cal.month_period(date(2024, 1, 1))


def feb(cal):
    return cal.month_period(date(2024, 2, 1))


def base_registry():
    r = MeasureRegistry()
    r.register_many([
        Measure(
            name="Expense",
            sql="SELECT * FROM gl WHERE account_id = '6000'",
            value_col="amount",
            date_col="date",
            agg_type=AggType.SUM,
            scenario_col="scenario",
        ),
        Measure(
            name="SalesExpense",
            sql="SELECT * FROM measure.Expense WHERE department = 'Sales'",
        ),
        Measure(
            name="MarketingExpense",
            sql="SELECT * FROM measure.Expense WHERE department = 'Marketing'",
        ),
    ])
    return r


# ---------------------------------------------------------------------------
# Correctness vs. equivalent direct SQL
# ---------------------------------------------------------------------------

class TestCompositionCorrectness:
    def test_composed_matches_direct_sql(self, con, calendar):
        """Composed measure returns same value as an equivalent direct leaf."""
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="SalesExpense",
                sql="SELECT * FROM measure.Expense WHERE department = 'Sales'",
            ),
            Measure(
                name="SalesExpenseDirect",
                sql="SELECT * FROM gl WHERE account_id = '6000' AND department = 'Sales'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
        ])
        calc = Calculator(r, connection=con)
        periods = [jan(calendar)]
        tbl = calc.build_table(
            ["SalesExpense", "SalesExpenseDirect"], periods, scenario="Actual"
        )
        assert tbl.loc["SalesExpense", "Jan 2024"] == pytest.approx(
            tbl.loc["SalesExpenseDirect", "Jan 2024"]
        )

    def test_composed_value_with_entity_filter(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        periods = [jan(calendar)]
        tbl = calc.build_table(
            ["SalesExpense"], periods, scenario="Actual", entity="North"
        )
        assert tbl.loc["SalesExpense", "Jan 2024"] == pytest.approx(500.0)

    def test_composed_value_no_filter_sums_all_entities(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        periods = [jan(calendar)]
        tbl = calc.build_table(["SalesExpense"], periods, scenario="Actual")
        # North Sales=500, South Sales=200
        assert tbl.loc["SalesExpense", "Jan 2024"] == pytest.approx(700.0)

    def test_composed_across_two_periods(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        periods = [jan(calendar), feb(calendar)]
        tbl = calc.build_table(
            ["SalesExpense"], periods, scenario="Actual", entity="North"
        )
        assert tbl.loc["SalesExpense", "Jan 2024"] == pytest.approx(500.0)
        assert tbl.loc["SalesExpense", "Feb 2024"] == pytest.approx(600.0)

    def test_composed_scenario_filter(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        periods = [jan(calendar)]
        tbl = calc.build_table(
            ["SalesExpense"], periods, scenario="Budget", entity="North"
        )
        assert tbl.loc["SalesExpense", "Jan 2024"] == pytest.approx(550.0)

    def test_two_composed_siblings_correct(self, con, calendar):
        """Two composed measures on the same leaf return independent correct values."""
        calc = Calculator(base_registry(), connection=con)
        periods = [jan(calendar)]
        tbl = calc.build_table(
            ["SalesExpense", "MarketingExpense"], periods,
            scenario="Actual", entity="North"
        )
        assert tbl.loc["SalesExpense",     "Jan 2024"] == pytest.approx(300.0 + 200.0)  # wait, entity=North
        assert tbl.loc["SalesExpense",     "Jan 2024"] == pytest.approx(500.0)
        assert tbl.loc["MarketingExpense", "Jan 2024"] == pytest.approx(300.0)

    def test_composed_no_data_returns_zero(self, con, calendar):
        """Period with no matching rows returns 0.0, not NULL."""
        calc = Calculator(base_registry(), connection=con)
        mar = calendar.month_period(date(2024, 3, 1))
        tbl = calc.build_table(["SalesExpense"], [mar], scenario="Actual")
        assert tbl.loc["SalesExpense", "Mar 2024"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Metadata inheritance
# ---------------------------------------------------------------------------

class TestMetadataInheritance:
    def test_inherits_value_col(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        vc, _, _ = calc._resolve_metadata("SalesExpense")
        assert vc == "amount"

    def test_inherits_agg_type(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        _, _, at = calc._resolve_metadata("SalesExpense")
        assert at == AggType.SUM

    def test_inherits_date_col(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="txn_date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="SalesExpense",
                sql="SELECT * FROM measure.Expense WHERE department = 'Sales'",
            ),
        ])
        calc = Calculator(r, connection=con)
        _, dc, _ = calc._resolve_metadata("SalesExpense")
        assert dc == "txn_date"

    def test_composed_overrides_agg_type(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="AvgExpense",
                sql="SELECT * FROM measure.Expense WHERE department = 'Sales'",
                agg_type=AggType.AVERAGE,
                scenario_col="scenario",
            ),
        ])
        calc = Calculator(r, connection=con)
        _, _, at = calc._resolve_metadata("AvgExpense")
        assert at == AggType.AVERAGE

    def test_three_level_inherits_from_leaf(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="NorthExpense",
                sql="SELECT * FROM measure.Expense WHERE entity = 'North'",
            ),
            Measure(
                name="NorthSalesExpense",
                sql="SELECT * FROM measure.NorthExpense WHERE department = 'Sales'",
            ),
        ])
        calc = Calculator(r, connection=con)
        vc, dc, at = calc._resolve_metadata("NorthSalesExpense")
        assert vc == "amount"
        assert dc == "date"
        assert at == AggType.SUM


# ---------------------------------------------------------------------------
# Multi-level CTE chains
# ---------------------------------------------------------------------------

class TestMultiLevelChain:
    def test_two_level_value(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="NorthExpense",
                sql="SELECT * FROM measure.Expense WHERE entity = 'North'",
            ),
            Measure(
                name="NorthSalesExpense",
                sql="SELECT * FROM measure.NorthExpense WHERE department = 'Sales'",
            ),
        ])
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(
            ["NorthSalesExpense"], [jan(calendar)], scenario="Actual"
        )
        assert tbl.loc["NorthSalesExpense", "Jan 2024"] == pytest.approx(500.0)

    def test_two_level_ancestors_ordered(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="NorthExpense",
                sql="SELECT * FROM measure.Expense WHERE entity = 'North'",
            ),
            Measure(
                name="NorthSalesExpense",
                sql="SELECT * FROM measure.NorthExpense WHERE department = 'Sales'",
            ),
        ])
        calc = Calculator(r, connection=con)
        ancestors = calc._sql_ancestors_ordered("NorthSalesExpense")
        assert ancestors == ["Expense", "NorthExpense", "NorthSalesExpense"]

    def test_two_level_breakdown(self, con, calendar):
        r = MeasureRegistry()
        r.register_many([
            Measure(
                name="Expense",
                sql="SELECT * FROM gl WHERE account_id = '6000'",
                value_col="amount", date_col="date", agg_type=AggType.SUM,
 scenario_col="scenario",
            ),
            Measure(
                name="SalesExpense",
                sql="SELECT * FROM measure.Expense WHERE department = 'Sales'",
            ),
            Measure(
                name="NorthSalesExpense",
                sql="SELECT * FROM measure.SalesExpense WHERE entity = 'North'",
            ),
        ])
        calc = Calculator(r, connection=con)
        # NorthSalesExpense collapses to a scalar — breakdown by a different dim
        tbl = calc.build_table(
            ["NorthSalesExpense"], [jan(calendar)], scenario="Actual"
        )
        assert tbl.loc["NorthSalesExpense", "Jan 2024"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Breakdown table with composed measures
# ---------------------------------------------------------------------------

class TestCompositionBreakdown:
    def test_breakdown_by_entity(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "SalesExpense", [jan(calendar)], scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(500.0)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(200.0)

    def test_breakdown_no_dimension_values(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "SalesExpense", [jan(calendar)], scenario="Actual",
            dimension="entity",
        )
        assert set(tbl.index) == {"North", "South"}

    def test_breakdown_missing_entity_returns_zero(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "SalesExpense", [jan(calendar)], scenario="Actual",
            dimension="entity", dimension_values=["North", "East"],
        )
        assert tbl.loc["East", "Jan 2024"] == pytest.approx(0.0)

    def test_breakdown_multiple_periods(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "SalesExpense", [jan(calendar), feb(calendar)], scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(500.0)
        assert tbl.loc["North", "Feb 2024"] == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# Python formula depending on composed SQL measure
# ---------------------------------------------------------------------------

class TestComposedWithFormula:
    def test_formula_sums_two_composed_measures(self, con, calendar):
        r = base_registry()
        r.register(Measure(
            name="SalesAndMarketing",
            dependencies=["SalesExpense", "MarketingExpense"],
            formula=lambda v: v["SalesExpense"] + v["MarketingExpense"],
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(
            ["SalesAndMarketing"], [jan(calendar)], scenario="Actual", entity="North"
        )
        assert tbl.loc["SalesAndMarketing", "Jan 2024"] == pytest.approx(800.0)

    def test_formula_ratio_composed_over_leaf(self, con, calendar):
        r = base_registry()
        r.register(Measure(
            name="SalesShare",
            dependencies=["SalesExpense", "Expense"],
            formula=lambda v: v["SalesExpense"] / v["Expense"] if v["Expense"] else 0.0,
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_table(
            ["SalesShare"], [jan(calendar)], scenario="Actual", entity="North"
        )
        # North: Sales=500, Total=1200
        assert tbl.loc["SalesShare", "Jan 2024"] == pytest.approx(500.0 / 1200.0)

    def test_formula_breakdown_depends_on_composed(self, con, calendar):
        r = base_registry()
        r.register(Measure(
            name="SalesAndMarketing",
            dependencies=["SalesExpense", "MarketingExpense"],
            formula=lambda v: v["SalesExpense"] + v["MarketingExpense"],
        ))
        calc = Calculator(r, connection=con)
        tbl = calc.build_breakdown_table(
            "SalesAndMarketing", [jan(calendar)], scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(800.0)   # 500+300
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(300.0)   # 200+100


# ---------------------------------------------------------------------------
# _sql_names_to_fetch optimisation
# ---------------------------------------------------------------------------

class TestSqlNamesToFetch:
    def test_excludes_pure_sql_dep(self, con, calendar):
        calc = Calculator(base_registry(), connection=con)
        all_needed = calc._measures_needed(["SalesExpense"])
        fetch = calc._sql_names_to_fetch(all_needed)
        assert "Expense" not in fetch
        assert "SalesExpense" in fetch

    def test_keeps_dep_needed_by_formula(self, con, calendar):
        r = base_registry()
        r.register(Measure(
            name="SalesShare",
            dependencies=["SalesExpense", "Expense"],
            formula=lambda v: v["SalesExpense"] / v["Expense"] if v["Expense"] else 0.0,
        ))
        calc = Calculator(r, connection=con)
        all_needed = calc._measures_needed(["SalesShare"])
        fetch = calc._sql_names_to_fetch(all_needed)
        assert "Expense" in fetch
        assert "SalesExpense" in fetch

    def test_two_sibling_composed_measures_share_leaf(self, con, calendar):
        """Both sibling composed measures are fetched; their shared leaf is not."""
        calc = Calculator(base_registry(), connection=con)
        all_needed = calc._measures_needed(["SalesExpense", "MarketingExpense"])
        fetch = calc._sql_names_to_fetch(all_needed)
        assert "Expense" not in fetch
        assert "SalesExpense" in fetch
        assert "MarketingExpense" in fetch


# ---------------------------------------------------------------------------
# Resolver fallback with composed measures
# ---------------------------------------------------------------------------

class TestComposedResolverFallback:
    def test_no_connection_raises_on_composed_sql_measure(self, calendar):
        """No resolver + no connection raises RuntimeError on a composed measure."""
        r = base_registry()
        calc = Calculator(r)  # no connection
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual"
        )
        with pytest.raises(RuntimeError, match="no resolver"):
            calc.resolve("SalesExpense", ctx)
