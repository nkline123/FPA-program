from __future__ import annotations
import dataclasses
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .calculator import Calculator, CalculationContext


class MeasureValues:
    """
    Dict-like container passed to Measure formulas.

    Supports three indexing forms:

        v["Revenue"]                   Current period value (from dep_values).
        v["Revenue", -12]              Revenue 12 months prior, same grain.
        v["Revenue", 0, Grain.MONTH]   Revenue for start-of-period month,
                                       overriding the grain.

    The offset is always in months, applied to period.start.  The optional
    third element overrides the result grain, which lets formulas on quarterly
    or annual periods fetch individual monthly values.

    Example formulas:
        # YoY growth (monthly or quarterly)
        lambda v: (v["Revenue"] / v["Revenue", -12] - 1) * 100

        # QoQ growth (quarterly periods)
        lambda v: (v["Revenue"] / v["Revenue", -3] - 1) * 100

        # From a quarterly period, inspect each constituent month
        lambda v: (v["Revenue", 0, Grain.MONTH]   # month 1 of this quarter
                 + v["Revenue", 1, Grain.MONTH]   # month 2
                 + v["Revenue", 2, Grain.MONTH])  # month 3
    """

    __slots__ = ("_v", "_calc", "_ctx")

    def __init__(self, dep_values: dict, calc: "Calculator", context: "CalculationContext"):
        self._v = dep_values
        self._calc = calc
        self._ctx = context

    def __getitem__(self, key: Any):
        if isinstance(key, tuple):
            if len(key) == 2:
                name, offset_months = key
                grain = None
            elif len(key) == 3:
                name, offset_months, grain = key
            else:
                raise ValueError(
                    f"Expected v[name], v[name, offset], or v[name, offset, Grain]; got {key!r}"
                )
            if self._calc._calendar is None:
                raise RuntimeError(
                    f"Time-shifted lookup v[\"{name}\", ...] requires a FiscalCalendar. "
                    "Pass calendar=your_calendar to Calculator(...)."
                )
            shifted_period = self._calc._calendar.shift(self._ctx.period, offset_months, grain)
            shifted_ctx = dataclasses.replace(self._ctx, period=shifted_period)
            return self._calc._resolve_unchecked(name, shifted_ctx)
        return self._v[key]

    @property
    def period(self):
        """The period being resolved — useful for grain-aware formulas."""
        return self._ctx.period

    @property
    def scenario(self) -> str:
        return self._ctx.scenario

    def __repr__(self) -> str:
        return f"MeasureValues({self._v})"
