from datetime import date
import pytest
from fpa import FiscalCalendar, Grain, Period


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cal(start_month=1, convention="ending"):
    return FiscalCalendar(fiscal_year_start_month=start_month, year_label_convention=convention)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------

def test_invalid_start_month_zero():
    with pytest.raises(ValueError):
        FiscalCalendar(fiscal_year_start_month=0)


def test_invalid_start_month_thirteen():
    with pytest.raises(ValueError):
        FiscalCalendar(fiscal_year_start_month=13)


def test_invalid_convention():
    with pytest.raises(ValueError):
        FiscalCalendar(fiscal_year_start_month=1, year_label_convention="wrong")


# ---------------------------------------------------------------------------
# Calendar-year fiscal calendar (start month = January)
# ---------------------------------------------------------------------------

class TestCalendarYear:
    def setup_method(self):
        self.c = cal(start_month=1)

    def test_month_period_label(self):
        p = self.c.month_period(date(2024, 3, 15))
        assert p.label == "Mar 2024"

    def test_month_period_start_end(self):
        p = self.c.month_period(date(2024, 3, 15))
        assert p.start == date(2024, 3, 1)
        assert p.end == date(2024, 3, 31)

    def test_month_period_fiscal_year(self):
        p = self.c.month_period(date(2024, 6, 1))
        assert p.fiscal_year == 2024

    def test_month_period_fiscal_period_num(self):
        p = self.c.month_period(date(2024, 3, 1))
        assert p.fiscal_period_num == 3

    def test_quarter_period_q1(self):
        p = self.c.quarter_period(date(2024, 2, 14))
        assert p.fiscal_period_num == 1
        assert p.start == date(2024, 1, 1)
        assert p.end == date(2024, 3, 31)
        assert p.label == "FY2024 Q1"

    def test_quarter_period_q4(self):
        p = self.c.quarter_period(date(2024, 12, 1))
        assert p.fiscal_period_num == 4
        assert p.start == date(2024, 10, 1)
        assert p.end == date(2024, 12, 31)

    def test_year_period(self):
        p = self.c.year_period(date(2024, 7, 1))
        assert p.start == date(2024, 1, 1)
        assert p.end == date(2024, 12, 31)
        assert p.label == "FY2024"

    def test_february_end_of_month(self):
        p = self.c.month_period(date(2024, 2, 1))
        assert p.end == date(2024, 2, 29)  # 2024 is a leap year

    def test_february_non_leap_year(self):
        p = self.c.month_period(date(2023, 2, 1))
        assert p.end == date(2023, 2, 28)


# ---------------------------------------------------------------------------
# Non-January fiscal year (July start, ending convention)
# ---------------------------------------------------------------------------

class TestJulyFiscalYearEnding:
    def setup_method(self):
        self.c = cal(start_month=7, convention="ending")

    def test_fiscal_year_label_july_is_fy_next(self):
        p = self.c.month_period(date(2024, 7, 1))
        assert p.fiscal_year == 2025

    def test_fiscal_year_label_june_is_fy_same(self):
        p = self.c.month_period(date(2025, 6, 1))
        assert p.fiscal_year == 2025

    def test_fiscal_period_num_july_is_1(self):
        p = self.c.month_period(date(2024, 7, 1))
        assert p.fiscal_period_num == 1

    def test_fiscal_period_num_june_is_12(self):
        p = self.c.month_period(date(2025, 6, 1))
        assert p.fiscal_period_num == 12

    def test_quarter_period_q1_july(self):
        p = self.c.quarter_period(date(2024, 8, 1))
        assert p.fiscal_period_num == 1
        assert p.start == date(2024, 7, 1)
        assert p.end == date(2024, 9, 30)

    def test_quarter_period_q4_april(self):
        p = self.c.quarter_period(date(2025, 4, 1))
        assert p.fiscal_period_num == 4
        assert p.start == date(2025, 4, 1)
        assert p.end == date(2025, 6, 30)

    def test_year_period_start_end(self):
        p = self.c.year_period(date(2024, 9, 1))
        assert p.fiscal_year == 2025
        assert p.start == date(2024, 7, 1)
        assert p.end == date(2025, 6, 30)


