from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from ..calendar.period import AggType, Period
from ..measures.measure import Measure, _sql_measure_refs, _MEASURE_REF_RE
from ..measures.measure_registry import MeasureRegistry
from ..measures.dag import MeasureDAG
from .measure_values import MeasureValues


def _col_key(period: "Period", measure_name: str) -> str:
    """Internal DataFrame column key: '<period label>|<measure name>'."""
    return f"{period.label}|{measure_name}"


def _filter_clause(key: str, value: Any) -> Tuple[str, List[Any]]:
    """Return (sql_fragment, params) for a single filter key/value pair.

    List or tuple values generate an IN clause; scalars generate = ?.
    """
    if isinstance(value, (list, tuple)):
        placeholders = ", ".join("?" * len(value))
        return f'"{key}" IN ({placeholders})', list(value)
    return f'"{key}" = ?', [value]


@dataclass(frozen=True)
class CalculationContext:
    """
    Bundles the inputs needed to resolve a single measure value.

    Frozen so it can be used as a dict key in the memo cache.

    Attributes:
        period:   The time period being resolved.
        scenario: The scenario label (e.g. "Actual", "Budget", "Forecast").
        filters:  Arbitrary key/value pairs for slicing data.
                  Stored as a tuple of sorted pairs for hashability.
    """
    period: Period
    scenario: str
    filters: tuple = field(default_factory=tuple)

    @classmethod
    def make(
        cls,
        period: Period,
        scenario: str,
        **filters: Any,
    ) -> "CalculationContext":
        """Convenience constructor — pass filters as keyword arguments.

        List values are converted to tuples so the context stays hashable
        and usable as a memo cache key.
        """
        normalised = {
            k: tuple(v) if isinstance(v, list) else v
            for k, v in filters.items()
        }
        return cls(
            period=period,
            scenario=scenario,
            filters=tuple(sorted(normalised.items())),
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a filter value by key."""
        return next((v for k, v in self.filters if k == key), default)


class Calculator:
    """
    Resolves measure values for a given CalculationContext.

    DuckDB is the primary execution path.  When a connection is provided,
    SQL measures (leaf and composed) are resolved via CTE-chained queries —
    one query per SQL measure covering all requested periods simultaneously.
    Composed measures reference their parent via ``measure.<name>`` in their
    SQL; the engine builds the full CTE chain automatically.

    Python-derived measures (formula + dependencies) are computed as
    vectorized pandas operations on the SQL results.

    Without a connection, all measures fall back to Python resolvers.

    Args:
        registry:     The MeasureRegistry containing all measure definitions.
        connection:   Optional open duckdb.DuckDBPyConnection.
        date_col:     Default date column name.  Used when a Measure does
                      not set its own date_col.  Default: "date".
        scenario_col: Column name for the scenario label.  Default: "scenario".
        calendar:     Optional FiscalCalendar.  Required for time-shifted
                      lookups (v["Revenue", -12]) in formulas.
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
        pair are free.
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
        """
        self._ensure_dag_current()
        all_needed = self._measures_needed(measure_names)
        if self._con and self._any_sql(all_needed):
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
        dimensions: str | List[str],
        dimension_values: Optional[List[Any]] = None,
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Resolve a single measure broken down by one or more dimensions across periods.

        Args:
            dimensions:       A single column name (str) or a list of column names
                              to group by.  When multiple dimensions are given the
                              result has a MultiIndex.
            dimension_values: Optional list that restricts which rows appear.
                              For a single dimension pass a list of scalars:
                                  ["North", "South"]
                              For multiple dimensions pass a list of tuples:
                                  [("North", "Sales"), ("South", "Marketing")]
                              When omitted the DuckDB path enumerates all groups
                              automatically; the Python path requires this to be set.

        Returns a DataFrame with dimension value(s) as the row index and period
        labels as columns.  A single dimension produces a plain Index; multiple
        dimensions produce a MultiIndex.

        DuckDB path: one CTE-chained query per SQL measure, GROUP BY all dimensions.
        Python path: one resolver call per (dimension combination, period) cell;
                     dimension_values is required.
        """
        if isinstance(dimensions, str):
            dimensions = [dimensions]
        self._ensure_dag_current()
        all_needed = self._measures_needed([measure_name])
        if self._con and self._any_sql(all_needed):
            return self._breakdown_duckdb(
                measure_name, periods, scenario, all_needed,
                dimensions, dimension_values, **filters
            )
        return self._breakdown_python(
            measure_name, periods, scenario,
            dimensions, dimension_values, **filters
        )

    def clear_cache(self) -> None:
        """Clear the memo cache.  Call after underlying data changes."""
        self._memo.clear()

    def _ensure_dag_current(self) -> None:
        current_names = frozenset(self._registry.names())
        if current_names != self._registry_names:
            self._dag = MeasureDAG(self._registry)
            self._registry_names = current_names

    # ------------------------------------------------------------------
    # Python resolution path
    # ------------------------------------------------------------------

    def _resolve_unchecked(self, measure_name: str, context: CalculationContext) -> float:
        cache_key = (measure_name, context)
        if cache_key in self._memo:
            return self._memo[cache_key]

        measure = self._registry.get(measure_name)
        if measure.formula is not None:
            value = self._resolve_formula(measure, context)
        else:
            value = self._resolve_scalar(measure, context)

        self._memo[cache_key] = value
        return value

    def _resolve_scalar(self, measure: Measure, context: CalculationContext) -> float:
        """Resolve a SQL or resolver-backed measure to a scalar float."""
        if measure.resolver is not None:
            try:
                result = measure.resolver(context)
            except Exception as e:
                raise RuntimeError(
                    f"Resolver error in Measure '{measure.name}': {e}"
                ) from e
            return 0.0 if result is None else float(result)

        if self._con and measure.sql:
            query, params = self._build_cte_query(
                measure.name, [context.period], context.scenario,
                dimensions=None, dimension_values=None,
                filters=dict(context.filters),
            )
            result = self._con.execute(query, params).fetchone()[0]
            return 0.0 if result is None else float(result)

        raise RuntimeError(
            f"Measure '{measure.name}' has no resolver. "
            "Provide a resolver for the Python path, or use a "
            "Calculator with a DuckDB connection."
        )

    def _resolve_formula(self, measure: Measure, context: CalculationContext) -> float:
        dep_values = {dep: self._resolve_unchecked(dep, context) for dep in measure.dependencies}
        return measure.formula(MeasureValues(dep_values, self, context))

    def _breakdown_python(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        dimensions: List[str],
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
            dim_filter = self._make_dim_filter(dimensions, value)
            for period in periods:
                ctx = CalculationContext.make(
                    period=period,
                    scenario=scenario,
                    **dim_filter,
                    **filters,
                )
                row[period.label] = self._resolve_unchecked(measure_name, ctx)
            data[value] = row

        df = pd.DataFrame(data).T
        if len(dimensions) > 1:
            df.index = pd.MultiIndex.from_tuples(df.index, names=dimensions)
        return df

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
        sql_names = self._sql_names_to_fetch(all_needed)

        raw = self._sql_fetch(
            sql_names, periods, scenario,
            dimensions=None, dimension_values=None, **filters
        )
        self._fill_resolver_columns(raw, all_needed, periods, scenario, None, None, **filters)
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
        dimensions: List[str],
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> pd.DataFrame:
        sql_names = self._sql_names_to_fetch(all_needed)

        raw = self._sql_fetch(
            sql_names, periods, scenario,
            dimensions=dimensions, dimension_values=dimension_values, **filters
        )
        self._fill_resolver_columns(
            raw, all_needed, periods, scenario, dimensions, dimension_values, **filters
        )
        raw = self._add_derived_columns(raw, all_needed, periods, scenario, filters, dimensions)

        target_cols = [_col_key(period, measure_name) for period in periods]
        result = raw[target_cols].copy()
        result.columns = [period.label for period in periods]

        if dimension_values is not None:
            if len(dimensions) == 1:
                result = result.reindex(dimension_values).fillna(0.0)
            else:
                idx = pd.MultiIndex.from_tuples(dimension_values, names=dimensions)
                result = result.reindex(idx).fillna(0.0)
        else:
            if len(dimensions) == 1:
                result.index.name = None
            # MultiIndex already carries names from set_index

        return result

    def _sql_fetch(
        self,
        sql_measure_names: List[str],
        periods: List[Period],
        scenario: str,
        dimensions: Optional[List[str]],
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> pd.DataFrame:
        """
        Execute one CTE-chained query per SQL measure and return a combined DataFrame.

        Each query builds a WITH chain of all SQL ancestors, then aggregates the
        terminal measure with FILTER (WHERE date BETWEEN ...) per period.

        Columns are named "{period.label}|{measure_name}".
        With dimensions:    indexed by dimension value(s); MultiIndex when >1 dimension.
        Without dimensions: single-row DataFrame.
        """
        if not sql_measure_names:
            return pd.DataFrame()

        frames: List[pd.DataFrame] = []

        for name in sql_measure_names:
            query, params = self._build_cte_query(
                name, periods, scenario,
                dimensions=dimensions, dimension_values=dimension_values,
                filters=filters,
            )
            df = self._con.execute(query, params).df()

            if dimensions:
                if len(dimensions) == 1:
                    df = df.set_index(dimensions[0])
                else:
                    df = df.set_index(dimensions)

            df.columns = [f"{col}|{name}" for col in df.columns]
            frames.append(df)

        return pd.concat(frames, axis=1).fillna(0.0)

    def _build_cte_query(
        self,
        measure_name: str,
        periods: List[Period],
        scenario: str,
        dimensions: Optional[List[str]],
        dimension_values: Optional[List[Any]],
        filters: dict,
    ) -> Tuple[str, List[Any]]:
        """
        Build a CTE-chained SQL query for a single terminal SQL measure.

        Walks up the SQL ancestor chain (measure.X references) to build a
        WITH clause, then selects FILTER aggregations for all periods from
        the terminal CTE.
        """
        sql_ancestors = self._sql_ancestors_ordered(measure_name)
        value_col, date_col, agg_type = self._resolve_metadata(measure_name)

        # Build CTE parts — replace measure.X refs with "X" identifiers
        cte_parts = []
        for name in sql_ancestors:
            m = self._registry.get(name)
            sql_body = m.sql.rstrip().rstrip(";")
            sql_body = _MEASURE_REF_RE.sub(lambda match: f'"{match.group(1)}"', sql_body)
            cte_parts.append(f'"{name}" AS (\n    {sql_body}\n)')

        with_clause = "WITH " + ",\n".join(cte_parts)

        # SELECT: period aggregations + optional dimension columns
        select_parts = [
            f'{self._agg_expr(agg_type, value_col, date_col, p)} AS "{p.label}"'
            for p in periods
        ]
        if dimensions:
            select_parts = [f'"{d}"' for d in dimensions] + select_parts

        # WHERE: scenario (optional) + fixed filters + optional dimension IN
        where_parts = []
        params: List[Any] = []

        # If scenario= is set on the measure, the SQL already scopes the data —
        # no scenario WHERE clause needed. Otherwise filter by scenario_col.
        m = self._registry.get(measure_name)
        if m.scenario is None:
            effective_scenario_col = m.scenario_col if m.scenario_col else self._scenario_col
            where_parts.append(f'"{effective_scenario_col}" = ?')
            params.append(scenario)

        for k, v in filters.items():
            fragment, vals = _filter_clause(k, v)
            where_parts.append(fragment)
            params.extend(vals)

        if dimensions and dimension_values is not None:
            if len(dimensions) == 1:
                placeholders = ", ".join("?" * len(dimension_values))
                where_parts.append(f'"{dimensions[0]}" IN ({placeholders})')
                params.extend(dimension_values)
            else:
                dim_cols = ", ".join(f'"{d}"' for d in dimensions)
                row_placeholders = ", ".join(
                    f"({', '.join('?' * len(dimensions))})" for _ in dimension_values
                )
                where_parts.append(f'({dim_cols}) IN ({row_placeholders})')
                for vals in dimension_values:
                    params.extend(vals)

        select_clause = ", ".join(select_parts)

        query = (
            f"{with_clause}\n"
            f'SELECT {select_clause}\n'
            f'FROM "{measure_name}"'
        )
        if where_parts:
            query += f'\nWHERE {" AND ".join(where_parts)}'
        if dimensions:
            group_cols = ", ".join(f'"{d}"' for d in dimensions)
            query += f'\nGROUP BY {group_cols}'

        return query, params

    def _sql_ancestors_ordered(self, measure_name: str) -> List[str]:
        """
        Return the SQL ancestor chain of measure_name plus itself, in
        evaluation order (leaf first, terminal last).
        """
        all_deps = self._dag.all_dependencies_of(measure_name)
        sql_ancestors = [n for n in all_deps if bool(self._registry.get(n).sql)]
        if bool(self._registry.get(measure_name).sql):
            return sql_ancestors + [measure_name]
        return sql_ancestors

    def _resolve_metadata(self, measure_name: str) -> Tuple[str, str, AggType]:
        """
        Resolve value_col, date_col, and agg_type for a SQL measure.

        For composed measures these are inherited from the nearest SQL ancestor
        that defines them; the measure can override any individual field.
        """
        m = self._registry.get(measure_name)

        # Walk SQL dependencies to find ancestor metadata
        ancestor_value_col, ancestor_date_col, ancestor_agg_type = "", self._date_col, AggType.SUM
        for dep_name in _sql_measure_refs(m.sql or ""):
            try:
                ancestor_value_col, ancestor_date_col, ancestor_agg_type = \
                    self._resolve_metadata(dep_name)
                break
            except (ValueError, KeyError):
                continue

        value_col = m.value_col or ancestor_value_col
        date_col  = m.date_col or ancestor_date_col
        agg_type  = m.agg_type if m.agg_type is not None else ancestor_agg_type

        if not value_col:
            raise ValueError(
                f"Cannot resolve value_col for measure '{measure_name}'. "
                "Set value_col on the measure or one of its SQL ancestors."
            )

        return value_col, date_col, agg_type

    @staticmethod
    def _agg_expr(
        agg_type: AggType,
        value_col: str,
        date_col: str,
        period: Period,
    ) -> str:
        """Return the SQL aggregation expression for one measure × one period."""
        start = period.start
        end   = period.end
        date_filter = f'"{date_col}" BETWEEN \'{start}\' AND \'{end}\''

        if agg_type == AggType.LAST_DAY:
            return f'COALESCE(arg_max("{value_col}", "{date_col}") FILTER (WHERE {date_filter}), 0.0)'
        elif agg_type == AggType.AVERAGE:
            return f'COALESCE(AVG("{value_col}") FILTER (WHERE {date_filter}), 0.0)'
        elif agg_type == AggType.CUMULATIVE_END:
            return f'COALESCE(SUM("{value_col}") FILTER (WHERE "{date_col}" <= \'{end}\'), 0.0)'
        elif agg_type == AggType.CUMULATIVE_START:
            return f'COALESCE(SUM("{value_col}") FILTER (WHERE "{date_col}" < \'{start}\'), 0.0)'
        else:
            return f'COALESCE(SUM("{value_col}") FILTER (WHERE {date_filter}), 0.0)'

    def _sql_names_to_fetch(self, all_needed: List[str]) -> List[str]:
        """
        Return the SQL measures that need a direct query.

        A SQL measure that is only referenced via measure.X by another SQL
        measure in the set is excluded — it will appear in that measure's CTE
        chain.  A SQL measure is kept if it is needed directly by a Python
        formula or is not a pure SQL dependency.
        """
        sql_needed = {n for n in all_needed if bool(self._registry.get(n).sql)}

        # SQL measures that are pure CTE dependencies of another SQL measure
        pure_sql_deps: set = set()
        for n in sql_needed:
            m = self._registry.get(n)
            for ref in _sql_measure_refs(m.sql or ""):
                if ref in sql_needed:
                    pure_sql_deps.add(ref)

        # SQL measures needed directly by a Python formula
        python_formula_needs: set = set()
        for n in all_needed:
            m = self._registry.get(n)
            if m.formula is not None:
                python_formula_needs.update(m.dependencies or [])

        order = self._dag.evaluation_order()
        return [
            n for n in order
            if n in sql_needed
            and (n not in pure_sql_deps or n in python_formula_needs)
        ]

    def _fill_resolver_columns(
        self,
        df: pd.DataFrame,
        all_needed: List[str],
        periods: List[Period],
        scenario: str,
        dimensions: Optional[List[str]],
        dimension_values: Optional[List[Any]],
        **filters: Any,
    ) -> None:
        """Fill columns for resolver-only measures (no sql) cell by cell."""
        resolver_only = [
            n for n in all_needed
            if self._registry.get(n).resolver is not None
            and not bool(self._registry.get(n).sql)
            and self._registry.get(n).formula is None
        ]
        if not resolver_only:
            return

        for period in periods:
            for name in resolver_only:
                col = _col_key(period, name)
                if col in df.columns:
                    continue

                if dimensions and dimension_values is not None:
                    # Build lookup keyed to match the DataFrame index type.
                    # Single-dim: index is string → key with str(dv).
                    # Multi-dim:  index is MultiIndex tuples → key with dv directly.
                    if len(dimensions) == 1:
                        values_map = {
                            dv: self._resolve_unchecked(
                                name,
                                CalculationContext.make(
                                    period=period, scenario=scenario,
                                    **{dimensions[0]: dv},
                                    **filters,
                                ),
                            )
                            for dv in dimension_values
                        }
                    else:
                        values_map = {
                            dv: self._resolve_unchecked(
                                name,
                                CalculationContext.make(
                                    period=period, scenario=scenario,
                                    **dict(zip(dimensions, dv)),
                                    **filters,
                                ),
                            )
                            for dv in dimension_values
                        }
                    df[col] = df.index.map(values_map).astype(float)
                elif dimensions:
                    df[col] = [
                        self._resolve_unchecked(
                            name,
                            CalculationContext.make(
                                period=period, scenario=scenario,
                                **self._make_dim_filter(dimensions, dv),
                                **filters,
                            ),
                        )
                        for dv in df.index
                    ]
                else:
                    ctx = CalculationContext.make(period=period, scenario=scenario, **filters)
                    df[col] = self._resolve_unchecked(name, ctx)

    def _add_derived_columns(
        self,
        df: pd.DataFrame,
        all_needed: List[str],
        periods: List[Period],
        scenario: str,
        filters: Optional[dict] = None,
        dimensions: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        Compute Python formula measures in DAG order and append to the DataFrame.

        Attempts vectorized pandas arithmetic first; falls back to row-wise
        .apply() for formulas with Python conditionals or time-shifted lookups.
        """
        new_cols: Dict[str, Any] = {}

        for name in all_needed:
            m = self._registry.get(name)
            if m.formula is None:
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
                    # Vectorized path failed — fall back to row-wise apply with MeasureValues.
                    # ValueError/TypeError: pandas raises these when a formula does `if series:`
                    #   (truth value of a Series is ambiguous).
                    # KeyError: time-shifted lookups like v["Revenue", -12] raise KeyError on a
                    #   plain dict; MeasureValues.__getitem__ handles tuple keys.
                    # AttributeError: some pandas versions raise this on Series truthiness checks.
                    combined = df.assign(
                        **{k: v for k, v in new_cols.items() if k not in df.columns}
                    )
                    new_cols[col] = combined.apply(
                        self._make_row_applier(m, period, scenario, filters, dimensions),
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
        dimensions: Optional[List[str]],
    ):
        """Return a row → float callable for use with DataFrame.apply."""
        def _apply(row):
            dim_filter = self._make_dim_filter(dimensions, row.name) if dimensions else {}
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

    @staticmethod
    def _make_dim_filter(dimensions: List[str], value: Any) -> dict:
        """Build a {col: val} filter dict from a dimensions list and one row's index value.

        For a single dimension value is a scalar; for multiple dimensions it is a tuple.
        """
        if len(dimensions) == 1:
            return {dimensions[0]: value}
        return dict(zip(dimensions, value))

    def _any_sql(self, all_needed: List[str]) -> bool:
        """Return True if any measure in the dependency chain has sql."""
        return any(bool(self._registry.get(n).sql) for n in all_needed)

    def _measures_needed(self, names: List[str]) -> List[str]:
        """Return names + all transitive dependencies in evaluation order."""
        needed = set(names)
        for n in names:
            needed.update(self._dag.all_dependencies_of(n))
        order = self._dag.evaluation_order()
        return [n for n in order if n in needed]
