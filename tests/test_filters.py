"""
Tests for the IN-clause filter support — list/tuple filter values,
CalculationContext normalisation, and interaction with all execution paths.
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
            region VARCHAR,
            amount DOUBLE
        )
    """)
    c.executemany("INSERT INTO gl VALUES (?,?,?,?,?,?)", [
        # Jan 2024
        ("Actual", "4000", date(2024, 1, 15), "North", "East",  1000.0),
        ("Actual", "4000", date(2024, 1, 15), "South", "East",   500.0),
        ("Actual", "4000", date(2024, 1, 15), "West",  "West",   300.0),
        ("Actual", "4000", date(2024, 1, 15), "East",  "East",   200.0),
        # Feb 2024
        ("Actual", "4000", date(2024, 2, 10), "North", "East",   800.0),
        ("Actual", "4000", date(2024, 2, 10), "West",  "West",   400.0),
        # Budget, Jan 2024
        ("Budget", "4000", date(2024, 1, 15), "North", "East",  1100.0),
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


def make_registry():
    r = MeasureRegistry()
    r.register(Measure(
        name="Revenue",
        sql="SELECT * FROM gl WHERE account_id = '4000'",
        value_col="amount", date_col="date", agg_type=AggType.SUM,
    ))
    return r


# ---------------------------------------------------------------------------
# CalculationContext normalisation
# ---------------------------------------------------------------------------

class TestContextNormalisation:
    def test_list_converted_to_tuple(self, calendar):
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        assert isinstance(ctx.get("entity"), tuple)
        assert ctx.get("entity") == ("North", "West")

    def test_scalar_unchanged(self, calendar):
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual", entity="North"
        )
        assert ctx.get("entity") == "North"

    def test_tuple_passthrough(self, calendar):
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=("North", "West"),
        )
        assert ctx.get("entity") == ("North", "West")

    def test_hashable_with_list_filter(self, calendar):
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        assert {ctx: 1}[ctx] == 1

    def test_same_list_produces_equal_contexts(self, calendar):
        ctx1 = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "West"])
        ctx2 = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "West"])
        assert ctx1 == ctx2
        assert hash(ctx1) == hash(ctx2)

    def test_different_list_produces_different_context(self, calendar):
        ctx1 = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "West"])
        ctx2 = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "East"])
        assert ctx1 != ctx2

    def test_list_and_scalar_with_same_single_value_are_not_equal(self, calendar):
        ctx_scalar = CalculationContext.make(period=jan(calendar), scenario="Actual", entity="North")
        ctx_list   = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North"])
        assert ctx_scalar != ctx_list

    def test_list_order_matters(self, calendar):
        ctx1 = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "West"])
        ctx2 = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["West", "North"])
        assert ctx1 != ctx2

    def test_usable_as_memo_key(self, calendar):
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        memo = {ctx: 42.0}
        ctx2 = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        assert memo[ctx2] == 42.0


# ---------------------------------------------------------------------------
# build_table — IN filter
# ---------------------------------------------------------------------------

