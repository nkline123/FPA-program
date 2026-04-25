import pytest
from fpa import BaseMeasure, Measure, MeasureRegistry


def base(name, tags=None):
    return BaseMeasure(name=name, resolver=lambda ctx: 0, tags=tags or [])


def derived(name, deps, tags=None):
    return Measure(name=name, dependencies=deps, formula=lambda v: 0, tags=tags or [])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_and_get():
    r = MeasureRegistry()
    m = base("Rev")
    r.register(m)
    assert r.get("Rev") is m


def test_register_duplicate_raises():
    r = MeasureRegistry()
    r.register(base("Rev"))
    with pytest.raises(ValueError, match="already registered"):
        r.register(base("Rev"))


def test_register_many():
    r = MeasureRegistry()
    r.register_many([base("Rev"), base("COGS")])
    assert "Rev" in r
    assert "COGS" in r


def test_get_missing_raises_key_error():
    r = MeasureRegistry()
    with pytest.raises(KeyError, match="not registered"):
        r.get("Missing")


def test_names():
    r = MeasureRegistry()
    r.register_many([base("A"), base("B")])
    assert set(r.names()) == {"A", "B"}


# ---------------------------------------------------------------------------
# Filtered views
# ---------------------------------------------------------------------------

def test_base_measures_returns_only_base():
    r = MeasureRegistry()
    r.register_many([base("Rev"), derived("GP", ["Rev"])])
    result = r.base_measures()
    assert len(result) == 1
    assert result[0].name == "Rev"


def test_derived_measures_returns_only_derived():
    r = MeasureRegistry()
    r.register_many([base("Rev"), derived("GP", ["Rev"])])
    result = r.derived_measures()
    assert len(result) == 1
    assert result[0].name == "GP"


def test_by_tag():
    r = MeasureRegistry()
    r.register_many([
        base("Rev", tags=["is"]),
        base("COGS", tags=["is"]),
        base("HC", tags=["headcount"]),
    ])
    tagged = r.by_tag("is")
    assert {m.name for m in tagged} == {"Rev", "COGS"}


def test_by_tag_empty_result():
    r = MeasureRegistry()
    r.register(base("Rev"))
    assert r.by_tag("nonexistent") == []


# ---------------------------------------------------------------------------
# Dunder methods
# ---------------------------------------------------------------------------

def test_contains():
    r = MeasureRegistry()
    r.register(base("Rev"))
    assert "Rev" in r
    assert "COGS" not in r


def test_len():
    r = MeasureRegistry()
    r.register_many([base("A"), base("B"), base("C")])
    assert len(r) == 3


def test_repr():
    r = MeasureRegistry()
    r.register(base("Rev"))
    assert "Rev" in repr(r)
