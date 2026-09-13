"""Validation and deterministic ordering for ModelBake build graphs."""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterable

from .types import NodeSpec


class GraphError(ValueError):
    """Base class for invalid build graph errors."""


class DuplicateNodeError(GraphError):
    pass


class UnknownDependencyError(GraphError):
    pass


class CycleError(GraphError):
    pass


_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _node_map(nodes: Iterable[NodeSpec]) -> dict[str, NodeSpec]:
    indexed: dict[str, NodeSpec] = {}
    for node in nodes:
        if not _NODE_ID_RE.fullmatch(node.id) or node.id in {".", ".."}:
            raise GraphError(f"node id is not path-safe: {node.id!r}")
        if node.id in indexed:
            raise DuplicateNodeError(f"duplicate node id: {node.id}")
        if len(set(node.dependencies)) != len(node.dependencies):
            raise GraphError(f"node {node.id!r} declares a dependency more than once")
        indexed[node.id] = node
    for node in indexed.values():
        for dependency in node.dependencies:
            if dependency not in indexed:
                raise UnknownDependencyError(
                    f"node {node.id!r} depends on unknown node {dependency!r}"
                )
    return indexed


def topological_sort(nodes: Iterable[NodeSpec]) -> tuple[NodeSpec, ...]:
    """Validate a DAG and return a deterministic dependency-first ordering."""

    indexed = _node_map(nodes)
    indegree = {node_id: 0 for node_id in indexed}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in indexed}
    for node in indexed.values():
        indegree[node.id] = len(node.dependencies)
        for dependency in node.dependencies:
            dependents[dependency].append(node.id)

    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[NodeSpec] = []
    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(indexed[node_id])
        for dependent in sorted(dependents[node_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(ordered) != len(indexed):
        cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise CycleError(f"build graph contains a cycle involving: {', '.join(cyclic)}")
    return tuple(ordered)


def validate_dag(nodes: Iterable[NodeSpec]) -> None:
    topological_sort(nodes)
