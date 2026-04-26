from __future__ import annotations
from typing import List
import networkx as nx

from .measure import _sql_measure_refs
from .measure_registry import MeasureRegistry


class MeasureDAG:
    """
    Builds and validates the dependency graph for all registered measures.

    Edges are derived from two sources:
    - Python formula measures: explicit ``dependencies`` list.
    - SQL composed measures: ``measure.<name>`` references in the sql string.

    Usage:
        dag = MeasureDAG(registry)
        order = dag.evaluation_order()   # topologically sorted list of names
    """

    def __init__(self, registry: MeasureRegistry):
        self._registry = registry
        self._graph = nx.DiGraph()
        self._build()

    def _build(self) -> None:
        for name in self._registry.names():
            self._graph.add_node(name)

        for measure in self._registry.all_measures():
            # Python formula dependencies
            for dep_name in (measure.dependencies or []):
                if dep_name not in self._registry:
                    raise ValueError(
                        f"Measure '{measure.name}' depends on '{dep_name}', "
                        "which is not registered."
                    )
                self._graph.add_edge(dep_name, measure.name)

            # SQL measure.X composition dependencies
            for dep_name in _sql_measure_refs(measure.sql or ""):
                if dep_name not in self._registry:
                    raise ValueError(
                        f"Measure '{measure.name}' references measure.{dep_name} in sql, "
                        "which is not registered."
                    )
                self._graph.add_edge(dep_name, measure.name)

        if not nx.is_directed_acyclic_graph(self._graph):
            cycles = list(nx.simple_cycles(self._graph))
            raise ValueError(
                f"Circular dependency detected in measures: {cycles}\n"
                "Measures cannot depend on each other in a cycle."
            )

        self._order: List[str] = list(nx.topological_sort(self._graph))

    def evaluation_order(self) -> List[str]:
        """Return measure names ordered so dependencies always precede dependents."""
        return self._order

    def dependencies_of(self, measure_name: str) -> List[str]:
        """Return all measures that ``measure_name`` directly depends on."""
        return list(self._graph.predecessors(measure_name))

    def dependents_of(self, measure_name: str) -> List[str]:
        """Return all measures that directly depend on ``measure_name``."""
        return list(self._graph.successors(measure_name))

    def all_dependencies_of(self, measure_name: str) -> List[str]:
        """
        Return all transitive dependencies of ``measure_name`` in evaluation
        order (everything that must be resolved before it).
        """
        ancestors = nx.ancestors(self._graph, measure_name)
        subgraph = self._graph.subgraph(ancestors | {measure_name})
        return [n for n in nx.topological_sort(subgraph) if n != measure_name]

    def __repr__(self) -> str:
        return (
            f"MeasureDAG("
            f"nodes={self._graph.number_of_nodes()}, "
            f"edges={self._graph.number_of_edges()})"
        )
