"""Stable value types shared by the planner, engine, adapters, and renderers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class NodeState(StrEnum):
    """Observed execution state. Names deliberately avoid compatibility claims."""

    PENDING = "pending"
    RUNNING = "running"
    PRODUCED = "produced"
    FIXTURE_OBSERVED = "fixture_observed"
    EXECUTED = "executed_on_runner"
    CACHE_HIT = "cache_hit"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class NodeSpec:
    id: str
    kind: str
    dependencies: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["dependencies"] = list(self.dependencies)
        return value


@dataclass(frozen=True, slots=True)
class BuildPlan:
    schema: str
    project: str
    recipe_path: Path
    recipe_digest: str
    runner: dict[str, Any]
    nodes: tuple[NodeSpec, ...]
    source: dict[str, Any]
    toolchain: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project": self.project,
            "recipe_path": str(self.recipe_path),
            "recipe_digest": self.recipe_digest,
            "runner": self.runner,
            "source": self.source,
            "toolchain": self.toolchain,
            "nodes": [node.to_dict() for node in self.nodes],
        }


@dataclass(slots=True)
class NodeResult:
    id: str
    kind: str
    state: NodeState
    cache_key: str
    input_digests: dict[str, str] = field(default_factory=dict)
    output_digests: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    log_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass(slots=True)
class BuildResult:
    schema: str
    run_id: str
    project: str
    status: str
    recipe_digest: str
    source_digest: str
    runner: dict[str, Any]
    started_at: str
    finished_at: str
    run_dir: Path
    nodes: list[NodeResult]
    claims: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "project": self.project,
            "status": self.status,
            "recipe_digest": self.recipe_digest,
            "source_digest": self.source_digest,
            "runner": self.runner,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_dir": str(self.run_dir),
            "nodes": [node.to_dict() for node in self.nodes],
            "claims": self.claims,
            "exclusions": self.exclusions,
        }
