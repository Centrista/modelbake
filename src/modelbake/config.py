"""Strict, safe configuration loading for ModelBake recipes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .hashing import tree_digest
from .types import BuildPlan, NodeSpec

RECIPE_SCHEMA = "modelbake/v0"
SUPPORTED_ARCHITECTURES = frozenset({"llama"})
SUPPORTED_QUANTIZATIONS = frozenset({"F16", "Q4_0", "Q8_0"})
PICKLE_SUFFIXES = frozenset({".bin", ".ckpt", ".pickle", ".pkl", ".pt", ".pth"})
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    """Raised when a recipe is unsafe, ambiguous, or outside the v0 schema."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that treats duplicate keys as configuration errors."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConfigError(f"duplicate YAML field: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True, slots=True)
class SourceConfig:
    path: Path
    architecture: str
    format: str


@dataclass(frozen=True, slots=True)
class TargetConfig:
    name: str
    quantization: str


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    prompt: str
    max_tokens: int


@dataclass(frozen=True, slots=True)
class ToolConfig:
    python: Path
    convert: Path
    quantize: Path
    runner: Path
    revision: str

    def to_dict(self) -> dict[str, str]:
        return {
            "python": str(self.python),
            "convert": str(self.convert),
            "quantize": str(self.quantize),
            "runner": str(self.runner),
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    adapter: str
    name: str
    timeout_seconds: int
    max_log_bytes: int
    tools: ToolConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "adapter": self.adapter,
            "name": self.name,
            "timeout_seconds": self.timeout_seconds,
            "max_log_bytes": self.max_log_bytes,
        }
        if self.tools is not None:
            data["tools"] = self.tools.to_dict()
        return data


@dataclass(frozen=True, slots=True)
class ModelBakeConfig:
    schema: str
    project: str
    recipe_path: Path
    source: SourceConfig
    targets: tuple[TargetConfig, ...]
    smoke: SmokeConfig
    runner: RunnerConfig
    canonical: Mapping[str, Any]


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ConfigError(f"{where} must be a mapping with string keys")
    return value


def _strict_keys(
    value: Mapping[str, Any], *, allowed: set[str], required: set[str], where: str
) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ConfigError(f"{where} contains unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ConfigError(f"{where} is missing required field(s): {', '.join(sorted(missing))}")


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _integer(value: Any, where: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{where} must be an integer from {minimum} to {maximum}")
    return value


def _contains_forbidden_key(value: Any, forbidden: str, seen: set[int] | None = None) -> bool:
    seen = seen if seen is not None else set()
    if isinstance(value, dict):
        if id(value) in seen:
            raise ConfigError("recursive YAML structures and aliases are not accepted")
        seen.add(id(value))
        return forbidden in value or any(
            _contains_forbidden_key(child, forbidden, seen) for child in value.values()
        )
    if isinstance(value, list):
        if id(value) in seen:
            raise ConfigError("recursive YAML structures and aliases are not accepted")
        seen.add(id(value))
        return any(_contains_forbidden_key(child, forbidden, seen) for child in value)
    return False


def _safe_source_path(
    raw: Any,
    recipe_dir: Path,
    roots: Sequence[Path],
    *,
    allow_absolute: bool,
) -> Path:
    text = _string(raw, "source.path")
    candidate = Path(text)
    if ".." in candidate.parts:
        raise ConfigError("source.path cannot contain '..'")
    if candidate.is_absolute() and not allow_absolute:
        raise ConfigError(
            "absolute source.path requires an explicit trusted --allow-source-root"
        )
    resolved = (candidate if candidate.is_absolute() else recipe_dir / candidate).resolve()
    allowed = [root.resolve() for root in roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        raise ConfigError("source.path is outside the allowed local source roots")
    if not resolved.exists():
        raise ConfigError(f"source.path does not exist: {candidate}")
    return resolved


def _validate_no_pickle(path: Path) -> None:
    files = [path] if path.is_file() else (item for item in path.rglob("*") if item.is_file())
    forbidden = sorted(str(item) for item in files if item.suffix.lower() in PICKLE_SUFFIXES)
    if forbidden:
        raise ConfigError(
            "pickle-backed source formats are not accepted; use safetensors "
            f"(found {Path(forbidden[0]).name})"
        )


def _validate_source_tree(path: Path, source_format: str) -> None:
    if source_format == "safetensors" and not path.is_dir():
        raise ConfigError("safetensors source must be a checkpoint directory")
    entries = [path] if path.is_file() else list(path.rglob("*"))
    symlinks = [entry for entry in entries if entry.is_symlink()]
    if symlinks:
        raise ConfigError(
            f"source tree cannot contain symlinks (found {symlinks[0].relative_to(path)})"
        )
    if source_format == "safetensors":
        files = [entry for entry in entries if entry.is_file()]
        if not any(entry.suffix.lower() == ".safetensors" for entry in files):
            raise ConfigError("safetensors source must contain at least one .safetensors file")


def _tool_path(raw: Any, where: str) -> Path:
    path = Path(_string(raw, where))
    if not path.is_absolute():
        raise ConfigError(f"{where} must be an explicit absolute path")
    # Keep the final path component intact: invoking a virtual-environment Python
    # through its symlink is what activates that environment's site packages.
    normalized = Path(os.path.abspath(path))
    if not normalized.is_file():
        raise ConfigError(f"{where} does not exist or is not a file: {path}")
    return normalized


def load_config(
    recipe_path: str | Path, *, allowed_source_roots: Sequence[str | Path] | None = None
) -> ModelBakeConfig:
    """Load a v0 recipe and reject every field or behavior not explicitly supported.

    Local sources default to the recipe directory. Callers can narrow or replace that
    allowlist with ``allowed_source_roots``; recipe content cannot expand it.
    """

    path = Path(recipe_path).resolve()
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read recipe {path}: {exc}") from exc
    data = _mapping(raw, "recipe")
    if _contains_forbidden_key(data, "trust_remote_code"):
        raise ConfigError("trust_remote_code is forbidden")
    _strict_keys(
        data,
        allowed={"schema", "project", "source", "targets", "smoke", "runner"},
        required={"schema", "project", "source", "targets", "smoke", "runner"},
        where="recipe",
    )
    if data["schema"] != RECIPE_SCHEMA:
        raise ConfigError(f"schema must be exactly {RECIPE_SCHEMA!r}")

    project = _string(data["project"], "project")
    if not _PROJECT_RE.fullmatch(project):
        raise ConfigError("project must be 1-64 safe filename characters")

    source_data = _mapping(data["source"], "source")
    _strict_keys(
        source_data,
        allowed={"kind", "path", "architecture", "format"},
        required={"kind", "path", "architecture", "format"},
        where="source",
    )
    if source_data["kind"] != "local":
        raise ConfigError("source.kind must be 'local'; remote and executable sources are unsupported")
    roots = (
        tuple(Path(root) for root in allowed_source_roots)
        if allowed_source_roots is not None
        else (path.parent,)
    )
    if not roots:
        raise ConfigError("at least one allowed local source root is required")
    source_path = _safe_source_path(
        source_data["path"],
        path.parent,
        roots,
        allow_absolute=allowed_source_roots is not None,
    )
    architecture = _string(source_data["architecture"], "source.architecture")
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise ConfigError(
            f"unsupported architecture {architecture!r}; supported: {', '.join(sorted(SUPPORTED_ARCHITECTURES))}"
        )
    source_format = _string(source_data["format"], "source.format")
    if source_format not in {"safetensors", "fixture"}:
        raise ConfigError("source.format must be 'safetensors' or 'fixture'")
    _validate_source_tree(source_path, source_format)
    _validate_no_pickle(source_path)

    targets_data = data["targets"]
    if not isinstance(targets_data, list) or not targets_data:
        raise ConfigError("targets must be a non-empty list")
    targets: list[TargetConfig] = []
    seen_names: set[str] = set()
    for index, raw_target in enumerate(targets_data):
        target = _mapping(raw_target, f"targets[{index}]")
        _strict_keys(
            target,
            allowed={"name", "quantization"},
            required={"name", "quantization"},
            where=f"targets[{index}]",
        )
        name = _string(target["name"], f"targets[{index}].name")
        if not _PROJECT_RE.fullmatch(name) or name in seen_names:
            raise ConfigError(f"targets[{index}].name must be unique and filename-safe")
        quantization = _string(target["quantization"], f"targets[{index}].quantization")
        if quantization not in SUPPORTED_QUANTIZATIONS:
            raise ConfigError(
                f"unsupported quantization {quantization!r}; supported: F16, Q4_0, Q8_0"
            )
        seen_names.add(name)
        targets.append(TargetConfig(name=name, quantization=quantization))

    smoke_data = _mapping(data["smoke"], "smoke")
    _strict_keys(
        smoke_data,
        allowed={"prompt", "max_tokens"},
        required={"prompt", "max_tokens"},
        where="smoke",
    )
    smoke = SmokeConfig(
        prompt=_string(smoke_data["prompt"], "smoke.prompt"),
        max_tokens=_integer(smoke_data["max_tokens"], "smoke.max_tokens", minimum=1, maximum=128),
    )

    runner_data = _mapping(data["runner"], "runner")
    _strict_keys(
        runner_data,
        allowed={"adapter", "name", "timeout_seconds", "max_log_bytes", "tools"},
        required={"adapter", "name", "timeout_seconds", "max_log_bytes"},
        where="runner",
    )
    adapter = _string(runner_data["adapter"], "runner.adapter")
    if adapter not in {"fixture", "llamacpp"}:
        raise ConfigError("runner.adapter must be 'fixture' or 'llamacpp'")
    tools: ToolConfig | None = None
    if adapter == "fixture":
        if "tools" in runner_data:
            raise ConfigError("fixture runner does not accept tool paths or commands")
        if source_format != "fixture":
            raise ConfigError("fixture runner requires source.format 'fixture'")
    else:
        if source_format != "safetensors":
            raise ConfigError("llamacpp runner requires source.format 'safetensors'")
        tools_data = _mapping(runner_data.get("tools"), "runner.tools")
        _strict_keys(
            tools_data,
            allowed={"python", "convert", "quantize", "runner", "revision"},
            required={"python", "convert", "quantize", "runner", "revision"},
            where="runner.tools",
        )
        tools = ToolConfig(
            python=_tool_path(tools_data["python"], "runner.tools.python"),
            convert=_tool_path(tools_data["convert"], "runner.tools.convert"),
            quantize=_tool_path(tools_data["quantize"], "runner.tools.quantize"),
            runner=_tool_path(tools_data["runner"], "runner.tools.runner"),
            revision=_string(tools_data["revision"], "runner.tools.revision"),
        )
    runner = RunnerConfig(
        adapter=adapter,
        name=_string(runner_data["name"], "runner.name"),
        timeout_seconds=_integer(
            runner_data["timeout_seconds"], "runner.timeout_seconds", minimum=1, maximum=86_400
        ),
        max_log_bytes=_integer(
            runner_data["max_log_bytes"], "runner.max_log_bytes", minimum=256, maximum=10_000_000
        ),
        tools=tools,
    )
    return ModelBakeConfig(
        schema=RECIPE_SCHEMA,
        project=project,
        recipe_path=path,
        source=SourceConfig(source_path, architecture, source_format),
        targets=tuple(targets),
        smoke=smoke,
        runner=runner,
        canonical=data,
    )


def build_plan(config: ModelBakeConfig) -> BuildPlan:
    """Compile a validated recipe into the deliberately small v0 build graph."""

    canonical_json = json.dumps(config.canonical, sort_keys=True, separators=(",", ":")).encode()
    recipe_digest = "sha256:" + hashlib.sha256(canonical_json).hexdigest()
    source_digest = tree_digest(config.source.path)
    nodes: list[NodeSpec] = [
        NodeSpec(
            id="source",
            kind="source",
            config={
                "path": str(config.source.path),
                "format": config.source.format,
                "architecture": config.source.architecture,
                "digest": source_digest,
                "adapter": config.runner.adapter,
            },
        )
    ]
    f16_id: str | None = None
    for target in config.targets:
        if target.quantization == "F16":
            node_id = f"convert-{target.name}"
            nodes.append(
                NodeSpec(
                    id=node_id,
                    kind="convert",
                    dependencies=("source",),
                    config={"quantization": "F16", "adapter": config.runner.adapter},
                )
            )
            f16_id = node_id
            break
    if f16_id is None:
        f16_id = "convert-f16-base"
        nodes.append(
            NodeSpec(
                id=f16_id,
                kind="convert",
                dependencies=("source",),
                config={"quantization": "F16", "adapter": config.runner.adapter, "intermediate": True},
            )
        )
    artifact_nodes: dict[str, str] = {}
    for target in config.targets:
        if target.quantization == "F16":
            artifact_nodes[target.name] = f16_id
        else:
            node_id = f"quantize-{target.name}"
            nodes.append(
                NodeSpec(
                    id=node_id,
                    kind="quantize",
                    dependencies=(f16_id,),
                    config={"quantization": target.quantization, "adapter": config.runner.adapter},
                )
            )
            artifact_nodes[target.name] = node_id
    for target in config.targets:
        nodes.append(
            NodeSpec(
                id=f"smoke-{target.name}",
                kind="smoke",
                dependencies=(artifact_nodes[target.name],),
                config={
                    "prompt": config.smoke.prompt,
                    "max_tokens": config.smoke.max_tokens,
                    "target": target.name,
                    "quantization": target.quantization,
                    "adapter": config.runner.adapter,
                },
            )
        )
    runner = config.runner.to_dict()
    return BuildPlan(
        schema=RECIPE_SCHEMA,
        project=config.project,
        recipe_path=config.recipe_path,
        recipe_digest=recipe_digest,
        runner=runner,
        nodes=tuple(nodes),
        source={
            "kind": "local",
            "path": str(config.source.path),
            "format": config.source.format,
            "architecture": config.source.architecture,
            "digest": source_digest,
        },
        toolchain={"adapter": config.runner.adapter, **({"tools": runner["tools"]} if "tools" in runner else {})},
    )
