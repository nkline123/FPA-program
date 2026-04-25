from datetime import date
import pytest
import duckdb
from fpa import (
    BaseMeasure, Measure, MeasureRegistry, Calculator,
    CalculationContext, FiscalCalendar, Grain, AggType,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def con():
    """In-memory DuckDB connection with a small GL table."""
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE gl (
            scenario   VARCHAR,
            account_id VARCHAR,
            date       DATE,
            entity     VARCHAR,
            amount     DOUBLE
        )
    """)
    c.executemany("INSERT INTO gl VALUES (?, ?, ?, ?, ?)", [
        # Actual, Jan 2024
        ("Actual", "4000", date(2024, 1, 15), "North", 1000.0),
        ("Actual", "4000", date(2024, 1, 20), "South",  500.0),
        ("Actual", "5000", date(2024, 1, 10), "North",  300.0),
        ("Actual", "5000", date(2024, 1, 25), "South",  100.0),
        # Actual, Feb 2024
        ("Actual", "4000", date(2024, 2, 10), "North",  800.0),
        ("Actual", "4000", date(2024, 2, 20), "South",  600.0),
        ("Actual", "5000", date(2024, 2, 15), "North",  200.0),
        ("Actual", "5000", date(2024, 2, 28), "South",  150.0),
        # Budget, Jan 2024
        ("Budget", "4000", date(2024, 1, 15), "North", 1100.0),
        ("Budget", "5000", date(2024, 1, 10), "North",  320.0),
    ])
    yield c
    c.close()


@pytest.fixture
def calendar():
    return FiscalCalendar(fiscal_year_start_month=1)


def make_registry(with_sql=False):
    """
    Registry with Revenue, COGS, Gross Profit, Gross Margin %.
    with_sql=True adds sql_expr to the base measures.
    """
    r = MeasureRegistry()
    rev_sql  = "SUM(CASE WHEN account_id = '4000' AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"
    cogs_sql = "SUM(CASE WHEN account_id = '5000' AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"
    r.register_many([
        BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: 0.0,
            sql_expr=rev_sql if with_sql else "",
        ),
        BaseMeasure(
            name="COGS",
            resolver=lambda ctx: 0.0,
            sql_expr=cogs_sql if with_sql else "",
        ),
        Measure(
            name="Gross Profit",
            dependencies=["Revenue", "COGS"],
            formula=lambda v: v["Revenue"] - v["COGS"],
        ),
        Measure(
            name="Gross Margin %",
            dependencies=["Gross Profit", "Revenue"],
            formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0,
        ),
    ])
    return r


def make_context(calendar, scenario="Actual", month=(2024, 1), **filters):
    period = calendar.month_period(date(*month, 1))
    return CalculationContext.make(period=period, scenario=scenario, **filters)


# ---------------------------------------------------------------------------
# CalculationContext
# ---------------------------------------------------------------------------

class TestCalculationContext:
    def test_make_stores_filters(self, calendar):
        ctx = make_context(calendar, entity="North", dept="Eng")
        assert ctx.get("entity") == "North"
        assert ctx.get("dept") == "Eng"

    def test_get_missing_returns_default(self, calendar):
        ctx = make_context(calendar)
        assert ctx.get("missing") is None
        assert ctx.get("missing", "fallback") == "fallback"

    def test_filters_are_sorted(self, calendar):
        ctx = CalculationContext.make(
            period=calendar.month_period(date(2024, 1, 1)),
            scenario="Actual",
            z="last", a="first",
        )
        keys = [k for k, _ in ctx.filters]
        assert keys == sorted(keys)

    def test_is_hashable(self, calendar):
        ctx = make_context(calendar)
        assert {ctx: 1}[ctx] == 1

    def test_equality_same_inputs(self, calendar):
        assert make_context(calendar, entity="North") == make_context(calendar, entity="North")

    def test_inequality_different_filters(self, calendar):
        assert make_context(calendar, entity="North") != make_context(calendar, entity="South")

    def test_usable_as_dict_key(self, calendar):
        ctx1 = make_context(calendar, scenario="Actual")
        ctx2 = make_context(calendar, scenario="Budget")
        d = {ctx1: "actual", ctx2: "budget"}
        assert d[make_context(calendar, scenario="Actual")] == "actual"


# ---------------------------------------------------------------------------
# Calculator — Python path (no connection)
# ---------------------------------------------------------------------------

class TestCalculatorPython:
    @pytest.fixture
    def calc(self, calendar):
        r = MeasureRegistry()
        r.register_many([
            BaseMeasure(name="Revenue",  resolver=lambda ctx: 100.0),
            BaseMeasure(name="COGS",     resolver=lambda ctx: 40.0),
            Measure(name="Gross Profit", dependencies=["Revenue", "COGS"],
                    formula=lambda v: v["Revenue"] - v["COGS"]),
            Measure(name="Gross Margin %", dependencies=["Gross Profit", "Revenue"],
                    formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100) if v["Revenue"] else 0.0),
        ])
        return Calculator(r)

    def test_resolve_base(self, calc, calendar):
        assert calc.resolve("Revenue", make_context(calendar)) == 100.0

    def test_resolve_derived(self, calc, calendar):
        assert calc.resolve("Gross Profit", make_context(calendar)) == 60.0

    def test_resolve_nested_derived(self, calc, calendar):
        assert calc.resolve("Gross Margin %", make_context(calendar)) == 60.0

    def test_resolve_unknown_raises(self, calc, calendar):
        with pytest.raises(KeyError):
            calc.resolve("Nonexistent", make_context(calendar))

    def test_resolver_none_becomes_zero(self, calendar):
        r = MeasureRegistry()
        r.register(BaseMeasure(name="X", resolver=lambda ctx: None))
        assert Calculator(r).resolve("X", make_context(calendar)) == 0.0

    def test_resolver_error_wrapped(self, calendar):
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Boom", resolver=lambda ctx: 1 / 0))
        with pytest.raises(RuntimeError, match="Resolver error"):
            Calculator(r).resolve("Boom", make_context(calendar))

    def test_result_coerced_to_float(self, calendar):
        r = MeasureRegistry()
        r.register(BaseMeasure(name="X", resolver=lambda ctx: 7))
        result = Calculator(r).resolve("X", make_context(calendar))
        assert isinstance(result, float)

    def test_memoization_resolver_called_once(self, calendar):
        calls = []
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Rev", resolver=lambda ctx: calls.append(1) or 50.0))
        r.register(Measure(name="A", dependencies=["Rev"], formula=lambda v: v["Rev"]))
        r.register(Measure(name="B", dependencies=["Rev"], formula=lambda v: v["Rev"] * 2))
        calc = Calculator(r)
        ctx = make_context(calendar)
        calc.resolve("A", ctx)
        calc.resolve("B", ctx)
        assert len(calls) == 1

    def test_clear_cache_causes_re_resolve(self, calendar):
        calls = []
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Rev", resolver=lambda ctx: calls.append(1) or 10.0))
        calc = Calculator(r)
        ctx = make_context(calendar)
        calc.resolve("Rev", ctx)
        calc.clear_cache()
        calc.resolve("Rev", ctx)
        assert len(calls) == 2

    def test_memo_isolated_across_contexts(self, calendar):
        calls = []
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Rev", resolver=lambda ctx: calls.append(1) or 10.0))
        calc = Calculator(r)
        calc.resolve("Rev", make_context(calendar, scenario="Actual"))
        calc.resolve("Rev", make_context(calendar, scenario="Budget"))
        assert len(calls) == 2

    def test_resolve_many(self, calc, calendar):
        result = calc.resolve_many(["Revenue", "COGS", "Gross Profit"], make_context(calendar))
        assert result == {"Revenue": 100.0, "COGS": 40.0, "Gross Profit": 60.0}

    def test_build_table_shape(self, calc, calendar):
        periods = calendar.periods_for_fiscal_year(2024, Grain.MONTH)
        tbl = calc.build_table(["Revenue", "Gross Profit"], periods, scenario="Actual")
        assert tbl.shape == (2, 12)

    def test_build_table_index_and_columns(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 2, 29))
        tbl = calc.build_table(["Revenue", "COGS"], periods, scenario="Actual")
        assert list(tbl.index) == ["Revenue", "COGS"]
        assert list(tbl.columns) == ["Jan 2024", "Feb 2024"]

    def test_build_table_values(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 3, 31))
        tbl = calc.build_table(["Revenue", "Gross Profit"], periods, scenario="Actual")
        assert tbl.loc["Revenue", "Jan 2024"] == 100.0
        assert tbl.loc["Gross Profit", "Jan 2024"] == 60.0

    def test_build_table_passes_filters(self, calendar):
        received = []
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Rev", resolver=lambda ctx: received.append(ctx.get("entity")) or 0.0))
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        Calculator(r).build_table(["Rev"], periods, scenario="Actual", entity="North")
        assert received == ["North"]

    def test_build_breakdown_table_shape(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 3, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.shape == (2, 3)

    def test_build_breakdown_table_index(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert list(tbl.index) == ["North", "South"]

    def test_build_breakdown_table_values(self, calendar):
        def resolver(ctx):
            return {"North": 300.0, "South": 200.0}.get(ctx.get("entity"), 0.0)
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Rev", resolver=resolver))
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = Calculator(r).build_breakdown_table(
            "Rev", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == 300.0
        assert tbl.loc["South", "Jan 2024"] == 200.0

    def test_build_breakdown_fixed_filters_passed(self, calendar):
        received = []
        def resolver(ctx):
            received.append((ctx.get("entity"), ctx.get("dept")))
            return 0.0
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Rev", resolver=resolver))
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        Calculator(r).build_breakdown_table(
            "Rev", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
            dept="Engineering",
        )
        assert received[0] == ("North", "Engineering")


# ---------------------------------------------------------------------------
# Calculator — DuckDB path (with connection)
# ---------------------------------------------------------------------------

class TestCalculatorDuckDB:
    @pytest.fixture
    def calc(self, con, calendar):
        return Calculator(make_registry(with_sql=True), connection=con, table="gl")

    # -- build_table: always uses Python path, even with connection --

    def test_build_table_uses_python_path(self, con, calendar):
        # Resolvers return a fixed value — if DuckDB were used, values would
        # come from the database and differ. Python path returns the fixed value.
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Rev",
            resolver=lambda ctx: 42.0,
            sql_expr="SUM(amount)",
        ))
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = Calculator(r, connection=con, table="gl").build_table(
            ["Rev"], periods, scenario="Actual"
        )
        assert tbl.loc["Rev", "Jan 2024"] == 42.0

    # -- build_breakdown_table: uses DuckDB when sql_expr present --

    def test_breakdown_base_measure(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1000.0)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(500.0)

    def test_breakdown_second_base_measure(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "COGS", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(300.0)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(100.0)

    def test_breakdown_derived_measure(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Gross Profit", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(700.0)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(400.0)

    def test_breakdown_conditional_formula(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Gross Margin %", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(700.0 / 1000.0 * 100)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(400.0 / 500.0 * 100)

    def test_breakdown_multiple_periods(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 2, 29))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1000.0)
        assert tbl.loc["North", "Feb 2024"] == pytest.approx(800.0)

    def test_breakdown_scenario_filter(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Budget",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1100.0)

    def test_breakdown_extra_fixed_filter(self, con, calendar):
        r = make_registry(with_sql=True)
        calc = Calculator(r, connection=con, table="gl")
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1000.0)

    def test_breakdown_shape(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 2, 29))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.shape == (2, 2)

    def test_breakdown_index_is_dimension_values(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert list(tbl.index) == ["North", "South"]

    def test_breakdown_missing_dimension_value_returns_zero(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "East"],
        )
        assert tbl.loc["East", "Jan 2024"] == pytest.approx(0.0)

    def test_breakdown_no_data_period_returns_zero(self, calc, calendar):
        periods = calendar.month_range(date(2024, 3, 1), date(2024, 3, 31))
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Mar 2024"] == pytest.approx(0.0)

    # -- Falls back to Python when no sql_expr --

    def test_falls_back_when_no_sql_expr(self, con, calendar):
        r = MeasureRegistry()
        r.register(BaseMeasure(name="Fixed", resolver=lambda ctx: 99.0))
        calc = Calculator(r, connection=con, table="gl")
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Fixed", periods, scenario="Actual",
            dimension="entity", dimension_values=["North", "South"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(99.0)
        assert tbl.loc["South", "Jan 2024"] == pytest.approx(99.0)

    def test_falls_back_without_connection(self, calendar):
        r = make_registry(with_sql=True)
        calc = Calculator(r)  # no connection
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        # sql_expr present but no connection — uses resolver (returns 0.0 placeholder)
        tbl = calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(0.0)

    # -- Mixed: some measures have sql_expr, some use Python resolver --

    def test_mixed_sql_and_python_base_measures(self, con, calendar):
        r = MeasureRegistry()
        r.register(BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: 0.0,
            sql_expr="SUM(CASE WHEN account_id = '4000' AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)",
        ))
        r.register(BaseMeasure(
            name="Adjustment",
            resolver=lambda ctx: 50.0,   # no sql_expr — uses Python resolver
        ))
        r.register(Measure(
            name="Adjusted Revenue",
            dependencies=["Revenue", "Adjustment"],
            formula=lambda v: v["Revenue"] + v["Adjustment"],
        ))
        calc = Calculator(r, connection=con, table="gl")
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        tbl = calc.build_breakdown_table(
            "Adjusted Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        assert tbl.loc["North", "Jan 2024"] == pytest.approx(1050.0)  # 1000 + 50

    # -- Correctness: DuckDB and Python paths agree --

    def test_duckdb_matches_python_path(self, con, calendar):
        """Both paths produce identical results for the same data."""
        lookup = {}
        for row in con.execute("""
            SELECT scenario, account_id, entity, strftime(date, '%Y-%m') AS month, SUM(amount) AS amt
            FROM gl GROUP BY scenario, account_id, entity, month
        """).fetchall():
            lookup[(row[0], row[1], row[2], row[3])] = row[4]

        def q(accts, ctx):
            entity = ctx.get("entity")
            m = ctx.period.start.strftime("%Y-%m")
            return sum(lookup.get((ctx.scenario, a, entity, m), 0.0) for a in accts)

        python_registry = MeasureRegistry()
        python_registry.register_many([
            BaseMeasure(name="Revenue", resolver=lambda ctx: q(["4000"], ctx)),
            BaseMeasure(name="COGS",    resolver=lambda ctx: q(["5000"], ctx)),
            Measure(name="Gross Profit", dependencies=["Revenue", "COGS"],
                    formula=lambda v: v["Revenue"] - v["COGS"]),
        ])

        duckdb_registry = make_registry(with_sql=True)

        periods = calendar.month_range(date(2024, 1, 1), date(2024, 2, 29))
        entities = ["North", "South"]

        python_calc = Calculator(python_registry)
        duckdb_calc = Calculator(duckdb_registry, connection=con, table="gl")

        for measure in ["Revenue", "COGS", "Gross Profit"]:
            py_tbl  = python_calc.build_breakdown_table(
                measure, periods, scenario="Actual",
                dimension="entity", dimension_values=entities,
            )
            db_tbl  = duckdb_calc.build_breakdown_table(
                measure, periods, scenario="Actual",
                dimension="entity", dimension_values=entities,
            )
            for entity in entities:
                for period in periods:
                    assert py_tbl.loc[entity, period.label] == pytest.approx(
                        db_tbl.loc[entity, period.label], rel=1e-6
                    ), f"{measure} {entity} {period.label}"

    # -- DuckDB-specific: verify single SQL query is issued --

    def test_single_query_for_multiple_periods(self, con, calendar):
        """Confirm one SQL query covers all periods (not one per period)."""
        from unittest.mock import patch, MagicMock

        calc = Calculator(make_registry(with_sql=True), connection=con, table="gl")
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 3, 31))

        with patch.object(calc, "_sql_fetch", wraps=calc._sql_fetch) as mock_fetch:
            calc.build_breakdown_table(
                "Revenue", periods, scenario="Actual",
                dimension="entity", dimension_values=["North", "South"],
            )
        assert mock_fetch.call_count == 1

    # -- Clear cache works the same regardless of path --

    def test_clear_cache(self, calc, calendar):
        periods = calendar.month_range(date(2024, 1, 1), date(2024, 1, 31))
        calc.build_breakdown_table(
            "Revenue", periods, scenario="Actual",
            dimension="entity", dimension_values=["North"],
        )
        calc.clear_cache()
        assert len(calc._memo) == 0
