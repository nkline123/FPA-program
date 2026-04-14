from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..calendar.period import Period
from ..measures.measure import BaseMeasure, Measure, AnyMeasure
from ..measures.measure_registry import MeasureRegistry
from ..measures.dag import MeasureDAG


@dataclass(frozen=True)
class CalculationContext:
    """
    Bundles the inputs needed to resolve a single measure value.

    Passed to every BaseMeasure resolver so it has full context about
    what is being requested. Frozen so it can be used as a dict key
    in the memo cache.

    Attributes:
        period:   The time period being resolved.
        scenario: The scenario label (e.g. "Actual", "Budget", "Forecast").
        filters:  Arbitrary key/value pairs the resolver can use to slice data
                  (e.g. {"entity": "North", "department": "Engineering"}).
                  Stored as a tuple of sorted pairs so the dataclass stays hashable.
    """
    period: Period
    scenario: str
    filters: tuple = field(default_factory=tuple)  # tuple of (key, value) pairs

    @classmethod
    def make(
        cls,
        period: Period,
        scenario: str,
        **filters: Any,
    ) -> "CalculationContext":
        """Convenience constructor — pass filters as keyword arguments."""
        return cls(
            period=period,
            scenario=scenario,
            filters=tuple(sorted(filters.items())),
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a filter value by key."""
        return dict(self.filters).get(key, default)


class Calculator:
    """
    Resolves measure values for a given CalculationContext.

    Base measures are resolved by calling their resolver callable.
    Derived measures are resolved by evaluating their formula over
    already-resolved dependencies, in DAG order.

    Results are memoized per (measure_name, context) so each cell
    is computed only once regardless of how many measures depend on it.
    """

    def __init__(self, registry: MeasureRegistry):
        self._registry = registry
        self._dag = MeasureDAG(registry)
        self._memo: Dict[Tuple[str, CalculationContext], float] = {}

    def resolve(self, measure_name: str, context: CalculationContext) -> float:
        """
        Return the value of `measure_name` for the given context.
        Results are memoized: repeated calls with the same arguments are free.
        """
        cache_key = (measure_name, context)
        if cache_key in self._memo:
            return self._memo[cache_key]

        measure = self._registry.get(measure_name)

        if isinstance(measure, BaseMeasure):
            value = self._resolve_base(measure, context)
        elif isinstance(measure, Measure):
            value = self._resolve_derived(measure, context)
        else:
            raise TypeError(f"Unknown measure type: {type(measure)}")

        self._memo[cache_key] = value
        return value

    def resolve_many(
        self,
        measure_names: List[str],
        context: CalculationContext,
    ) -> Dict[str, float]:
        """Resolve multiple measures for the same context."""
        return {name: self.resolve(name, context) for name in measure_names}

    def build_table(
        self,
        measure_names: List[str],
        periods: List[Period],
        scenario: str,
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Resolve a list of measures across a list of periods.

        Returns a DataFrame with measures as rows and period labels as columns.
        Pass any additional keyword arguments as filters (e.g. entity="North").
        """
        data = {}
        for period in periods:
            ctx = CalculationContext.make(period=period, scenario=scenario, **filters)
            data[period.label] = {
                name: self.resolve(name, ctx) for name in measure_names
            }
        return pd.DataFrame(data, index=measure_names)

    def build_breakdown_table(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        dimension: str,
        dimension_values: List[Any],
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Resolve a single measure broken down by a dimension across periods.

        Returns a DataFrame with dimension values as rows and period labels
        as columns. Each cell is the measure value for that (dimension value,
        period) combination.

        The resolver receives the dimension value as a filter key, e.g.
        ctx.get("customer") if dimension="customer". It is responsible for
        filtering its data source accordingly.

        Args:
            measure_name:     The measure to resolve.
            periods:          Time periods (columns).
            scenario:         Scenario label.
            dimension:        Filter key to vary across rows (e.g. "customer").
            dimension_values: The values to iterate over (e.g. ["Acme", "Globex"]).
            **filters:        Additional fixed filters applied to every cell.
        """
        data = {}
        for value in dimension_values:
            row = {}
            for period in periods:
                ctx = CalculationContext.make(
                    period=period,
                    scenario=scenario,
                    **{dimension: value, **filters},
                )
                row[period.label] = self.resolve(measure_name, ctx)
            data[value] = row
        return pd.DataFrame(data).T

    def clear_cache(self) -> None:
        """Clear the memo cache. Call after underlying data changes."""
        self._memo.clear()

    # ------------------------------------------------------------------
    # Internal resolution
    # ------------------------------------------------------------------

    def _resolve_base(self, measure: BaseMeasure, context: CalculationContext) -> float:
        """Call the measure's resolver and return the result."""
        try:
            result = measure.resolver(context)
        except Exception as e:
            raise RuntimeError(
                f"Resolver error in BaseMeasure '{measure.name}': {e}"
            )
        if result is None:
            return 0.0
        return float(result)

    def _resolve_derived(self, measure: Measure, context: CalculationContext) -> float:
        """Resolve all dependencies then evaluate the formula."""
        dep_values = {
            dep: self.resolve(dep, context)
            for dep in measure.dependencies
        }
        return measure.formula(dep_values)
