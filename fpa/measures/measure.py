from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from ..calendar.period import AggType


_MEASURE_REF_RE = re.compile(r'\bmeasure\.(\w+)\b', re.IGNORECASE)


def _sql_measure_refs(sql: str) -> List[str]:
    """Return measure names referenced as measure.<name> in a SQL string."""
    return _MEASURE_REF_RE.findall(sql)


@dataclass(frozen=True)
class Measure:
    """
    A single measure — SQL-backed (leaf or composed) or Python-derived.

    Three execution paths, determined by which fields are set:

    **Leaf SQL measure** — SQL references a real table directly.
    The engine wraps it in a CTE, injects period FILTER aggregations, and
    applies scenario / filter / dimension WHERE clauses automatically.

        Measure(
            name="Expense",
            sql="SELECT * FROM gl WHERE account_id IN ('6000','6010')",
            value_col="amount",
            date_col="date",
            agg_type=AggType.SUM,
            scenario_col="scenario",   # defaults to Calculator.scenario_col if omitted
        )

    **Pre-scoped measure** — use ``scenario`` when the source data has no
    scenario column.  The SQL itself filters the data to one scenario; set
    ``scenario`` to label which scenario it represents.  The engine will not
    inject a scenario WHERE clause, so ``scenario_col`` must be omitted.

        Measure(
            name="Actual Revenue",
            sql="SELECT * FROM gl WHERE scenario = 'Actual' AND account_type = 'Income'",
            value_col="amount",
            date_col="date",
            agg_type=AggType.SUM,
            scenario="Actual",         # label only — no WHERE injected by the engine
        )

    **Composed SQL measure** — SQL references another measure via
    ``measure.<name>``.  The engine builds a CTE chain so the parent
    measure's data is available without re-scanning the source table.
    ``value_col``, ``date_col``, ``agg_type``, and ``scenario_col`` are
    inherited from the nearest SQL ancestor that defines them; override
    them here only when the aggregation genuinely changes.

    .. note::
        Measure names used in ``measure.<name>`` SQL references must contain
        only word characters (``[a-zA-Z0-9_]``).  Names with spaces or
        special characters can be registered and used as formula
        ``dependencies``, but cannot be composed via SQL.

        Measure(
            name="Sales & Marketing Expense",
            sql="SELECT * FROM measure.Expense WHERE department IN ('Sales', 'Marketing')",
            # value_col / date_col / agg_type / scenario_col inherited from Expense
        )

    **Python-derived measure** — computed from other resolved measures.
    The formula receives a MeasureValues dict-like object.

        Measure(
            name="Gross Margin %",
            dependencies=["Gross Profit", "Revenue"],
            formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100)
                              if v["Revenue"] else 0.0,
        )

    A ``resolver`` can be combined with ``sql`` to provide a Python
    fallback when no DuckDB connection is available (useful in tests).

    Args:
        name:         Unique measure name.
        sql:          SQL SELECT query.  Reference real tables for leaf
                      measures; use ``measure.<name>`` to compose on top
                      of another measure.
        value_col:    Column to aggregate.  Required on leaf measures;
                      inherited from the nearest SQL ancestor on composed
                      measures.
        date_col:     Date column for period filtering.  Inherited from
                      the nearest SQL ancestor when omitted.
        scenario_col: Column holding the scenario label (e.g. "scenario",
                      "version").  Defaults to the Calculator's
                      ``scenario_col`` argument.  Inherited from the
                      nearest SQL ancestor when omitted.
        agg_type:     Aggregation type (SUM / AVERAGE / LAST_DAY /
                      CUMULATIVE_END / CUMULATIVE_START).  Inherited from
                      the nearest SQL ancestor when omitted.
        scenario:     Label for pre-scoped measures whose SQL already filters
                      to a single scenario.  When set, the engine skips the
                      scenario WHERE clause entirely.  Cannot be combined
                      with ``scenario_col``.
        dependencies: Measure names required by ``formula``.
        formula:      Callable(MeasureValues) → float.
        resolver:     Scalar Python fallback callable(CalculationContext)
                      → float.  Combined with ``sql`` for test fallback;
                      used alone for resolver-only measures.
        tags:         Optional grouping labels.
        description:  Human-readable description.
    """

    name: str
    # SQL path
    sql: str = ""
    value_col: str = ""
    date_col: str = ""
    scenario_col: str = ""           # column that holds the scenario label
    agg_type: Optional[AggType] = None
    scenario: Optional[str] = None   # locks this measure to a specific scenario
    # Python formula path
    dependencies: List[str] = field(default_factory=list)
    formula: Optional[Callable] = None
    # Python resolver (scalar fallback, or resolver-only measure)
    resolver: Optional[Callable] = None
    # metadata
    tags: List[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self):
        has_sql      = bool(self.sql)
        has_formula  = self.formula is not None
        has_resolver = self.resolver is not None

        if not (has_sql or has_formula or has_resolver):
            raise ValueError(
                f"Measure '{self.name}' requires sql, formula, or resolver."
            )
        if has_formula and has_resolver:
            raise ValueError(
                f"Measure '{self.name}' cannot combine formula and resolver. "
                "Use formula for computed measures, resolver for scalar lookups."
            )
        if has_sql and has_formula:
            raise ValueError(
                f"Measure '{self.name}' cannot combine sql with formula. "
                "Use sql for SQL-backed measures, formula for Python-derived measures."
            )
        if has_formula and not self.dependencies:
            raise ValueError(
                f"Measure '{self.name}' with formula must declare at least one dependency."
            )
        if has_sql and self.agg_type == AggType.CALCULATED:
            raise ValueError(
                f"Measure '{self.name}' is a SQL measure and cannot use AggType.CALCULATED. "
                "Use SUM, AVERAGE, or LAST_DAY instead."
            )
        # Leaf SQL measures (no measure.X refs) require value_col, and scenario_col or scenario
        if has_sql and not _sql_measure_refs(self.sql):
            if not self.value_col:
                raise ValueError(
                    f"Measure '{self.name}' is a leaf SQL measure and requires value_col."
                )
            if not self.scenario_col and self.scenario is None:
                raise ValueError(
                    f"Measure '{self.name}' is a leaf SQL measure and requires either "
                    "scenario_col (the column to filter on) or scenario (a label when "
                    "the data has no scenario column)."
                )
            if self.scenario_col and self.scenario is not None:
                raise ValueError(
                    f"Measure '{self.name}' cannot set both scenario_col and scenario. "
                    "Use scenario_col when the data has a scenario column to filter on, "
                    "or scenario when the data has no scenario column."
                )

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, Measure) and self.name == other.name


# Kept for backward compatibility
AnyMeasure = Measure
