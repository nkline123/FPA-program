from __future__ import annotations
from typing import List
import networkx as nx

from .measure import AnyMeasure, Measure
from .measure_registry import MeasureRegistry


class MeasureDAG:
    """
    Builds and validates the dependency graph for all registered measures.

    Usage:
        dag = MeasureDAG(registry)
        order = dag.evaluation_order()   # topologically sorted list of measure names
    """

    def __init__(self, registry: MeasureRegistry):
        self._registry = registry
        self._graph = nx.DiGraph()
        self._build()

    def _build(self) -> None:
        # Add every measure as a node
        for name in self._registry.names():
            self._graph.add_node(name)

        # Add edges: dependency → measure
        for measure in self._registry.measures():
            for dep_name in measure.dependencies:
                if dep_name not in self._registry:
                    raise ValueError(
                        f"Measure '{measure.name}' depends on '{dep_name}', "
                        f"which is not registered."
                    )
                self._graph.add_edge(dep_name, measure.name)

        # Detect cycles — raises immediately so the user sees a clear error
        if not nx.is_directed_acyclic_graph(self._graph):
            cycles = list(nx.simple_cycles(self._graph))
            raise ValueError(
                f"Circular dependency detected in measures: {cycles}\n"
                "Measures cannot depend on each other in a cycle."
            )

    def evaluation_order(self) -> List[str]:
        """
        Return measure names in the order they must be calculated.
        Dependencies always come before the measures that use them.
        """
        return list(nx.topological_sort(self._graph))

    def dependencies_of(self, measure_name: str) -> List[str]:
        """Return all measures that `measure_name` directly depends on."""
        return list(self._graph.predecessors(measure_name))

    def dependents_of(self, measure_name: str) -> List[str]:
        """Return all measures that directly depend on `measure_name`."""
        return list(self._graph.successors(measure_name))

    def all_dependencies_of(self, measure_name: str) -> List[str]:
        """
        Return all transitive dependencies of `measure_name`
        (i.e. everything that must be computed before it), in evaluation order.
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
