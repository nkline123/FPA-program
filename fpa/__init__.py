"""
FPA — Financial Planning & Analysis library.

A DuckDB-centric calculation engine for financial metrics across time periods
and scenarios.  Base measures are defined as SQL filter queries; the engine
wraps them as subqueries, appends date / scenario / dimension filters
automatically, and executes one query per base measure per call — enabling
high-cardinality GROUP BY breakdowns without parameter-list explosion.

    import fpa
    import duckdb

    con = duckdb.connect("warehouse.duckdb")
    calendar = fpa.FiscalCalendar(fiscal_year_start_month=1)

    registry = fpa.MeasureRegistry()
    registry.register_many([
        fpa.BaseMeasure(
            name="Revenue",
            sql="SELECT * FROM general_ledger WHERE account_type = 'Income'",
            value_col="amount",
            date_col="period_enddate",
            agg_type=fpa.AggType.SUM,
        ),
        fpa.BaseMeasure(
            name="COGS",
            sql="SELECT * FROM general_ledger WHERE account_type = 'COGS'",
            value_col="amount",
            date_col="period_enddate",
            agg_type=fpa.AggType.SUM,
        ),
        fpa.Measure(
            name="Gross Profit",
            dependencies=["Revenue", "COGS"],
            formula=lambda v: v["Revenue"] - v["COGS"],
        ),
        fpa.Measure(
            name="Gross Margin %",
            dependencies=["Gross Profit", "Revenue"],
            formula=lambda v: (v["Gross Profit"] / v["Revenue"] * 100)
                              if v["Revenue"] else 0.0,
        ),
    ])

    calc = fpa.Calculator(registry, connection=con)
    periods = calendar.periods_for_fiscal_year(2024, fpa.Grain.MONTH)

    # P&L table — measures as rows, months as columns
    table = calc.build_table(
        ["Revenue", "COGS", "Gross Profit", "Gross Margin %"],
        periods,
        scenario="Actual",
    )

    # Dimension breakdown — one query per base measure, any number of groups
    by_dept = calc.build_breakdown_table(
        "Gross Margin %",
        periods,
        scenario="Actual",
        dimension="department",   # dimension_values optional — returns all groups
    )
"""

# Calendar
from .calendar.fiscal_calendar import FiscalCalendar
from .calendar.period import Period, Grain, AggType

# Measures
from .measures.measure import BaseMeasure, Measure, AnyMeasure
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
    "BaseMeasure",
    "Measure",
    "AnyMeasure",
    "MeasureRegistry",
    # Engine
    "Calculator",
    "CalculationContext",
    "MeasureValues",
]
