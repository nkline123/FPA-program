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
        return next((v for k, v in self.filters if k == key), default)


class Calculator:
    """
    Resolves measure values for a given CalculationContext.

    Without a DuckDB connection, base measures are resolved by calling their
    resolver callable and derived measures are computed in DAG order. Results
    are memoized per (measure_name, context).

    With a DuckDB connection and table, build_breakdown_table executes a single
    SQL query for all base measures that declare sql_expr, then computes derived
    measures as vectorized pandas operations. build_table always uses the Python
    path — it has no dimension axis to GROUP BY so SQL offers no advantage.

    Args:
        registry:     The MeasureRegistry.
        connection:   Optional open duckdb.Connection. When provided together
                      with table, enables the SQL execution path for breakdowns.
        table:        Name of the GL table in DuckDB.
        date_col:     Column name for transaction date (default "date").
        scenario_col: Column name for scenario (default "scenario").

    Examples:
        # Python-only (always works):
        calc = fpa.Calculator(registry)

        # With DuckDB (faster breakdowns when sql_expr is set on BaseMeasures):
        calc = fpa.Calculator(registry, connection=con, table="gl")
    """

    def __init__(
        self,
        registry: MeasureRegistry,
        connection=None,
        table: str = "",
        date_col: str = "date",
        scenario_col: str = "scenario",
    ):
        self._registry = registry
        self._dag = MeasureDAG(registry)
        self._memo: Dict[Tuple[str, CalculationContext], float] = {}
        self._con = connection
        self._table = table
        self._date_col = date_col
        self._scenario_col = scenario_col

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        Always uses the Python resolver path — build_table has no dimension axis
        to GROUP BY so a SQL query offers no throughput advantage over dict lookups.
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

        Returns a DataFrame with dimension values as rows and period labels as columns.

        When a DuckDB connection and table were provided at construction and the
        measure's base dependencies declare sql_expr, executes one SQL query for
        all base measures across all periods and dimension values, then computes
        derived measures as vectorized pandas operations.

        Otherwise falls back to the Python resolver path (one resolver call per cell).

        Args:
            measure_name:     The measure to resolve.
            periods:          Time periods (columns).
            scenario:         Scenario label.
            dimension:        Filter key to vary across rows (e.g. "customer_id").
            dimension_values: The values to iterate over.
            **filters:        Additional fixed filters applied to every cell.
        """
        if self._con and self._table:
            all_needed = self._measures_needed([measure_name])
            base_sql = [n for n in all_needed if self._has_sql_expr(n)]
            if base_sql:
                return self._breakdown_duckdb(
                    measure_name, base_sql, periods, scenario,
                    dimension, dimension_values, **filters
                )
        return self._breakdown_python(
            measure_name, periods, scenario, dimension, dimension_values, **filters
        )

    def clear_cache(self) -> None:
        """Clear the memo cache. Call after underlying data changes."""
        self._memo.clear()

    # ------------------------------------------------------------------
    # Python resolution path
    # ------------------------------------------------------------------

    def _resolve_base(self, measure: BaseMeasure, context: CalculationContext) -> float:
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
        dep_values = {dep: self.resolve(dep, context) for dep in measure.dependencies}
        return measure.formula(dep_values)

    def _breakdown_python(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        dimension: str,
        dimension_values: List[Any],
        **filters: Any,
    ) -> pd.DataFrame:
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

    # ------------------------------------------------------------------
    # DuckDB resolution path
    # ------------------------------------------------------------------

    def _breakdown_duckdb(
        self,
        measure_name: str,
        base_sql_names: List[str],
        periods: List[Period],
        scenario: str,
        dimension: str,
        dimension_values: List[Any],
        **filters: Any,
    ) -> pd.DataFrame:
        raw = self._sql_fetch(base_sql_names, periods, scenario,
                              dimension=dimension, dimension_values=dimension_values, **filters)
        raw = raw.set_index(dimension)
        raw.index = raw.index.astype(str)

        self._fill_python_base_columns(raw, [measure_name], periods, scenario,
                                       dimension=dimension, dimension_values=dimension_values, **filters)
        self._add_derived_columns(raw, [measure_name], periods)

        target_cols = [f"{period.label}|{measure_name}" for period in periods]
        result = raw[target_cols].copy()
        result.columns = [period.label for period in periods]
        result = result.reindex([str(v) for v in dimension_values]).fillna(0.0)
        result.index = dimension_values
        return result

    def _sql_fetch(
        self,
        base_measure_names: List[str],
        periods: List[Period],
        scenario: str,
        dimension: Optional[str],
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> pd.DataFrame:
        """Execute one SQL query returning all base measure values."""
        select_parts = []
        for period in periods:
            for name in base_measure_names:
                expr = self._registry.get(name).sql_expr.format(
                    start=period.start, end=period.end
                )
                select_parts.append(f'{expr} AS "{period.label}|{name}"')

        where_parts = [f"{self._scenario_col} = '{scenario}'"]
        for k, v in filters.items():
            where_parts.append(f"{k} = '{v}'")
        where_clause = " AND ".join(where_parts)

        if dimension and dimension_values:
            vals = ", ".join(f"'{v}'" for v in dimension_values)
            query = f"""
                SELECT {dimension}, {", ".join(select_parts)}
                FROM {self._table}
                WHERE {where_clause} AND {dimension} IN ({vals})
                GROUP BY {dimension}
            """
        else:
            query = f"""
                SELECT {", ".join(select_parts)}
                FROM {self._table}
                WHERE {where_clause}
            """
        return self._con.execute(query).df()

    def _fill_python_base_columns(
        self,
        df: pd.DataFrame,
        measure_names: List[str],
        periods: List[Period],
        scenario: str,
        dimension: Optional[str],
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> None:
        """Add columns for base measures that have no sql_expr via Python resolver."""
        python_base = [
            n for n in self._measures_needed(measure_names)
            if isinstance(self._registry.get(n), BaseMeasure) and not self._has_sql_expr(n)
        ]
        if not python_base:
            return

        for period in periods:
            for name in python_base:
                col = f"{period.label}|{name}"
                if col in df.columns:
                    continue
                if dimension and dimension_values:
                    values = {
                        str(dv): self.resolve(
                            name,
                            CalculationContext.make(period=period, scenario=scenario,
                                                    **{dimension: dv, **filters})
                        )
                        for dv in dimension_values
                    }
                    df[col] = df.index.map(values).astype(float)
                else:
                    ctx = CalculationContext.make(period=period, scenario=scenario, **filters)
                    df[col] = self.resolve(name, ctx)

    def _add_derived_columns(
        self,
        df: pd.DataFrame,
        measure_names: List[str],
        periods: List[Period],
    ) -> None:
        """
        Compute derived measure columns in DAG order, concat in one shot.
        Tries vectorized pandas arithmetic; falls back to row-wise apply for
        formulas with Python conditionals (e.g. "if v['Rev'] else 0").
        """
        all_needed = self._measures_needed(measure_names)
        new_cols: Dict[str, Any] = {}

        for name in all_needed:
            m = self._registry.get(name)
            if not isinstance(m, Measure):
                continue
            for period in periods:
                col = f"{period.label}|{name}"
                if col in df.columns or col in new_cols:
                    continue
                dep_series = {
                    dep: new_cols.get(f"{period.label}|{dep}", df.get(f"{period.label}|{dep}"))
                    for dep in m.dependencies
                }
                try:
                    new_cols[col] = m.formula(dep_series)
                except (ValueError, TypeError):
                    combined = df.assign(**{k: v for k, v in new_cols.items() if k not in df.columns})
                    new_cols[col] = combined.apply(
                        lambda row, m=m, period=period: m.formula(
                            {dep: row[f"{period.label}|{dep}"] for dep in m.dependencies}
                        ),
                        axis=1,
                    )

        if new_cols:
            new_df = pd.concat([df, pd.DataFrame(new_cols, index=df.index)], axis=1)
            df.__dict__.update(new_df.__dict__)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _has_sql_expr(self, name: str) -> bool:
        m = self._registry.get(name)
        return isinstance(m, BaseMeasure) and bool(m.sql_expr)

    def _measures_needed(self, names: List[str]) -> List[str]:
        """Return names + all transitive dependencies in evaluation order."""
        needed = set(names)
        for n in names:
            needed.update(self._dag.all_dependencies_of(n))
        order = self._dag.evaluation_order()
        return [n for n in order if n in needed]
