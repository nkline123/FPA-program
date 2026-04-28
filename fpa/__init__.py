"""
FPA — Financial Planning & Analysis library.

A DuckDB-centric calculation engine for financial metrics across time periods
and scenarios.  Measures are defined as SQL queries or Python formulas; the
engine builds CTE-chained queries covering all periods simultaneously and
supports SQL composition via ``measure.<name>`` references.

    import fpa
    import duckdb

    con = duckdb.connect("warehouse.duckdb")
    calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

    registry = fpa.MeasureRegistry()
    registry.register_many([
        fpa.Measure(
            name="Expense",
            sql="SELECT * FROM gl WHERE account_id IN ('6000','6010')",
            value_col="amount",
            date_col="date",
            agg_type=fpa.AggType.SUM,
            scenario_col="scenario",
        ),
        fpa.Measure(
            name="Sales & Marketing",
            sql="SELECT * FROM measure.Expense WHERE department IN ('Sales', 'Marketing')",
            # value_col / date_col / agg_type / scenario_col inherited from Expense
        ),
        fpa.Measure(
            name="S&M %",
            dependencies=["Sales & Marketing", "Expense"],
            formula=lambda v: (v["Sales & Marketing"] / v["Expense"] * 100)
                              if v["Expense"] else 0.0,
        ),
    ])

    calc = fpa.Calculator(registry, connection=con)
    periods = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

    table = calc.build_table(
        ["Expense", "Sales & Marketing", "S&M %"],
        periods,
        scenario="Actual",
    )

    # Single dimension — plain Index
    by_dept = calc.build_breakdown_table(
        "Sales & Marketing",
        periods,
        scenario="Actual",
        dimensions="department",
    )

    # Multiple dimensions — MultiIndex
    by_dept_entity = calc.build_breakdown_table(
        "Sales & Marketing",
        periods,
        scenario="Actual",
        dimensions=["entity", "department"],
    )
"""

# Calendar
from .calendar.fiscal_calendar import FiscalCalendar
from .calendar.period import Period, Grain, AggType

# Measures
from .measures.measure import Measure, AnyMeasure
from .measures.measure_registry import MeasureRegistry

# Engine
from .engine.calculator import Calculator, CalculationContext
from .engine.measure_values import MeasureValues

__all__ = [
    # Calendar
    "FiscalCalendar",
    "Period",
    "Grain",
    "AggType",
    # Measures
    "Measure",
    "AnyMeasure",
    "MeasureRegistry",
    # Engine
    "Calculator",
    "CalculationContext",
    "MeasureValues",
]
