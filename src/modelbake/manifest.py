"""Read-only manifest summaries and byte-digest verification."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import tree_digest

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATUSES = frozenset({"running", "succeeded", "failed", "interrupted"})
_STATES = frozenset(
    {
        "produced",
        "fixture_observed",
        "executed_on_runner",
        "cache_hit",
        "failed",
        "blocked",
    }
)
_OUTPUT_STATES = frozenset(
    {"produced", "fixture_observed", "executed_on_runner", "cache_hit"}
)


class ManifestError(ValueError):
    pass


def _reject_nonfinite_json(value: str) -> None:
    raise ManifestError(f"manifest contains non-finite JSON value: {value}")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    checked: int
    failures: tuple[str, ...]


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        value = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonfinite_json,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {manifest_path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "modelbake.run.v1":
        raise ManifestError("manifest schema must be exactly 'modelbake.run.v1'")
    if not isinstance(value.get("nodes"), list):
        raise ManifestError("manifest nodes must be a list")
    if len(value["nodes"]) > 1_000:
        raise ManifestError("manifest contains too many nodes")
    if value.get("status") not in _STATUSES:
        raise ManifestError("manifest status is invalid")
    if not isinstance(value.get("run_id"), str) or not _SAFE_ID_RE.fullmatch(value["run_id"]):
        raise ManifestError("manifest run_id is invalid")
    for field in ("project", "recipe_digest", "source_digest", "started_at", "finished_at"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ManifestError(f"manifest {field} must be a non-empty string")
    for field in ("recipe_digest", "source_digest"):
        if not _DIGEST_RE.fullmatch(value[field]):
            raise ManifestError(f"manifest {field} is not a sha256 digest")
    if not isinstance(value.get("runner"), dict):
        raise ManifestError("manifest runner must be an object")

    seen_ids: set[str] = set()
    successful_outputs = 0
    failure_states = 0
    for index, node in enumerate(value["nodes"]):
        if not isinstance(node, dict):
            raise ManifestError(f"nodes[{index}] must be an object")
        node_id = node.get("id")
        state = node.get("state")
        if not isinstance(node_id, str) or not _SAFE_ID_RE.fullmatch(node_id):
            raise ManifestError(f"nodes[{index}].id is invalid")
        if node_id in seen_ids:
            raise ManifestError(f"duplicate node id: {node_id}")
        seen_ids.add(node_id)
        if state not in _STATES:
            raise ManifestError(f"{node_id}: state is invalid")
        outputs = node.get("outputs")
        digests = node.get("output_digests")
        if not isinstance(outputs, dict) or not isinstance(digests, dict):
            raise ManifestError(f"{node_id}: outputs and output_digests must be objects")
        if set(outputs) != set(digests):
            raise ManifestError(f"{node_id}: outputs and output_digests must have identical keys")
        for name, digest in digests.items():
            if not isinstance(name, str) or not _SAFE_ID_RE.fullmatch(name):
                raise ManifestError(f"{node_id}: invalid output name")
            if not isinstance(outputs[name], str) or not outputs[name]:
                raise ManifestError(f"{node_id}:{name}: output path is invalid")
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise ManifestError(f"{node_id}:{name}: output digest is invalid")
        if state in _OUTPUT_STATES and not outputs:
            raise ManifestError(f"{node_id}: successful state requires an output")
        successful_outputs += len(outputs)
        failure_states += int(state in {"failed", "blocked"})
        command = node.get("command")
        observations = node.get("observations")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ManifestError(f"{node_id}: command must be a string list")
        if not isinstance(observations, dict):
            raise ManifestError(f"{node_id}: observations must be an object")

    if value["status"] == "succeeded":
        if not value["nodes"] or not successful_outputs:
            raise ManifestError("succeeded manifest must contain at least one artifact")
        if failure_states:
            raise ManifestError("succeeded manifest cannot contain failed or blocked nodes")
    if value["status"] == "failed" and not failure_states:
        raise ManifestError("failed manifest must contain a failed or blocked node")
    return value


def verify_manifest(
    path: str | Path, *, allowed_roots: tuple[str | Path, ...]
) -> VerificationResult:
    """Recompute declared output digests; make no compatibility or quality claim."""

    manifest_path = Path(path).resolve()
    data = load_manifest(manifest_path)
    failures: list[str] = []
    checked = 0
    roots = tuple(Path(root).resolve() for root in allowed_roots)
    if not roots:
        raise ManifestError(
            "verification requires at least one explicit trusted artifact root"
        )
    for index, raw_node in enumerate(data["nodes"]):
        if not isinstance(raw_node, Mapping):
            failures.append(f"nodes[{index}] is not an object")
            continue
        node_id = str(raw_node.get("id", f"nodes[{index}]"))
        outputs = raw_node.get("outputs", {})
        digests = raw_node.get("output_digests", {})
        if not isinstance(outputs, Mapping) or not isinstance(digests, Mapping):
            failures.append(f"{node_id}: outputs or output_digests is not an object")
            continue
        for name, expected in sorted(digests.items()):
            output_value = outputs.get(name)
            if not isinstance(output_value, str) or not isinstance(expected, str):
                failures.append(f"{node_id}:{name}: invalid path or digest")
                continue
            raw_path = Path(output_value)
            if ".." in raw_path.parts:
                failures.append(f"{node_id}:{name}: path traversal is forbidden")
                continue
            output = (raw_path if raw_path.is_absolute() else manifest_path.parent / raw_path).resolve()
            if not any(output == root or output.is_relative_to(root) for root in roots):
                failures.append(f"{node_id}:{name}: output is outside allowed artifact roots")
                continue
            if not output.exists() and not output.is_symlink():
                failures.append(f"{node_id}:{name}: output is missing")
                continue
            try:
                observed = tree_digest(output)
            except (OSError, ValueError) as exc:
                failures.append(f"{node_id}:{name}: cannot digest output: {exc}")
                continue
            checked += 1
            if observed != expected:
                failures.append(
                    f"{node_id}:{name}: digest mismatch; expected {expected}, observed {observed}"
                )
    if checked == 0:
        failures.append("manifest contains no verifiable artifacts")
    return VerificationResult(not failures, checked, tuple(failures))


def render_report(data: Mapping[str, Any]) -> str:
    """Render deliberately narrow observed-result language."""

    lines = [
        f"ModelBake run {data.get('run_id', '<unknown>')}",
        f"project: {data.get('project', '<unknown>')}",
        f"recorded status: {data.get('status', '<unknown>')}",
    ]
    runner = data.get("runner", {})
    if isinstance(runner, Mapping):
        declared = runner.get("declared", runner)
        if isinstance(declared, Mapping):
            lines.append(f"declared runner: {declared.get('name', declared.get('adapter', '<unknown>'))}")
    lines.append("nodes:")
    for node in data.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        state = node.get("state", "unknown")
        summary = f"  {node.get('id', '<unknown>')}: {state}"
        observations = node.get("observations")
        if isinstance(observations, Mapping) and observations.get("runner"):
            summary += f" on {observations['runner']}"
        lines.append(summary)
    lines.extend(
        [
            "truth boundary:",
            "  produced = declared bytes were produced and digested",
            "  digest verified = current bytes match a recorded digest",
            "  fixture_observed = the in-process fixture adapter decoded its private test artifact",
            "  executed_on_runner = the configured external runner process returned success",
            "  no compatibility, quality, or numerical-equivalence conclusion is implied",
        ]
    )
    return "\n".join(lines)