# ---------------------------------------------------------------------------
# Non-January fiscal year (July start, starting convention)
# ---------------------------------------------------------------------------

class TestJulyFiscalYearStarting:
    def setup_method(self):
        self.c = cal(start_month=7, convention="starting")

    def test_fiscal_year_label_july_is_fy_same(self):
        p = self.c.month_period(date(2024, 7, 1))
        assert p.fiscal_year == 2024

    def test_fiscal_year_label_june_is_fy_prior(self):
        p = self.c.month_period(date(2025, 6, 1))
        assert p.fiscal_year == 2024


# ---------------------------------------------------------------------------
# period_for dispatch
# ---------------------------------------------------------------------------

def test_period_for_month():
    c = cal()
    p = c.period_for(date(2024, 4, 1), Grain.MONTH)
    assert p.grain == Grain.MONTH


def test_period_for_quarter():
    c = cal()
    p = c.period_for(date(2024, 4, 1), Grain.QUARTER)
    assert p.grain == Grain.QUARTER


def test_period_for_year():
    c = cal()
    p = c.period_for(date(2024, 4, 1), Grain.YEAR)
    assert p.grain == Grain.YEAR


# ---------------------------------------------------------------------------
# Period ranges
# ---------------------------------------------------------------------------

def test_month_range_count():
    c = cal()
    periods = c.month_range(date(2024, 1, 1), date(2024, 12, 31))
    assert len(periods) == 12


def test_month_range_labels():
    c = cal()
    periods = c.month_range(date(2024, 1, 1), date(2024, 3, 31))
    assert [p.label for p in periods] == ["Jan 2024", "Feb 2024", "Mar 2024"]


def test_month_range_single_month():
    c = cal()
    periods = c.month_range(date(2024, 6, 15), date(2024, 6, 20))
    assert len(periods) == 1


def test_quarter_range_count():
    c = cal()
    periods = c.quarter_range(date(2024, 1, 1), date(2024, 12, 31))
    assert len(periods) == 4


def test_quarter_range_no_duplicates():
    c = cal()
    periods = c.quarter_range(date(2024, 1, 1), date(2024, 3, 31))
    assert len(periods) == 1


def test_periods_for_fiscal_year_months():
    c = cal()
    periods = c.periods_for_fiscal_year(2024, Grain.MONTH)
    assert len(periods) == 12
    assert periods[0].label == "Jan 2024"
    assert periods[-1].label == "Dec 2024"


def test_periods_for_fiscal_year_quarters():
    c = cal()
    periods = c.periods_for_fiscal_year(2024, Grain.QUARTER)
    assert len(periods) == 4


def test_periods_for_fiscal_year_year():
    c = cal()
    periods = c.periods_for_fiscal_year(2024, Grain.YEAR)
    assert len(periods) == 1
    assert periods[0].fiscal_year == 2024


def test_periods_for_fiscal_year_non_jan():
    c = cal(start_month=7, convention="ending")
    periods = c.periods_for_fiscal_year(2025, Grain.MONTH)
    assert len(periods) == 12
    assert periods[0].start == date(2024, 7, 1)
    assert periods[-1].end == date(2025, 6, 30)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def test_prior_period_month():
    c = cal()
    p = c.month_period(date(2024, 3, 1))
    prior = c.prior_period(p)
    assert prior.label == "Feb 2024"


def test_prior_period_month_crosses_year():
    c = cal()
    p = c.month_period(date(2024, 1, 1))
    prior = c.prior_period(p)
    assert prior.label == "Dec 2023"


def test_prior_period_quarter():
    c = cal()
    p = c.quarter_period(date(2024, 4, 1))
    prior = c.prior_period(p)
    assert prior.fiscal_period_num == 1


def test_prior_period_year():
    c = cal()
    p = c.year_period(date(2024, 6, 1))
    prior = c.prior_period(p)
    assert prior.fiscal_year == 2023


def test_prior_year_period():
    c = cal()
    p = c.month_period(date(2024, 6, 1))
    prior = c.prior_year_period(p)
    assert prior.label == "Jun 2023"


