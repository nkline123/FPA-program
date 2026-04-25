import pytest
from fpa import BaseMeasure, Measure, AggType


# ---------------------------------------------------------------------------
# BaseMeasure
# ---------------------------------------------------------------------------

def test_base_measure_requires_sql_or_resolver():
    with pytest.raises(ValueError, match="requires either sql or resolver"):
        BaseMeasure(name="Rev")


def test_base_measure_sql_requires_value_col():
    with pytest.raises(ValueError, match="value_col"):
        BaseMeasure(
            name="Rev",
            sql="SELECT * FROM gl WHERE account_type = 'Income'",
            # value_col omitted
        )


def test_base_measure_resolver_must_be_callable():
    with pytest.raises(ValueError, match="resolver must be callable"):
        BaseMeasure(
            name="Rev",
            sql="SELECT * FROM gl",
            value_col="amount",
            resolver="not_a_function",
        )


def test_base_measure_sql_only_valid():
    m = BaseMeasure(
        name="Revenue",
        sql="SELECT * FROM gl WHERE account_type = 'Income'",
        value_col="amount",
    )
    assert m.sql == "SELECT * FROM gl WHERE account_type = 'Income'"
    assert m.value_col == "amount"
    assert m.resolver is None


def test_base_measure_resolver_only_valid():
    m = BaseMeasure(name="Rev", resolver=lambda ctx: 42.0)
    assert m.resolver(None) == 42.0
    assert m.sql == ""


def test_base_measure_both_sql_and_resolver_valid():
    m = BaseMeasure(
        name="Rev",
        sql="SELECT * FROM gl",
        value_col="amount",
        resolver=lambda ctx: 0.0,
    )
    assert m.sql
    assert callable(m.resolver)


def test_base_measure_default_agg_type():
    m = BaseMeasure(name="Rev", resolver=lambda ctx: 0)
    assert m.agg_type == AggType.SUM


def test_base_measure_custom_agg_type():
    m = BaseMeasure(
        name="HC",
        sql="SELECT * FROM hc",
        value_col="headcount",
        agg_type=AggType.LAST_DAY,
    )
    assert m.agg_type == AggType.LAST_DAY


def test_base_measure_date_col_default_empty():
    m = BaseMeasure(name="Rev", resolver=lambda ctx: 0)
    assert m.date_col == ""


def test_base_measure_custom_date_col():
    m = BaseMeasure(
        name="Rev",
        sql="SELECT * FROM gl",
        value_col="amount",
        date_col="period_enddate",
    )
    assert m.date_col == "period_enddate"


def test_base_measure_tags_default_empty():
    m = BaseMeasure(name="Rev", resolver=lambda ctx: 0)
    assert m.tags == []


def test_base_measure_tags_mutable_default_not_shared():
    m1 = BaseMeasure(name="A", resolver=lambda ctx: 0)
    m2 = BaseMeasure(name="B", resolver=lambda ctx: 0)
    m1.tags.append("x")
    assert "x" not in m2.tags


def test_base_measure_hash_by_name():
    m1 = BaseMeasure(name="Rev", resolver=lambda ctx: 1)
    m2 = BaseMeasure(name="Rev", sql="SELECT * FROM gl", value_col="amount")
    assert hash(m1) == hash(m2)


def test_base_measure_equality_by_name():
    m1 = BaseMeasure(name="Rev", resolver=lambda ctx: 1)
    m2 = BaseMeasure(name="Rev", sql="SELECT * FROM gl", value_col="amount")
    assert m1 == m2


def test_base_measure_inequality_different_name():
    m1 = BaseMeasure(name="Rev", resolver=lambda ctx: 0)
    m2 = BaseMeasure(name="COGS", resolver=lambda ctx: 0)
    assert m1 != m2


def test_base_measure_not_equal_to_non_measure():
    m = BaseMeasure(name="Rev", resolver=lambda ctx: 0)
    assert m != "Rev"


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------

def test_measure_requires_at_least_one_dependency():
    with pytest.raises(ValueError, match="at least one dependency"):
        Measure(name="GP", dependencies=[], formula=lambda v: 0)


def test_measure_requires_callable_formula():
    with pytest.raises(ValueError, match="formula must be callable"):
        Measure(name="GP", dependencies=["Revenue"], formula="not_a_function")


def test_measure_formula_called_with_dep_values():
    m = Measure(
        name="GP",
        dependencies=["Revenue", "COGS"],
        formula=lambda v: v["Revenue"] - v["COGS"],
    )
    assert m.formula({"Revenue": 100, "COGS": 60}) == 40


def test_measure_default_agg_type_is_calculated():
    m = Measure(name="GM%", dependencies=["GP", "Rev"], formula=lambda v: 0)
    assert m.agg_type == AggType.CALCULATED


def test_measure_hash_by_name():
    m1 = Measure(name="GP", dependencies=["Rev"], formula=lambda v: 0)
    m2 = Measure(name="GP", dependencies=["COGS"], formula=lambda v: 1)
    assert hash(m1) == hash(m2)


def test_measure_equality_by_name():
    m1 = Measure(name="GP", dependencies=["Rev"], formula=lambda v: 0)
    m2 = Measure(name="GP", dependencies=["COGS"], formula=lambda v: 1)
    assert m1 == m2


def test_measure_not_equal_to_base_measure():
    derived = Measure(name="GP", dependencies=["Rev"], formula=lambda v: 0)
    base = BaseMeasure(name="GP", resolver=lambda ctx: 0)
    assert derived != base
