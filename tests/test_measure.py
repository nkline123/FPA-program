import pytest
from fpa import Measure, AggType


# ---------------------------------------------------------------------------
# Validation — SQL path
# ---------------------------------------------------------------------------

def test_measure_requires_sql_formula_or_resolver():
    with pytest.raises(ValueError, match="requires sql, formula, or resolver"):
        Measure(name="Rev")


def test_leaf_sql_requires_value_col():
    with pytest.raises(ValueError, match="value_col"):
        Measure(
            name="Rev",
            sql="SELECT * FROM gl WHERE account_type = 'Income'",
        )


def test_composed_sql_does_not_require_value_col():
    # measure.X reference means composed — value_col is inherited, not required
    m = Measure(
        name="SalesExp",
        sql="SELECT * FROM measure.Expense WHERE department = 'Sales'",
    )
    assert m.sql
    assert m.value_col == ""


def test_sql_and_formula_raises():
    with pytest.raises(ValueError, match="cannot combine sql with formula"):
        Measure(
            name="Bad",
            sql="SELECT * FROM gl",
            value_col="amount",
            formula=lambda v: 0,
            dependencies=["X"],
        )


def test_sql_rejects_calculated_agg_type():
    with pytest.raises(ValueError, match="CALCULATED"):
        Measure(
            name="Bad",
            sql="SELECT * FROM gl",
            value_col="amount",
            agg_type=AggType.CALCULATED,
        )


def test_sql_and_resolver_is_valid():
    m = Measure(
        name="Rev",
        sql="SELECT * FROM gl",
        value_col="amount",
        resolver=lambda ctx: 0.0,
    )
    assert m.sql
    assert callable(m.resolver)


def test_resolver_only_is_valid():
    m = Measure(name="Rev", resolver=lambda ctx: 42.0)
    assert m.resolver(None) == 42.0
    assert m.sql == ""


# ---------------------------------------------------------------------------
# Validation — Python formula path
# ---------------------------------------------------------------------------

def test_formula_requires_at_least_one_dependency():
    with pytest.raises(ValueError, match="at least one dependency"):
        Measure(name="GP", dependencies=[], formula=lambda v: 0)


def test_formula_and_resolver_raises():
    with pytest.raises(ValueError, match="cannot combine formula and resolver"):
        Measure(
            name="GP",
            dependencies=["Rev"],
            formula=lambda v: 0,
            resolver=lambda ctx: 0,
        )


# ---------------------------------------------------------------------------
# Defaults and field access
# ---------------------------------------------------------------------------

def test_agg_type_defaults_to_none():
    m = Measure(name="Rev", resolver=lambda ctx: 0)
    assert m.agg_type is None


def test_custom_agg_type():
    m = Measure(
        name="HC",
        sql="SELECT * FROM hc",
        value_col="headcount",
        agg_type=AggType.LAST_DAY,
    )
    assert m.agg_type == AggType.LAST_DAY


def test_date_col_default_empty():
    m = Measure(name="Rev", resolver=lambda ctx: 0)
    assert m.date_col == ""


def test_custom_date_col():
    m = Measure(
        name="Rev",
        sql="SELECT * FROM gl",
        value_col="amount",
        date_col="period_enddate",
    )
    assert m.date_col == "period_enddate"


def test_tags_default_empty():
    m = Measure(name="Rev", resolver=lambda ctx: 0)
    assert m.tags == []


def test_tags_mutable_default_not_shared():
    m1 = Measure(name="A", resolver=lambda ctx: 0)
    m2 = Measure(name="B", resolver=lambda ctx: 0)
    m1.tags.append("x")
    assert "x" not in m2.tags


# ---------------------------------------------------------------------------
# Formula behaviour
# ---------------------------------------------------------------------------

def test_formula_called_with_dep_values():
    m = Measure(
        name="GP",
        dependencies=["Revenue", "COGS"],
        formula=lambda v: v["Revenue"] - v["COGS"],
    )
    assert m.formula({"Revenue": 100, "COGS": 60}) == 40


# ---------------------------------------------------------------------------
# Equality and hashing (name-based)
# ---------------------------------------------------------------------------

def test_hash_by_name():
    m1 = Measure(name="Rev", resolver=lambda ctx: 1)
    m2 = Measure(name="Rev", sql="SELECT * FROM gl", value_col="amount")
    assert hash(m1) == hash(m2)


def test_equality_by_name():
    m1 = Measure(name="Rev", resolver=lambda ctx: 1)
    m2 = Measure(name="Rev", sql="SELECT * FROM gl", value_col="amount")
    assert m1 == m2


def test_inequality_different_names():
    m1 = Measure(name="Rev", resolver=lambda ctx: 0)
    m2 = Measure(name="COGS", resolver=lambda ctx: 0)
    assert m1 != m2


def test_not_equal_to_non_measure():
    m = Measure(name="Rev", resolver=lambda ctx: 0)
    assert m != "Rev"


def test_name_deduplication_in_set():
    m1 = Measure(name="Rev", resolver=lambda ctx: 0)
    m2 = Measure(name="Rev", sql="SELECT * FROM gl", value_col="amount")
    assert len({m1, m2}) == 1


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_measure_is_immutable():
    m = Measure(name="Rev", resolver=lambda ctx: 1.0)
    with pytest.raises(Exception):  # FrozenInstanceError
        m.resolver = lambda ctx: 999.0  # type: ignore[misc]
