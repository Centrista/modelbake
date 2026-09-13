"""Reviewable, claim-scoped comparisons between two ModelBake run manifests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .hashing import digest_json
from .manifest import load_manifest

_NODE_FIELDS = (
    "kind",
    "cache_key",
    "input_digests",
    "output_digests",
    "command",
    "state",
)
_CORE_FIELDS = frozenset({"kind", "cache_key", "input_digests", "output_digests", "command"})


def _mapping_digest(value: Any) -> str:
    return digest_json(value if isinstance(value, (dict, list, str, int, float, bool)) else None)


def _runner_parts(manifest: Mapping[str, Any]) -> tuple[Any, Any]:
    runner = manifest.get("runner", {})
    if not isinstance(runner, Mapping):
        return {}, {}
    return runner.get("declared", {}), runner.get("observed", {})


def _node_map(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(node["id"]): node for node in manifest["nodes"] if isinstance(node, Mapping)}


def _changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Name changed fields without emitting potentially sensitive values."""

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_changed_paths(before[key], after[key], child))
        return paths
    return [] if before == after else [prefix or "value"]


def _node_snapshot(node: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
        "kind": node.get("kind"),
        "state": node.get("state"),
        "cache_key": node.get("cache_key"),
        "input_digests": node.get("input_digests", {}),
        "output_digests": node.get("output_digests", {}),
        "command_digest": _mapping_digest(node.get("command", [])),
        "observations_digest": _mapping_digest(node.get("observations", {})),
    }


def _runner_comparison(before: Any, after: Any) -> dict[str, Any]:
    before_value = before if isinstance(before, Mapping) else {}
    after_value = after if isinstance(after, Mapping) else {}
    before_digest = _mapping_digest(before_value)
    after_digest = _mapping_digest(after_value)
    return {
        "same": before_digest == after_digest,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "changed_fields": _changed_paths(before_value, after_value)[:100],
    }


def compare_manifests(before_path: str | Path, after_path: str | Path) -> dict[str, Any]:
    """Compare recorded lineage without inferring promotion safety or model quality."""

    before = load_manifest(before_path)
    after = load_manifest(after_path)
    before_declared, before_observed = _runner_parts(before)
    after_declared, after_observed = _runner_parts(after)
    before_nodes = _node_map(before)
    after_nodes = _node_map(after)
    nodes: list[dict[str, Any]] = []

    for node_id in sorted(set(before_nodes) | set(after_nodes)):
        previous = before_nodes.get(node_id)
        current = after_nodes.get(node_id)
        if previous is None:
            nodes.append(
                {
                    "id": node_id,
                    "change": "added",
                    "changed_fields": ["node"],
                    "recorded_output_digests": "added",
                    "before_state": None,
                    "after_state": current.get("state"),
                    "before": None,
                    "after": _node_snapshot(current),
                }
            )
            continue
        if current is None:
            nodes.append(
                {
                    "id": node_id,
                    "change": "removed",
                    "changed_fields": ["node"],
                    "recorded_output_digests": "removed",
                    "before_state": previous.get("state"),
                    "after_state": None,
                    "before": _node_snapshot(previous),
                    "after": None,
                }
            )
            continue

        changed_fields = [field for field in _NODE_FIELDS if previous.get(field) != current.get(field)]
        if _mapping_digest(previous.get("observations", {})) != _mapping_digest(
            current.get("observations", {})
        ):
            changed_fields.append("observations")
        outputs_same = previous.get("output_digests", {}) == current.get("output_digests", {})
        core_same = not (_CORE_FIELDS & set(changed_fields))
        if not changed_fields:
            change = "unchanged"
        elif core_same and current.get("state") == "cache_hit":
            change = "reused_from_cache"
        elif outputs_same:
            change = "evidence_changed_same_recorded_output"
        else:
            change = "recorded_output_changed"
        nodes.append(
            {
                "id": node_id,
                "change": change,
                "changed_fields": changed_fields,
                "recorded_output_digests": "same" if outputs_same else "changed",
                "before_state": previous.get("state"),
                "after_state": current.get("state"),
                "before": _node_snapshot(previous),
                "after": _node_snapshot(current),
            }
        )

    counts = {
        name: sum(node["change"] == name for node in nodes)
        for name in (
            "unchanged",
            "reused_from_cache",
            "evidence_changed_same_recorded_output",
            "recorded_output_changed",
            "added",
            "removed",
        )
    }
    return {
        "schema": "modelbake.compare.v1",
        "before": {
            "run_id": before["run_id"],
            "status": before["status"],
            "project": before["project"],
        },
        "after": {
            "run_id": after["run_id"],
            "status": after["status"],
            "project": after["project"],
        },
        "lineage": {
            "project_same": before["project"] == after["project"],
            "recipe_same": before["recipe_digest"] == after["recipe_digest"],
            "source_digest_same": before["source_digest"] == after["source_digest"],
            "declared_runner_same": _mapping_digest(before_declared)
            == _mapping_digest(after_declared),
            "observed_runner_same": _mapping_digest(before_observed)
            == _mapping_digest(after_observed),
            "recipe": {
                "same": before["recipe_digest"] == after["recipe_digest"],
                "before_digest": before["recipe_digest"],
                "after_digest": after["recipe_digest"],
            },
            "source": {
                "same": before["source_digest"] == after["source_digest"],
                "before_digest": before["source_digest"],
                "after_digest": after["source_digest"],
            },
            "declared_runner": _runner_comparison(before_declared, after_declared),
            "observed_runner": _runner_comparison(before_observed, after_observed),
        },
        "counts": counts,
        "nodes": nodes,
        "promotion_decision": "not_inferred",
        "truth_boundary": (
            "This comparison reads recorded manifest values and digests; it does not reread "
            "artifact files. Run modelbake verify with explicit trusted artifact roots for "
            "current-byte integrity. Neither command establishes model quality, compatibility, "
            "or promotion safety."
        ),
    }


