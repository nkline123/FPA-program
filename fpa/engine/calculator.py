from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..calendar.period import AggType, Period
from ..measures.measure import BaseMeasure, Measure, AnyMeasure
from ..measures.measure_registry import MeasureRegistry
from ..measures.dag import MeasureDAG
from .measure_values import MeasureValues


def _col_key(period: "Period", measure_name: str) -> str:
    """Internal DataFrame column key: '<period label>|<measure name>'."""
    return f"{period.label}|{measure_name}"


@dataclass(frozen=True)
class CalculationContext:
    """
    Bundles the inputs needed to resolve a single measure value.

    Passed to every BaseMeasure resolver so it has full context about what
    is being requested.  Frozen so it can be used as a dict key in the memo
    cache.

    Attributes:
        period:   The time period being resolved.
        scenario: The scenario label (e.g. "Actual", "Budget", "Forecast").
        filters:  Arbitrary key/value pairs for slicing data
                  (e.g. {"entity": "North", "department": "Engineering"}).
                  Stored as a tuple of sorted pairs so the dataclass stays
                  hashable.
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

    DuckDB is the primary execution path.  When a connection is provided,
    both build_table and build_breakdown_table route base measure resolution
    through DuckDB: the engine wraps each BaseMeasure's sql query as a
    subquery, generates FILTER (WHERE date_col BETWEEN … AND …) expressions
    for every requested period, and executes one query per base measure.
    Derived measures are then computed as vectorized pandas operations.

    Without a connection (or for base measures that only declare a resolver),
    the Python resolver path is used: one resolver call per (measure, period)
    cell, memoized.

    Args:
        registry:     The MeasureRegistry containing all measure definitions.
        connection:   Optional open duckdb.DuckDBPyConnection.  When provided,
                      enables the DuckDB execution path for all base measures
                      that declare a sql query.
        date_col:     Default column name for transaction / event dates.
                      Used when a BaseMeasure does not set its own date_col.
                      Default: "date".
        scenario_col: Column name for the scenario label.  Default: "scenario".
        calendar:     Optional FiscalCalendar.  Required only when any Measure
                      formula uses time-shifted lookups (v["Revenue", -12]).
                      Unused otherwise — existing formulas are unaffected.

    Examples:
        # DuckDB path — primary
        import duckdb
        con = duckdb.connect("warehouse.duckdb")
        calc = fpa.Calculator(registry, connection=con, calendar=calendar)

        # Python path — no database required (BaseMeasures need resolver)
        calc = fpa.Calculator(registry, calendar=calendar)
    """

    def __init__(
        self,
        registry: MeasureRegistry,
        connection=None,
        date_col: str = "date",
        scenario_col: str = "scenario",
        calendar=None,
    ):
        self._registry = registry
        self._dag = MeasureDAG(registry)
        self._registry_names = frozenset(registry.names())
        self._memo: Dict[Tuple[str, CalculationContext], float] = {}
        self._con = connection
        self._date_col = date_col
        self._scenario_col = scenario_col
        self._calendar = calendar

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, measure_name: str, context: CalculationContext) -> float:
        """
        Return the value of measure_name for the given context.

        Results are memoized: repeated calls with the same (measure, context)
        pair are free.  Requires a resolver on BaseMeasure when no DuckDB
        connection is available.
        """
        self._ensure_dag_current()
        return self._resolve_unchecked(measure_name, context)

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

        When a DuckDB connection is available and at least one base measure in
        the dependency chain declares a sql query, executes one SQL query per
        base measure (no GROUP BY) to fetch scalar values for all periods at
        once.  Measures without sql are filled via their Python resolver.
        Derived measures are computed as vectorized pandas operations.

        Falls back to the Python resolver path when no connection is present.
        """
        self._ensure_dag_current()
        all_needed = self._measures_needed(measure_names)
        if self._con and self._any_base_has_sql(all_needed):
            return self._build_table_duckdb(measure_names, periods, scenario, all_needed, **filters)

        data = {}
        for period in periods:
            ctx = CalculationContext.make(period=period, scenario=scenario, **filters)
            data[period.label] = {
                name: self._resolve_unchecked(name, ctx) for name in measure_names
            }
        return pd.DataFrame(data, index=measure_names)

    def build_breakdown_table(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        dimension: str,
        dimension_values: Optional[List[Any]] = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Resolve a single measure broken down by a dimension across periods.

        Returns a DataFrame with dimension values as rows and period labels
        as columns.

        DuckDB path (requires connection + at least one base measure with sql):
          Executes one SQL query per base measure using GROUP BY dimension,
          covering all periods and all dimension values in a single scan.
          dimension_values is optional — omit it to let DuckDB return every
          group present in the data.  High-cardinality dimensions (100K+
          distinct values) are handled natively by DuckDB without any
          parameter-list explosion.

        Python path (no connection, or no sql on any required base measure):
          One resolver call per (dimension_value, period) cell.
          dimension_values is required on the Python path.

        Args:
            measure_name:     The measure to resolve (base or derived).
            periods:          Time periods (columns in the result).
            scenario:         Scenario label.
            dimension:        Column to group rows by (e.g. "department").
            dimension_values: Optional list of specific values to return.
                              Pass None to return all groups from the data
                              (DuckDB path only).
            **filters:        Additional fixed filters applied to every cell.
        """
        self._ensure_dag_current()
        all_needed = self._measures_needed([measure_name])
        if self._con and self._any_base_has_sql(all_needed):
            return self._breakdown_duckdb(
                measure_name, periods, scenario, all_needed,
                dimension, dimension_values, **filters
            )
        return self._breakdown_python(
            measure_name, periods, scenario,
            dimension, dimension_values, **filters
        )

    def clear_cache(self) -> None:
        """Clear the memo cache.  Call after underlying data changes."""
        self._memo.clear()

    def _ensure_dag_current(self) -> None:
        """Rebuild the DAG if the registry's measure set has changed since init."""
        current_names = frozenset(self._registry.names())
        if current_names != self._registry_names:
            self._dag = MeasureDAG(self._registry)
            self._registry_names = current_names

    # ------------------------------------------------------------------
    # Python resolution path
    # ------------------------------------------------------------------

    def _resolve_unchecked(self, measure_name: str, context: CalculationContext) -> float:
        """Resolve without re-running _ensure_dag_current — for internal loops."""
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

    def _resolve_base(self, measure: BaseMeasure, context: CalculationContext) -> float:
        if measure.resolver is not None:
            try:
                result = measure.resolver(context)
            except Exception as e:
                raise RuntimeError(
                    f"Resolver error in BaseMeasure '{measure.name}': {e}"
                ) from e
            return 0.0 if result is None else float(result)

        if self._con and measure.sql:
            # Single-period scalar query — used by resolve() and time-shifted lookups
            date_col = measure.date_col or self._date_col
            sql = measure.sql.rstrip().rstrip(";")
            agg_expr = self._agg_expr(measure.agg_type, measure.value_col, date_col, context.period)
            where_parts = [f'"{self._scenario_col}" = ?']
            params: List[Any] = [context.scenario]
            for k, v in context.filters:
                where_parts.append(f'"{k}" = ?')
                params.append(v)
            query = (
                f"SELECT {agg_expr} FROM ({sql}) __base "
                f"WHERE {' AND '.join(where_parts)}"
            )
            result = self._con.execute(query, params).fetchone()[0]
            return 0.0 if result is None else float(result)

        raise RuntimeError(
            f"BaseMeasure '{measure.name}' has no resolver. "
            "Provide a resolver for the Python path, or use a "
            "Calculator with a DuckDB connection."
        )

    def _resolve_derived(self, measure: Measure, context: CalculationContext) -> float:
        dep_values = {dep: self._resolve_unchecked(dep, context) for dep in measure.dependencies}
        return measure.formula(MeasureValues(dep_values, self, context))

    def _breakdown_python(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        dimension: str,
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> pd.DataFrame:
        if dimension_values is None:
            raise ValueError(
                "dimension_values is required on the Python path. "
                "Provide explicit dimension values or use a Calculator "
                "with a DuckDB connection to enumerate groups automatically."
            )
        data = {}
        for value in dimension_values:
            row = {}
            for period in periods:
                ctx = CalculationContext.make(
                    period=period,
                    scenario=scenario,
                    **{dimension: value, **filters},
                )
                row[period.label] = self._resolve_unchecked(measure_name, ctx)
            data[value] = row
        return pd.DataFrame(data).T

    # ------------------------------------------------------------------
    # DuckDB resolution path
    # ------------------------------------------------------------------

    def _build_table_duckdb(
        self,
        measure_names: List[str],
        periods: List[Period],
        scenario: str,
        all_needed: List[str],
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Resolve measures × periods via DuckDB with no GROUP BY.

        Runs one SQL query per base measure (no dimension), getting scalar
        aggregates for all periods at once.  Resolver-only base measures are
        filled via _fill_python_base_columns.  Derived measures are computed
        as vectorized pandas operations.
        """
        base_sql_names = [
            n for n in all_needed
            if isinstance(self._registry.get(n), BaseMeasure)
            and bool(self._registry.get(n).sql)
        ]

        raw = self._sql_fetch(
            base_sql_names, periods, scenario,
            dimension=None, dimension_values=None, **filters
        )

        self._fill_python_base_columns(
            raw, all_needed, periods, scenario,
            dimension=None, dimension_values=None, **filters
        )
        raw = self._add_derived_columns(raw, all_needed, periods, scenario, filters)

        data = {}
        for period in periods:
            col_values = {}
            for name in measure_names:
                key = _col_key(period, name)
                col_values[name] = float(raw[key].iloc[0]) if key in raw.columns else 0.0
            data[period.label] = col_values

        return pd.DataFrame(data, index=measure_names)

    def _breakdown_duckdb(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        all_needed: List[str],
        dimension: str,
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Resolve a measure broken down by dimension via DuckDB.

        Runs one SQL query per base measure with GROUP BY dimension, covering
        all periods simultaneously.  Resolver-only base measures are filled
        via _fill_python_base_columns.  Derived measures are computed as
        vectorized pandas operations.
        """
        base_sql_names = [
            n for n in all_needed
            if isinstance(self._registry.get(n), BaseMeasure)
            and bool(self._registry.get(n).sql)
        ]

        raw = self._sql_fetch(
            base_sql_names, periods, scenario,
            dimension=dimension, dimension_values=dimension_values, **filters
        )

        self._fill_python_base_columns(
            raw, all_needed, periods, scenario,
            dimension=dimension, dimension_values=dimension_values, **filters
        )
        raw = self._add_derived_columns(raw, all_needed, periods, scenario, filters, dimension)

        target_cols = [_col_key(period, measure_name) for period in periods]
        result = raw[target_cols].copy()
        result.columns = [period.label for period in periods]

        if dimension_values is not None:
            result = result.reindex([str(v) for v in dimension_values]).fillna(0.0)
            result.index = dimension_values
        else:
            result.index.name = None

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
        """
        Execute one SQL query per base measure and return a combined DataFrame.

        Each query wraps the measure's sql as a subquery, applies scenario /
        filter / dimension WHERE clauses (parameterized), and aggregates
        value_col per period using FILTER (WHERE date_col BETWEEN start AND end).

        Columns are named "{period.label}|{measure_name}".
        With a dimension:  indexed by dimension value (string).
        Without dimension: single-row DataFrame (for build_table).

        Period start/end dates are embedded as literals because they come from
        FiscalCalendar — not user input — and are always valid ISO date strings.
        Scenario, filter values, and dimension values are always parameterized.
        """
        if not base_measure_names:
            return pd.DataFrame()

        frames: List[pd.DataFrame] = []

        for name in base_measure_names:
            m = self._registry.get(name)
            date_col = m.date_col or self._date_col
            sql = m.sql.rstrip().rstrip(";")

            # Build SELECT expressions — one per period, embedded date literals
            select_parts = [
                f'{self._agg_expr(m.agg_type, m.value_col, date_col, p)} AS "{p.label}"'
                for p in periods
            ]

            # Build WHERE clause — parameterized for all user-supplied values
            where_parts = [f'"{self._scenario_col}" = ?']
            params: List[Any] = [scenario]

            for k, v in filters.items():
                where_parts.append(f'"{k}" = ?')
                params.append(v)

            if dimension and dimension_values is not None:
                placeholders = ", ".join("?" * len(dimension_values))
                where_parts.append(f'"{dimension}" IN ({placeholders})')
                params.extend(dimension_values)

            where_clause = " AND ".join(where_parts)
            select_clause = ", ".join(select_parts)

            if dimension:
                query = (
                    f'SELECT "{dimension}", {select_clause} '
                    f"FROM ({sql}) __base "
                    f"WHERE {where_clause} "
                    f'GROUP BY "{dimension}"'
                )
            else:
                query = (
                    f"SELECT {select_clause} "
                    f"FROM ({sql}) __base "
                    f"WHERE {where_clause}"
                )

            df = self._con.execute(query, params).df()

            if dimension:
                df = df.set_index(dimension)
                df.index = df.index.astype(str)

            # Prefix columns so multiple measures can coexist in one DataFrame
            df.columns = [f"{col}|{name}" for col in df.columns]
            frames.append(df)

        result = pd.concat(frames, axis=1).fillna(0.0)
        return result

    @staticmethod
    def _agg_expr(
        agg_type: AggType,
        value_col: str,
        date_col: str,
        period: Period,
    ) -> str:
        """
        Return the SQL aggregation expression for one measure × one period.

        Period dates are embedded as ISO literals (safe — they come from
        FiscalCalendar, not user input).
        """
        start = period.start   # datetime.date → "YYYY-MM-DD" in f-string
        end = period.end
        date_filter = f'"{date_col}" BETWEEN \'{start}\' AND \'{end}\''

        if agg_type == AggType.LAST_DAY:
            # arg_max returns value_col from the row with the latest date_col
            # within the period — correct for headcount, balances, ARR, etc.
            return f'COALESCE(arg_max("{value_col}", "{date_col}") FILTER (WHERE {date_filter}), 0.0)'
        elif agg_type == AggType.AVERAGE:
            return f'COALESCE(AVG("{value_col}") FILTER (WHERE {date_filter}), 0.0)'
        else:
            # SUM
            return f'COALESCE(SUM("{value_col}") FILTER (WHERE {date_filter}), 0.0)'

    def _fill_python_base_columns(
        self,
        df: pd.DataFrame,
        all_needed: List[str],
        periods: List[Period],
        scenario: str,
        dimension: Optional[str],
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> None:
        """
        Fill columns for base measures that have no sql (resolver-only).

        These measures cannot be fetched via SQL, so their values are
        resolved one cell at a time using the Python resolver and inserted
        into the shared DataFrame.
        """
        python_only = [
            n for n in all_needed
            if isinstance(self._registry.get(n), BaseMeasure)
            and not bool(self._registry.get(n).sql)
        ]
        if not python_only:
            return

        for period in periods:
            for name in python_only:
                col = _col_key(period, name)
                if col in df.columns:
                    continue

                if dimension and dimension_values is not None:
                    values_map = {
                        str(dv): self._resolve_unchecked(
                            name,
                            CalculationContext.make(
                                period=period, scenario=scenario,
                                **{dimension: dv, **filters}
                            ),
                        )
                        for dv in dimension_values
                    }
                    df[col] = df.index.map(values_map).astype(float)
                elif dimension:
                    df[col] = [
                        self._resolve_unchecked(
                            name,
                            CalculationContext.make(
                                period=period, scenario=scenario,
                                **{dimension: dv, **filters}
                            ),
                        )
                        for dv in df.index
                    ]
                else:
                    ctx = CalculationContext.make(
                        period=period, scenario=scenario, **filters
                    )
                    df[col] = self._resolve_unchecked(name, ctx)

    def _add_derived_columns(
        self,
        df: pd.DataFrame,
        all_needed: List[str],
        periods: List[Period],
        scenario: str = "",
        filters: Optional[dict] = None,
        dimension: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Compute derived measure columns in DAG order and concat in one shot.

        Attempts vectorized pandas arithmetic first.  Falls back to row-wise
        .apply() for formulas that include Python conditionals (e.g.
        ``if v['Revenue'] else 0``) or time-shifted lookups (v["Revenue", -12])
        which cannot be vectorized.

        Returns the DataFrame with derived columns appended (new object when
        any derived columns were added, same object otherwise).
        """
        new_cols: Dict[str, Any] = {}

        for name in all_needed:
            m = self._registry.get(name)
            if not isinstance(m, Measure):
                continue
            for period in periods:
                col = _col_key(period, name)
                if col in df.columns or col in new_cols:
                    continue
                dep_series = {
                    dep: new_cols.get(_col_key(period, dep), df.get(_col_key(period, dep)))
                    for dep in m.dependencies
                }
                try:
                    new_cols[col] = m.formula(dep_series)
                except (ValueError, TypeError, KeyError, AttributeError):
                    # KeyError: formula used tuple key v["Revenue", -12] on plain dict
                    combined = df.assign(
                        **{k: v for k, v in new_cols.items() if k not in df.columns}
                    )
                    new_cols[col] = combined.apply(
                        self._make_row_applier(m, period, scenario, filters, dimension),
                        axis=1,
                    )

        if new_cols:
            return pd.concat(
                [df, pd.DataFrame(new_cols, index=df.index)], axis=1
            )
        return df

    def _make_row_applier(
        self,
        measure: Measure,
        period: Period,
        scenario: str,
        filters: Optional[dict],
        dimension: Optional[str],
    ):
        """Return a row → float callable for use with DataFrame.apply."""
        def _apply(row):
            dim_filter = {dimension: row.name} if dimension else {}
            ctx = CalculationContext.make(
                period=period,
                scenario=scenario,
                **dim_filter,
                **(filters or {}),
            )
            dep_values = {dep: row[_col_key(period, dep)] for dep in measure.dependencies}
            return measure.formula(MeasureValues(dep_values, self, ctx))
        return _apply

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _any_base_has_sql(self, all_needed: List[str]) -> bool:
        """Return True if any base measure in the dependency chain has sql."""
        return any(
            bool(m.sql)
            for n in all_needed
            if isinstance(m := self._registry.get(n), BaseMeasure)
        )

    def _measures_needed(self, names: List[str]) -> List[str]:
        """Return names + all transitive dependencies in evaluation order."""
        needed = set(names)
        for n in names:
            needed.update(self._dag.all_dependencies_of(n))
        order = self._dag.evaluation_order()
        return [n for n in order if n in needed]