class TestInFilterBuildTable:
    def test_two_values(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["North", "West"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1300.0)

    def test_three_values(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["North", "South", "West"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1800.0)

    def test_all_values(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["North", "South", "West", "East"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(2000.0)

    def test_single_item_list(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["North"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1000.0)

    def test_single_item_list_matches_scalar(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl_scalar = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual", entity="North")
        tbl_list   = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual", entity=["North"])
        assert tbl_scalar.loc["Revenue", "Jan 2024"] == pytest.approx(
            tbl_list.loc["Revenue", "Jan 2024"]
        )

    def test_no_matching_values_returns_zero(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["Nonexistent"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(0.0)

    def test_multiple_periods(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar), feb(calendar)],
                               scenario="Actual", entity=["North", "West"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1300.0)
        assert tbl.loc["Revenue", "Feb 2024"] == pytest.approx(1200.0)

    def test_in_filter_with_scalar_filter(self, con, calendar):
        """entity IN (...) AND region = 'East' are ANDed correctly."""
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["North", "South", "West"], region="East")
        # North/East=1000, South/East=500 — West/West excluded by region='East'
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_two_in_filters(self, con, calendar):
        """Two list filters are both applied as IN clauses."""
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Actual",
                               entity=["North", "South", "West"],
                               region=["East"])
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1500.0)

    def test_scenario_is_still_filtered(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_table(["Revenue"], [jan(calendar)], scenario="Budget",
                               entity=["North", "West"])
        # Only Budget/North exists = 1100; West has no Budget row
        assert tbl.loc["Revenue", "Jan 2024"] == pytest.approx(1100.0)


# ---------------------------------------------------------------------------
# build_breakdown_table — IN filter
# ---------------------------------------------------------------------------

class TestInFilterBreakdown:
    def test_in_filter_restricts_rows(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "Revenue", [jan(calendar)], scenario="Actual",
            dimension="entity",
            region=["East"],
        )
        # Only East-region rows: North and South (and East entity)
        assert "West" not in tbl.index

    def test_in_filter_correct_values_with_explicit_dimension(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "Revenue", [jan(calendar)], scenario="Actual",
            dimension="entity", dimension_values=["North", "West"],
            region=["East", "West"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1000.0)
        assert tbl.loc["West",  "Jan 2024"] == pytest.approx(300.0)

    def test_in_filter_no_dimension_values_duckdb_enumerates(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        tbl = calc.build_breakdown_table(
            "Revenue", [jan(calendar)], scenario="Actual",
            dimension="entity",
            entity=["North", "South"],
        )
        assert set(tbl.index) == {"North", "South"}
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1000.0)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# resolve() — IN filter
# ---------------------------------------------------------------------------

class TestInFilterResolve:
    def test_resolve_with_in_filter(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        assert calc.resolve("Revenue", ctx) == pytest.approx(1300.0)

    def test_resolve_memoised(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        v1 = calc.resolve("Revenue", ctx)
        v2 = calc.resolve("Revenue", ctx)
        assert v1 == v2

    def test_different_lists_not_confused_in_memo(self, con, calendar):
        calc = Calculator(make_registry(), connection=con)
        ctx_nw = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "West"])
        ctx_ns = CalculationContext.make(period=jan(calendar), scenario="Actual", entity=["North", "South"])
        v_nw = calc.resolve("Revenue", ctx_nw)
        v_ns = calc.resolve("Revenue", ctx_ns)
        assert v_nw == pytest.approx(1300.0)
        assert v_ns == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# Python resolver path — IN filter
# ---------------------------------------------------------------------------

class TestInFilterPythonPath:
    def test_list_filter_passed_to_resolver_as_tuple(self, calendar):
        received = []
        r = MeasureRegistry()
        r.register(Measure(
            name="Rev",
            resolver=lambda ctx: received.append(ctx.get("entity")) or 0.0,
        ))
        calc = Calculator(r)
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual",
            entity=["North", "West"],
        )
        calc.resolve("Rev", ctx)
        assert received[0] == ("North", "West")

    def test_scalar_filter_passed_to_resolver_unchanged(self, calendar):
        received = []
        r = MeasureRegistry()
        r.register(Measure(
            name="Rev",
            resolver=lambda ctx: received.append(ctx.get("entity")) or 0.0,
        ))
        calc = Calculator(r)
        ctx = CalculationContext.make(
            period=jan(calendar), scenario="Actual", entity="North"
        )
        calc.resolve("Rev", ctx)
        assert received[0] == "North"

    def test_build_table_python_path_passes_list_filter(self, calendar):
        received = []
        r = MeasureRegistry()
        r.register(Measure(
            name="Rev",
            resolver=lambda ctx: received.append(ctx.get("entity")) or 0.0,
        ))
        calc = Calculator(r)
        calc.build_table(["Rev"], [jan(calendar)], scenario="Actual",
                         entity=["North", "West"])
        assert received[0] == ("North", "West")
