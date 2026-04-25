from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, List
from ..calendar.period import AggType


@dataclass
class BaseMeasure:
    """
    A measure whose value is fetched by a resolver callable.

    The library does not handle data access. Instead, you provide a
    resolver — any callable that accepts a CalculationContext and returns
    a float. How that float is obtained (database query, API call, file
    read, computation) is entirely up to the caller.

    Args:
        name:        Unique measure name. Used as the key everywhere.
        resolver:    Callable(CalculationContext) → float.
                     Called by the Calculator whenever this measure needs
                     to be resolved for a given period and scenario.
        agg_type:    How this measure aggregates across time periods.
                     Used by higher-level layers that need to know the
                     aggregation behavior (SUM, LAST_DAY, AVERAGE).
        tags:        Optional grouping labels (e.g. ["income_statement"]).
        description: Human-readable description.

    Example:
        fpa.BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: my_db.query_revenue(ctx.period.start, ctx.period.end, ctx.scenario),
        )
    """
    name: str
    resolver: Callable
    agg_type: AggType = AggType.SUM
    tags: List[str] = field(default_factory=list)
    description: str = ""
    sql_expr: str = ""
    """
    Optional SQL aggregation expression for DuckDBCalculator.
    Use {start} and {end} as placeholders for the period's start and end dates.
    Example: "SUM(CASE WHEN account_id IN ('4000','4010') AND date BETWEEN '{start}' AND '{end}' THEN amount ELSE 0 END)"
    When provided, DuckDBCalculator uses this instead of the resolver for bulk resolution.
    The resolver is still required and is used when a plain Calculator is in use.
    """

    def __post_init__(self):
        if not callable(self.resolver):
            raise ValueError(f"BaseMeasure '{self.name}' resolver must be callable")

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, BaseMeasure) and self.name == other.name


@dataclass
class Measure:
    """
    A measure calculated from other measures.

    The formula receives a dict of {measure_name: float} for all declared
    dependencies and returns a float. Dependencies are resolved first,
    in DAG order, before the formula is called.

    Args:
        name:         Unique measure name.
        dependencies: Names of measures this formula depends on.
        formula:      Callable({dep_name: value, ...}) → float
        agg_type:     Aggregation behavior across time periods.
        tags:         Optional grouping labels.
        description:  Human-readable description.

    Example:
        fpa.Measure(
            name="Gross Profit",
            dependencies=["Revenue", "COGS"],
            formula=lambda v: v["Revenue"] - v["COGS"],
        )
    """
    name: str
    dependencies: List[str]
    formula: Callable[[dict[str, float]], float]
    agg_type: AggType = AggType.CALCULATED
    tags: List[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self):
        if not self.dependencies:
            raise ValueError(f"Measure '{self.name}' must declare at least one dependency")
        if not callable(self.formula):
            raise ValueError(f"Measure '{self.name}' formula must be callable")

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, Measure) and self.name == other.name


# Type alias used throughout the engine
AnyMeasure = BaseMeasure | Measure
