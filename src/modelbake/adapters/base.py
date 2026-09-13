"""Typed boundary between the build engine and concrete toolchain adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..types import BuildPlan, NodeSpec, NodeState


class AdapterError(RuntimeError):
    """A concrete adapter observed that it could not complete its node."""


@dataclass(frozen=True, slots=True)
class DependencyOutput:
    outputs: Mapping[str, Path]
    output_digests: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AdapterContext:
    node: NodeSpec
    plan: BuildPlan
    work_dir: Path
    output_dir: Path
    log_path: Path
    dependencies: Mapping[str, DependencyOutput]


@dataclass(frozen=True, slots=True)
class AdapterExecution:
    outputs: Mapping[str, Path]
    command: tuple[str, ...] = ()
    observations: Mapping[str, Any] = field(default_factory=dict)
    state: NodeState = NodeState.PRODUCED

    def __post_init__(self) -> None:
        if self.state not in (
            NodeState.PRODUCED,
            NodeState.FIXTURE_OBSERVED,
            NodeState.EXECUTED,
        ):
            raise ValueError(
                "adapter state must be produced, fixture_observed, or executed_on_runner"
            )


class Adapter(ABC):
    """A registered, typed executor for one node kind.

    The core engine never interprets or executes command strings. Concrete
    adapters may invoke a pinned tool using argument arrays, then return the
    observed outputs through this interface.
    """

    @abstractmethod
    def fingerprint(self) -> Mapping[str, Any]:
        """Return JSON-compatible adapter/toolchain identity for cache keys."""

    @abstractmethod
    def run(self, context: AdapterContext) -> AdapterExecution:
        """Execute the node and place all declared outputs under output_dir."""