def test_prior_year_period_non_jan():
    c = cal(start_month=7, convention="ending")
    p = c.month_period(date(2025, 1, 1))
    prior = c.prior_year_period(p)
    assert prior.start == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# YTD and rolling
# ---------------------------------------------------------------------------

def test_ytd_periods_march():
    c = cal()
    p = c.month_period(date(2024, 3, 1))
    ytd = c.ytd_periods(p)
    assert len(ytd) == 3
    assert ytd[0].label == "Jan 2024"
    assert ytd[-1].label == "Mar 2024"


def test_ytd_periods_first_month():
    c = cal()
    p = c.month_period(date(2024, 1, 1))
    ytd = c.ytd_periods(p)
    assert len(ytd) == 1


def test_ytd_periods_non_jan_fiscal():
    c = cal(start_month=7, convention="ending")
    p = c.month_period(date(2024, 9, 1))  # FP3 of FY2025
    ytd = c.ytd_periods(p)
    assert len(ytd) == 3
    assert ytd[0].start == date(2024, 7, 1)


def test_ytd_periods_requires_monthly():
    c = cal()
    p = c.quarter_period(date(2024, 3, 1))
    with pytest.raises(ValueError):
        c.ytd_periods(p)


def test_rolling_periods_count():
    c = cal()
    p = c.month_period(date(2024, 12, 1))
    rolling = c.rolling_periods(p, 12)
    assert len(rolling) == 12
    assert rolling[0].label == "Jan 2024"
    assert rolling[-1].label == "Dec 2024"


def test_rolling_periods_crosses_year():
    c = cal()
    p = c.month_period(date(2024, 6, 1))
    rolling = c.rolling_periods(p, 6)
    assert rolling[0].label == "Jan 2024"
    assert rolling[-1].label == "Jun 2024"


def test_rolling_periods_requires_monthly():
    c = cal()
    p = c.quarter_period(date(2024, 3, 1))
    with pytest.raises(ValueError):
        c.rolling_periods(p, 4)


# ---------------------------------------------------------------------------
# shift()
# ---------------------------------------------------------------------------

def test_shift_forward_one_month():
    c = cal()
    jan = c.month_period(date(2024, 1, 1))
    result = c.shift(jan, 1)
    assert result.label == "Feb 2024"
    assert result.grain == Grain.MONTH


def test_shift_backward_one_month():
    c = cal()
    jan = c.month_period(date(2024, 1, 1))
    result = c.shift(jan, -1)
    assert result.label == "Dec 2023"


def test_shift_zero_returns_same_period():
    c = cal()
    jan = c.month_period(date(2024, 1, 1))
    result = c.shift(jan, 0)
    assert result == jan


def test_shift_backward_twelve_months():
    c = cal()
    jan_2024 = c.month_period(date(2024, 1, 1))
    result = c.shift(jan_2024, -12)
    assert result.label == "Jan 2023"


def test_shift_quarterly_period_same_grain():
    c = cal()
    q2 = c.quarter_period(date(2024, 4, 1))
    result = c.shift(q2, -3)
    assert result.label == "FY2024 Q1"
    assert result.grain == Grain.QUARTER


def test_shift_cross_grain_quarter_to_month():
    c = cal()
    q2 = c.quarter_period(date(2024, 4, 1))
    assert c.shift(q2, 0, Grain.MONTH).label == "Apr 2024"
    assert c.shift(q2, 1, Grain.MONTH).label == "May 2024"
    assert c.shift(q2, 2, Grain.MONTH).label == "Jun 2024"


def test_shift_preserves_grain_by_default():
    c = cal()
    q1 = c.quarter_period(date(2024, 1, 1))
    result = c.shift(q1, 3)
    assert result.grain == Grain.QUARTER


def test_shift_non_january_fiscal_year():
    c = cal(start_month=7, convention="ending")
    jul_2024 = c.month_period(date(2024, 7, 1))
    result = c.shift(jul_2024, -12)
    assert result.label == "Jul 2023"
    assert result.fiscal_year == 2024
