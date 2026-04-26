from __future__ import annotations
from typing import Dict, List
from .measure import AnyMeasure, Measure


class MeasureRegistry:
    """
    Central registry of all measure definitions.

    Measures are registered in any order; the DAG handles resolution order.
    Duplicate names raise an error.
    """

    def __init__(self):
        self._measures: Dict[str, Measure] = {}

    def register(self, measure: Measure) -> None:
        if measure.name in self._measures:
            raise ValueError(f"Measure '{measure.name}' is already registered.")
        self._measures[measure.name] = measure

    def register_many(self, measures: List[Measure]) -> None:
        for m in measures:
            self.register(m)

    def get(self, name: str) -> Measure:
        if name not in self._measures:
            raise KeyError(f"Measure '{name}' is not registered. Available: {self.names()}")
        return self._measures[name]

    def names(self) -> List[str]:
        return list(self._measures.keys())

    def all_measures(self) -> List[Measure]:
        return list(self._measures.values())

    def sql_measures(self) -> List[Measure]:
        """Return all measures backed by SQL (leaf or composed)."""
        return [m for m in self._measures.values() if bool(m.sql)]

    def formula_measures(self) -> List[Measure]:
        """Return all Python formula-derived measures."""
        return [m for m in self._measures.values() if m.formula is not None]

    def by_tag(self, tag: str) -> List[Measure]:
        return [m for m in self._measures.values() if tag in m.tags]

    def __contains__(self, name: str) -> bool:
        return name in self._measures

    def __len__(self) -> int:
        return len(self._measures)

    def __repr__(self) -> str:
        return f"MeasureRegistry({self.names()})"
