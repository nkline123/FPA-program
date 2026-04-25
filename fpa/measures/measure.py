from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from ..calendar.period import AggType


@dataclass(frozen=True)
class BaseMeasure:
    """
    A leaf measure that fetches its value from a DuckDB table.

    Define the measure by writing a SQL SELECT query that filters the source
    table to the rows that contribute to this measure.  The engine wraps that
    query as a subquery, then automatically appends:

      - A WHERE clause for scenario, period date range, and any extra filters
        passed to build_table / build_breakdown_table.
      - A GROUP BY clause for the breakdown dimension (when requested).

    Every column produced by your SQL other than value_col and date_col is
    treated as a dimension and can be used as a breakdown axis with no
    additional configuration.

    Args:
        name:      Unique measure name.  Used as the key everywhere.
        sql:       A SQL SELECT query returning the rows for this measure.
                   Use SELECT * so that all dimension columns (entity,
                   department, account_id, …) are available for filtering
                   and GROUP BY.  Do NOT include WHERE clauses for date,
                   scenario, or dimension values — those are added by the
                   engine automatically.
                   Example:
                     "SELECT * FROM general_ledger WHERE account_type = 'Income'"
                   Trailing semicolons are stripped automatically.
                   Optional when resolver is provided (e.g. for unit tests
                   that run without a DuckDB connection).
        value_col: Column in the query result containing the numeric value
                   to aggregate.  Required when sql is set.
                   Example: "amount"
        date_col:  Column used to filter rows to the period date range.
                   Defaults to the Calculator's date_col if left blank.
                   Example: "period_enddate"
        agg_type:  How value_col is aggregated within a period:
                     SUM      → COALESCE(SUM(value_col), 0)
                     AVERAGE  → COALESCE(AVG(value_col), 0)
                     LAST_DAY → COALESCE(arg_max(value_col, date_col), 0)
                                Returns the value on the latest date in
                                the period — correct for headcount,
                                balance sheet, ARR, etc.
                   CALCULATED is reserved for derived Measures and is
                   rejected at construction time on a BaseMeasure.
        resolver:  Optional Python callable(CalculationContext) → float.
                   Used by the Python path (no DuckDB connection) and by
                   build_table when no connection is available.  If omitted,
                   a Calculator without a connection will raise RuntimeError
                   when this measure is resolved.
                   Return None to treat the value as 0.0.
        tags:      Optional grouping labels (e.g. ["income_statement"]).
        description: Human-readable description.

    Examples:
        # DuckDB-backed — primary style
        fpa.BaseMeasure(
            name="Revenue",
            sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
            value_col="amount",
            date_col="period_enddate",
            agg_type=fpa.AggType.SUM,
        )

        # Headcount — value at last day of the period
        fpa.BaseMeasure(
            name="Headcount",
            sql="SELECT * FROM hr_data WHERE status = 'Active'",
            value_col="employee_count",
            date_col="as_of_date",
            agg_type=fpa.AggType.LAST_DAY,
        )

        # Python resolver only — useful for testing without a database
        fpa.BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: lookup[(ctx.scenario, ctx.period.label)],
        )

        # Both — SQL for production, resolver as test fallback
        fpa.BaseMeasure(
            name="Revenue",
            sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
            value_col="amount",
            date_col="period_enddate",
            resolver=lambda ctx: 0.0,
        )
    """
    name: str
    sql: str = ""
    value_col: str = ""
    date_col: str = ""
    agg_type: AggType = AggType.SUM
    resolver: Optional[Callable] = None
    tags: List[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self):
        if not self.sql and self.resolver is None:
            raise ValueError(
                f"BaseMeasure '{self.name}' requires either sql or resolver (or both). "
                "Provide sql for the DuckDB path, resolver for the Python path."
            )
        if self.sql and not self.value_col:
            raise ValueError(
                f"BaseMeasure '{self.name}' has sql but no value_col. "
                "Set value_col to the column containing the numeric value to aggregate."
            )
        if self.resolver is not None and not callable(self.resolver):
            raise ValueError(f"BaseMeasure '{self.name}' resolver must be callable")
        if self.agg_type == AggType.CALCULATED:
            raise ValueError(
                f"BaseMeasure '{self.name}' cannot use AggType.CALCULATED. "
                "CALCULATED is only valid for derived Measures that compute a formula. "
                "Use SUM, AVERAGE, or LAST_DAY instead."
            )

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, BaseMeasure) and self.name == other.name


@dataclass(frozen=True)
class Measure:
    """
    A measure calculated from other measures.

    The formula receives a dict of {measure_name: float} for all declared
    dependencies and returns a float.  Dependencies are resolved first,
    in DAG order, before the formula is called.

    On the DuckDB path the engine attempts vectorized pandas arithmetic
    first and falls back to row-wise .apply() automatically for formulas
    that include Python conditionals (e.g. ``if v['Revenue'] else 0``).

    Args:
        name:         Unique measure name.
        dependencies: Names of measures this formula depends on.
                      At least one is required.
        formula:      Callable({dep_name: value, …}) → float.
        agg_type:     Aggregation behavior.  Default: CALCULATED.
        tags:         Optional grouping labels.
        description:  Human-readable description.

    Example:
        fpa.Measure(
            name="Gross Profit",
            dependencies=["Revenue", "COGS"],
            formula=lambda v: v["Revenue"] - v["COGS"],
        )

        fpa.Measure(
            name="Gross Margin %",
            dependencies=["Gross Profit", "Revenue"],
            formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100)
                              if v["Revenue"] else 0.0,
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