def _short_digest(value: Any) -> str:
    rendered = str(value or "—")
    return rendered if len(rendered) <= 20 else f"{rendered[:15]}…{rendered[-4:]}"


def _output_transition(node: Mapping[str, Any]) -> str:
    before = node.get("before")
    after = node.get("after")
    before_digests = before.get("output_digests", {}) if isinstance(before, Mapping) else {}
    after_digests = after.get("output_digests", {}) if isinstance(after, Mapping) else {}
    names = sorted(set(before_digests) | set(after_digests))
    if not names:
        return "—"
    return ", ".join(
        f"{name}: {_short_digest(before_digests.get(name))} → "
        f"{_short_digest(after_digests.get(name))}"
        for name in names
    )


def render_comparison(comparison: Mapping[str, Any], *, markdown: bool = False) -> str:
    """Render a compact terminal or GitHub-ready comparison."""

    before = comparison["before"]
    after = comparison["after"]
    lineage = comparison["lineage"]
    nodes = comparison["nodes"]
    marker = {
        "unchanged": "=",
        "reused_from_cache": "↺",
        "evidence_changed_same_recorded_output": "~",
        "recorded_output_changed": "!",
        "added": "+",
        "removed": "-",
    }
    if markdown:
        lines = [
            "## ModelBake release comparison",
            "",
            (
                f"`{before['run_id']}` ({before['status']}) → "
                f"`{after['run_id']}` ({after['status']})"
            ),
            "",
            "| Lineage | Result |",
            "| --- | --- |",
            f"| Recipe | {'same' if lineage['recipe_same'] else 'changed'} |",
            f"| Recorded source digest | {'same' if lineage['source_digest_same'] else 'changed'} |",
            f"| Declared runner | {'same' if lineage['declared_runner_same'] else 'changed'} |",
            f"| Observed runner | {'same' if lineage['observed_runner_same'] else 'changed'} |",
            f"| Recipe digest | `{lineage['recipe']['before_digest']}` → `{lineage['recipe']['after_digest']}` |",
            f"| Source digest | `{lineage['source']['before_digest']}` → `{lineage['source']['after_digest']}` |",
            f"| Declared runner fields | {', '.join(lineage['declared_runner']['changed_fields']) or 'none'} |",
            f"| Observed runner fields | {', '.join(lineage['observed_runner']['changed_fields']) or 'none'} |",
            "",
            "| Node | Change | Changed fields | Recorded output digest transition | Before → after |",
            "| --- | --- | --- | --- | --- |",
        ]
        for node in nodes:
            lines.append(
                f"| `{node['id']}` | {node['change']} | "
                f"{', '.join(node['changed_fields']) or 'none'} | `{_output_transition(node)}` | "
                f"`{node['before_state'] or '—'}` → `{node['after_state'] or '—'}` |"
            )
        lines.extend(["", f"> {comparison['truth_boundary']}"])
        return "\n".join(lines)

    lines = [
        (
            f"ModelBake comparison {before['run_id']} ({before['status']}) -> "
            f"{after['run_id']} ({after['status']})"
        ),
        f"recipe: {'same' if lineage['recipe_same'] else 'changed'}",
        f"recorded source digest: {'same' if lineage['source_digest_same'] else 'changed'}",
        f"declared runner: {'same' if lineage['declared_runner_same'] else 'changed'}",
        f"observed runner: {'same' if lineage['observed_runner_same'] else 'changed'}",
        f"recipe digest: {lineage['recipe']['before_digest']} -> {lineage['recipe']['after_digest']}",
        f"source digest: {lineage['source']['before_digest']} -> {lineage['source']['after_digest']}",
        "declared runner fields changed: "
        + (", ".join(lineage["declared_runner"]["changed_fields"]) or "none"),
        "observed runner fields changed: "
        + (", ".join(lineage["observed_runner"]["changed_fields"]) or "none"),
        "nodes:",
    ]
    for node in nodes:
        fields = ", ".join(node["changed_fields"]) or "none"
        lines.append(
            f"  {marker[node['change']]} {node['id']}: {node['change']} "
            f"(recorded output digests {node['recorded_output_digests']}; "
            f"{_output_transition(node)}; changed: {fields})"
        )
    lines.extend(["truth boundary:", f"  {comparison['truth_boundary']}"])
    return "\n".join(lines)
