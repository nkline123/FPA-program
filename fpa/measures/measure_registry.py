from __future__ import annotations
from typing import Dict, List
from .measure import AnyMeasure, BaseMeasure, Measure


class MeasureRegistry:
    """
    Central registry of all measure definitions.

    Measures are registered in any order. The DAG module handles
    resolution order. Duplicate names raise an error.
    """

    def __init__(self):
        self._measures: Dict[str, AnyMeasure] = {}

    def register(self, measure: AnyMeasure) -> None:
        if measure.name in self._measures:
            raise ValueError(f"Measure '{measure.name}' is already registered.")
        self._measures[measure.name] = measure

    def register_many(self, measures: List[AnyMeasure]) -> None:
        for m in measures:
            self.register(m)

    def get(self, name: str) -> AnyMeasure:
        if name not in self._measures:
            raise KeyError(f"Measure '{name}' is not registered. Available: {self.names()}")
        return self._measures[name]

    def names(self) -> List[str]:
        return list(self._measures.keys())

    def base_measures(self) -> List[BaseMeasure]:
        return [m for m in self._measures.values() if isinstance(m, BaseMeasure)]

    def derived_measures(self) -> List[Measure]:
        return [m for m in self._measures.values() if isinstance(m, Measure)]

    def by_tag(self, tag: str) -> List[AnyMeasure]:
        return [m for m in self._measures.values() if tag in m.tags]

    def __contains__(self, name: str) -> bool:
        return name in self._measures

    def __len__(self) -> int:
        return len(self._measures)

    def __repr__(self) -> str:
        return f"MeasureRegistry({self.names()})"
