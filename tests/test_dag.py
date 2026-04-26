import pytest
from fpa import Measure, MeasureRegistry
from fpa.measures.dag import MeasureDAG


def make_registry(*measures):
    r = MeasureRegistry()
    r.register_many(list(measures))
    return r


def leaf(name):
    return Measure(name=name, resolver=lambda ctx: 0)


def derived(name, deps):
    return Measure(name=name, dependencies=deps, formula=lambda v: 0)


def sql_leaf(name):
    return Measure(
        name=name,
        sql=f"SELECT * FROM gl WHERE account_id = '{name}'",
        value_col="amount",
    )


def sql_composed(name, parent):
    return Measure(
        name=name,
        sql=f"SELECT * FROM measure.{parent} WHERE dept = 'X'",
    )


# ---------------------------------------------------------------------------
# Build and evaluation order
# ---------------------------------------------------------------------------

def test_evaluation_order_deps_before_dependents():
    r = make_registry(
        leaf("Rev"),
        leaf("COGS"),
        derived("GP", ["Rev", "COGS"]),
        derived("GM%", ["GP", "Rev"]),
    )
    dag = MeasureDAG(r)
    order = dag.evaluation_order()
    assert order.index("Rev") < order.index("GP")
    assert order.index("COGS") < order.index("GP")
    assert order.index("GP") < order.index("GM%")


def test_all_measures_present_in_order():
    r = make_registry(leaf("A"), leaf("B"), derived("C", ["A", "B"]))
    dag = MeasureDAG(r)
    assert set(dag.evaluation_order()) == {"A", "B", "C"}


def test_only_leaf_measures():
    r = make_registry(leaf("A"), leaf("B"))
    dag = MeasureDAG(r)
    assert set(dag.evaluation_order()) == {"A", "B"}


# ---------------------------------------------------------------------------
# SQL measure.X composition dependencies
# ---------------------------------------------------------------------------

def test_sql_composed_measure_edges_built_from_sql_refs():
    r = make_registry(sql_leaf("Expense"), sql_composed("SalesExp", "Expense"))
    dag = MeasureDAG(r)
    assert "Expense" in dag.dependencies_of("SalesExp")


def test_sql_composed_evaluation_order():
    r = make_registry(sql_leaf("Expense"), sql_composed("SalesExp", "Expense"))
    dag = MeasureDAG(r)
    order = dag.evaluation_order()
    assert order.index("Expense") < order.index("SalesExp")


def test_sql_composed_unknown_ref_raises():
    r = MeasureRegistry()
    r._measures["SalesExp"] = Measure(
        name="SalesExp",
        sql="SELECT * FROM measure.Expense WHERE dept = 'Sales'",
    )
    with pytest.raises(ValueError, match="not registered"):
        MeasureDAG(r)


def test_sql_composed_all_dependencies_transitive():
    r = make_registry(
        sql_leaf("GL"),
        sql_composed("Expense", "GL"),
        sql_composed("SalesExp", "Expense"),
    )
    dag = MeasureDAG(r)
    all_deps = dag.all_dependencies_of("SalesExp")
    assert "GL" in all_deps
    assert "Expense" in all_deps
    assert "SalesExp" not in all_deps


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

def test_cycle_raises():
    r = MeasureRegistry()
    r._measures["A"] = Measure(name="A", dependencies=["B"], formula=lambda v: 0)
    r._measures["B"] = Measure(name="B", dependencies=["A"], formula=lambda v: 0)
    with pytest.raises(ValueError, match="[Cc]ircular"):
        MeasureDAG(r)


def test_self_cycle_raises():
    r = MeasureRegistry()
    r._measures["A"] = Measure(name="A", dependencies=["A"], formula=lambda v: 0)
    with pytest.raises(ValueError):
        MeasureDAG(r)


# ---------------------------------------------------------------------------
# Unknown dependency
# ---------------------------------------------------------------------------

def test_unknown_formula_dependency_raises():
    r = make_registry(derived("GP", ["Revenue", "COGS"]))
    with pytest.raises(ValueError, match="not registered"):
        MeasureDAG(r)


# ---------------------------------------------------------------------------
# Graph queries
# ---------------------------------------------------------------------------

def test_dependencies_of():
    r = make_registry(leaf("Rev"), leaf("COGS"), derived("GP", ["Rev", "COGS"]))
    dag = MeasureDAG(r)
    assert set(dag.dependencies_of("GP")) == {"Rev", "COGS"}


def test_dependencies_of_leaf():
    r = make_registry(leaf("Rev"))
    dag = MeasureDAG(r)
    assert dag.dependencies_of("Rev") == []


def test_dependents_of():
    r = make_registry(leaf("Rev"), leaf("COGS"), derived("GP", ["Rev", "COGS"]))
    dag = MeasureDAG(r)
    assert "GP" in dag.dependents_of("Rev")


def test_dependents_of_terminal():
    r = make_registry(leaf("Rev"), derived("GP", ["Rev"]))
    dag = MeasureDAG(r)
    assert dag.dependents_of("GP") == []


def test_all_dependencies_of_transitive():
    r = make_registry(
        leaf("Rev"),
        leaf("COGS"),
        derived("GP", ["Rev", "COGS"]),
        derived("GM%", ["GP", "Rev"]),
    )
    dag = MeasureDAG(r)
    all_deps = dag.all_dependencies_of("GM%")
    assert "Rev" in all_deps
    assert "COGS" in all_deps
    assert "GP" in all_deps
    assert "GM%" not in all_deps


def test_all_dependencies_order():
    r = make_registry(
        leaf("Rev"),
        leaf("COGS"),
        derived("GP", ["Rev", "COGS"]),
        derived("GM%", ["GP", "Rev"]),
    )
    dag = MeasureDAG(r)
    all_deps = dag.all_dependencies_of("GM%")
    assert all_deps.index("Rev") < all_deps.index("GP")
    assert all_deps.index("COGS") < all_deps.index("GP")


def test_repr():
    r = make_registry(leaf("A"), derived("B", ["A"]))
    dag = MeasureDAG(r)
    assert "MeasureDAG" in repr(dag)
