"""
FPA — Financial Planning & Analysis library.

A calculation engine for financial metrics across time periods and scenarios.
Data access is handled by the caller via resolver callables on BaseMeasure.

    import fpa

    calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)
    registry = fpa.MeasureRegistry()
    registry.register_many([
        fpa.BaseMeasure(
            name="Revenue",
            resolver=lambda ctx: my_db.get_revenue(ctx.period.start, ctx.period.end, ctx.scenario),
        ),
        fpa.Measure(
            name="Gross Profit",
            dependencies=["Revenue", "COGS"],
            formula=lambda v: v["Revenue"] - v["COGS"],
        ),
    ])
    calc = fpa.Calculator(registry)
    periods = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)
    table = calc.build_table(["Revenue", "Gross Profit"], periods, scenario="Actual")
"""

# Calendar
from .calendar.fiscal_calendar import FiscalCalendar
from .calendar.period import Period, Grain, AggType

# Measures
from .measures.measure import BaseMeasure, Measure, AnyMeasure
from .measures.measure_registry import MeasureRegistry

# Engine
from .engine.calculator import Calculator, CalculationContext

__all__ = [
    # Calendar
    "FiscalCalendar",
    "Period",
    "Grain",
    "AggType",
    # Measures
    "BaseMeasure",
    "Measure",
    "AnyMeasure",
    "MeasureRegistry",
    # Engine
    "Calculator",
    "CalculationContext",
]
