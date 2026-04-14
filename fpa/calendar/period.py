from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from datetime import date


class Grain(Enum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class AggType(Enum):
    SUM = "sum"           # Flows: Revenue, Expenses
    LAST_DAY = "last_day" # Stocks: Headcount, Cash Balance
    AVERAGE = "average"   # Rates: Average Price
    CALCULATED = "calc"   # Ratios: Gross Margin % — must recalculate, never aggregate


@dataclass(frozen=True)
class Period:
    """
    A single time period with fiscal and calendar context.
    Frozen so it can be used as a dict key / in sets.
    """
    grain: Grain
    start: date         # First day of the period (inclusive)
    end: date           # Last day of the period (inclusive)
    fiscal_year: int    # e.g. 2025
    fiscal_period_num: int  # 1-12 for months, 1-4 for quarters, 1 for year
    label: str          # Human-readable: "Jan 2025", "FY2025 Q2", "FY2025"

    @property
    def calendar_year(self) -> int:
        return self.start.year

    @property
    def calendar_month(self) -> int:
        return self.start.month

    def __str__(self) -> str:
        return self.label

    def __repr__(self) -> str:
        return f"Period({self.label})"
