from datetime import date
import pytest
from fpa import Period, Grain, AggType


def make_period(label="Jan 2025"):
    return Period(
        grain=Grain.MONTH,
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
        fiscal_year=2025,
        fiscal_period_num=1,
        label=label,
    )


def test_period_is_hashable():
    p = make_period()
    assert {p: 1}[p] == 1


def test_period_usable_in_set():
    p1 = make_period()
    p2 = make_period()
    assert len({p1, p2}) == 1


def test_period_equality():
    p1 = make_period()
    p2 = make_period()
    assert p1 == p2


def test_period_inequality_different_start():
    p1 = Period(Grain.MONTH, date(2025, 1, 1), date(2025, 1, 31), 2025, 1, "Jan 2025")
    p2 = Period(Grain.MONTH, date(2025, 2, 1), date(2025, 2, 28), 2025, 2, "Feb 2025")
    assert p1 != p2


def test_period_calendar_year():
    p = make_period()
    assert p.calendar_year == 2025


def test_period_calendar_month():
    p = make_period()
    assert p.calendar_month == 1


def test_period_str_returns_label():
    p = make_period("Jan 2025")
    assert str(p) == "Jan 2025"


def test_period_repr():
    p = make_period("Jan 2025")
    assert repr(p) == "Period(Jan 2025)"


def test_grain_values():
    assert Grain.MONTH.value == "month"
    assert Grain.QUARTER.value == "quarter"
    assert Grain.YEAR.value == "year"


def test_agg_type_values():
    assert AggType.SUM.value == "sum"
    assert AggType.LAST_DAY.value == "last_day"
    assert AggType.AVERAGE.value == "average"
    assert AggType.CALCULATED.value == "calc"
