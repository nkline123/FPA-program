import pytest
from fpa import Measure, MeasureRegistry


def sql_measure(name, tags=None):
    return Measure(
        name=name,
        sql=f"SELECT * FROM gl WHERE account_id = '{name}'",
        value_col="amount",
        scenario_col="scenario",
        tags=tags or [],
    )


def resolver_measure(name, tags=None):
    return Measure(name=name, resolver=lambda ctx: 0, tags=tags or [])


def formula_measure(name, deps, tags=None):
    return Measure(name=name, dependencies=deps, formula=lambda v: 0, tags=tags or [])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_and_get():
    r = MeasureRegistry()
    m = resolver_measure("Rev")
    r.register(m)
    assert r.get("Rev") is m


def test_register_duplicate_raises():
    r = MeasureRegistry()
    r.register(resolver_measure("Rev"))
    with pytest.raises(ValueError, match="already registered"):
        r.register(resolver_measure("Rev"))


def test_register_many():
    r = MeasureRegistry()
    r.register_many([resolver_measure("Rev"), resolver_measure("COGS")])
    assert "Rev" in r
    assert "COGS" in r


def test_get_missing_raises_key_error():
    r = MeasureRegistry()
    with pytest.raises(KeyError, match="not registered"):
        r.get("Missing")


def test_names():
    r = MeasureRegistry()
    r.register_many([resolver_measure("A"), resolver_measure("B")])
    assert set(r.names()) == {"A", "B"}


# ---------------------------------------------------------------------------
# Filtered views
# ---------------------------------------------------------------------------

def test_sql_measures_returns_only_sql():
    r = MeasureRegistry()
    r.register_many([sql_measure("Rev"), formula_measure("GP", ["Rev"])])
    result = r.sql_measures()
    assert len(result) == 1
    assert result[0].name == "Rev"


def test_formula_measures_returns_only_formula():
    r = MeasureRegistry()
    r.register_many([resolver_measure("Rev"), formula_measure("GP", ["Rev"])])
    result = r.formula_measures()
    assert len(result) == 1
    assert result[0].name == "GP"


def test_all_measures_returns_all():
    r = MeasureRegistry()
    r.register_many([resolver_measure("Rev"), formula_measure("GP", ["Rev"])])
    assert len(r.all_measures()) == 2


def test_by_tag():
    r = MeasureRegistry()
    r.register_many([
        resolver_measure("Rev",  tags=["is"]),
        resolver_measure("COGS", tags=["is"]),
        resolver_measure("HC",   tags=["headcount"]),
    ])
    tagged = r.by_tag("is")
    assert {m.name for m in tagged} == {"Rev", "COGS"}


def test_by_tag_empty_result():
    r = MeasureRegistry()
    r.register(resolver_measure("Rev"))
    assert r.by_tag("nonexistent") == []


# ---------------------------------------------------------------------------
# Dunder methods
# ---------------------------------------------------------------------------

def test_contains():
    r = MeasureRegistry()
    r.register(resolver_measure("Rev"))
    assert "Rev" in r
    assert "COGS" not in r


def test_len():
    r = MeasureRegistry()
    r.register_many([resolver_measure("A"), resolver_measure("B"), resolver_measure("C")])
    assert len(r) == 3


def test_repr():
    r = MeasureRegistry()
    r.register(resolver_measure("Rev"))
    assert "Rev" in repr(r)
