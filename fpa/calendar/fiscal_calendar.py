from __future__ import annotations
from datetime import date
from dateutil.relativedelta import relativedelta
from typing import List

from .period import Grain, Period


class FiscalCalendar:
    """
    Converts dates to fiscal Periods and navigates between them.

    All fiscal logic flows through this class. Configure once at startup.

    Args:
        fiscal_year_start_month: Month number (1-12) where fiscal year begins.
                                 E.g. 1 = January (calendar year), 7 = July.
        year_label_convention:  "ending"  → FY label = year the fiscal year ends in.
                                "starting" → FY label = year the fiscal year starts in.

    Example (fiscal year starts July):
        fiscal_year_start_month=7, year_label_convention="ending"
        → Jul 2024 – Jun 2025 is labelled FY2025
    """

    def __init__(
        self,
        fiscal_year_start_month: int = 1,
        year_label_convention: str = "ending",
    ):
        if not 1 <= fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be 1–12")
        if year_label_convention not in ("ending", "starting"):
            raise ValueError("year_label_convention must be 'ending' or 'starting'")

        self.fiscal_year_start_month = fiscal_year_start_month
        self.year_label_convention = year_label_convention

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fiscal_year_for_date(self, d: date) -> int:
        """Return the fiscal year integer that contains the given date."""
        if self.fiscal_year_start_month == 1:
            return d.year
        if d.month >= self.fiscal_year_start_month:
            calendar_year = d.year
        else:
            calendar_year = d.year - 1
        if self.year_label_convention == "ending":
            # fiscal year ends in the next calendar year
            return calendar_year + 1
        else:
            return calendar_year

    def _fiscal_period_num_for_date(self, d: date) -> int:
        """Return fiscal month number (1-12) for the given date."""
        offset = (d.month - self.fiscal_year_start_month) % 12
        return offset + 1

    def _month_start(self, d: date) -> date:
        return d.replace(day=1)

    def _month_end(self, d: date) -> date:
        next_month = d.replace(day=1) + relativedelta(months=1)
        return next_month - relativedelta(days=1)

    def _fiscal_year_start_date(self, fiscal_year: int) -> date:
        """Return the first date of the given fiscal year."""
        if self.year_label_convention == "ending":
            calendar_year = fiscal_year - 1 if self.fiscal_year_start_month != 1 else fiscal_year
        else:
            calendar_year = fiscal_year
        return date(calendar_year, self.fiscal_year_start_month, 1)

    # ------------------------------------------------------------------
    # Core conversion: date → Period
    # ------------------------------------------------------------------

    def month_period(self, d: date) -> Period:
        """Return the monthly Period containing the given date."""
        start = self._month_start(d)
        end = self._month_end(d)
        fy = self._fiscal_year_for_date(d)
        fp = self._fiscal_period_num_for_date(d)
        label = f"{start.strftime('%b %Y')}"
        return Period(
            grain=Grain.MONTH,
            start=start,
            end=end,
            fiscal_year=fy,
            fiscal_period_num=fp,
            label=label,
        )

    def quarter_period(self, d: date) -> Period:
        """Return the quarterly Period containing the given date."""
        fp_month = self._fiscal_period_num_for_date(d)
        quarter_num = (fp_month - 1) // 3 + 1
        # First month of this fiscal quarter
        fp_quarter_start_month_num = (quarter_num - 1) * 3 + 1
        # Walk back to find the start date of the quarter
        month_offset = (fp_month - fp_quarter_start_month_num)
        quarter_start_date = self._month_start(d) - relativedelta(months=month_offset)
        quarter_end_date = self._month_end(quarter_start_date + relativedelta(months=2))
        fy = self._fiscal_year_for_date(d)
        label = f"FY{fy} Q{quarter_num}"
        return Period(
            grain=Grain.QUARTER,
            start=quarter_start_date,
            end=quarter_end_date,
            fiscal_year=fy,
            fiscal_period_num=quarter_num,
            label=label,
        )

    def year_period(self, d: date) -> Period:
        """Return the annual Period containing the given date."""
        fy = self._fiscal_year_for_date(d)
        start = self._fiscal_year_start_date(fy)
        end = self._month_end(start + relativedelta(months=11))
        label = f"FY{fy}"
        return Period(
            grain=Grain.YEAR,
            start=start,
            end=end,
            fiscal_year=fy,
            fiscal_period_num=1,
            label=label,
        )

    def period_for(self, d: date, grain: Grain) -> Period:
        """Dispatch to the right period type by grain."""
        if grain == Grain.MONTH:
            return self.month_period(d)
        elif grain == Grain.QUARTER:
            return self.quarter_period(d)
        elif grain == Grain.YEAR:
            return self.year_period(d)
        raise ValueError(f"Unsupported grain: {grain}")

    # ------------------------------------------------------------------
    # Period ranges
    # ------------------------------------------------------------------

    def month_range(self, start: date, end: date) -> List[Period]:
        """Return all monthly Periods from start month through end month."""
        periods = []
        current = self._month_start(start)
        end_start = self._month_start(end)
        while current <= end_start:
            periods.append(self.month_period(current))
            current += relativedelta(months=1)
        return periods

    def quarter_range(self, start: date, end: date) -> List[Period]:
        """Return all quarterly Periods between start and end dates."""
        seen = set()
        periods = []
        current = self._month_start(start)
        end_start = self._month_start(end)
        while current <= end_start:
            p = self.quarter_period(current)
            if p not in seen:
                seen.add(p)
                periods.append(p)
            current += relativedelta(months=1)
        return periods

    def periods_for_fiscal_year(self, fiscal_year: int, grain: Grain) -> List[Period]:
        """Return all periods of the given grain within a fiscal year."""
        fy_start = self._fiscal_year_start_date(fiscal_year)
        fy_end = self._month_end(fy_start + relativedelta(months=11))
        if grain == Grain.MONTH:
            return self.month_range(fy_start, fy_end)
        elif grain == Grain.QUARTER:
            return self.quarter_range(fy_start, fy_end)
        elif grain == Grain.YEAR:
            return [self.year_period(fy_start)]
        raise ValueError(f"Unsupported grain: {grain}")

    # ------------------------------------------------------------------
    # Comparisons & navigation
    # ------------------------------------------------------------------

    def prior_year_period(self, period: Period) -> Period:
        """Return the same period one fiscal year earlier."""
        prior_start = period.start - relativedelta(years=1)
        return self.period_for(prior_start, period.grain)

    def prior_period(self, period: Period) -> Period:
        """Return the immediately preceding period of the same grain."""
        if period.grain == Grain.MONTH:
            prior_start = period.start - relativedelta(months=1)
        elif period.grain == Grain.QUARTER:
            prior_start = period.start - relativedelta(months=3)
        elif period.grain == Grain.YEAR:
            prior_start = period.start - relativedelta(years=1)
        else:
            raise ValueError(f"Unsupported grain: {period.grain}")
        return self.period_for(prior_start, period.grain)

    def ytd_periods(self, as_of_period: Period) -> List[Period]:
        """
        Return all monthly periods from the start of the fiscal year
        through and including as_of_period.
        """
        if as_of_period.grain != Grain.MONTH:
            raise ValueError("ytd_periods requires a monthly as_of_period")
        fy_start = self._fiscal_year_start_date(as_of_period.fiscal_year)
        return self.month_range(fy_start, as_of_period.start)

    def rolling_periods(self, as_of_period: Period, n: int) -> List[Period]:
        """
        Return the n most recent monthly periods ending at as_of_period (inclusive).
        E.g. rolling_periods(Aug_2025, 12) → Sep 2024 – Aug 2025
        """
        if as_of_period.grain != Grain.MONTH:
            raise ValueError("rolling_periods requires a monthly as_of_period")
        start = as_of_period.start - relativedelta(months=n - 1)
        return self.month_range(start, as_of_period.start)
