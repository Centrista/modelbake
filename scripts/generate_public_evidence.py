#!/usr/bin/env python3
"""Create path-free public evidence from a verified two-run acceptance test."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
import tempfile
import zipfile
from collections import Counter
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from modelbake.adapters.base import AdapterError
from modelbake.adapters.llamacpp import LlamaCppAdapter
from modelbake.baseline import (
    BaselineError,
    check_baseline,
    load_current_baseline,
    verify_ledger,
)
from modelbake.hashing import (
    HashingError,
    canonical_json_bytes,
    digest_bytes,
    digest_json,
    python_source_digest,
    tree_digest,
)
from modelbake.manifest import ManifestError, load_manifest, verify_manifest

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_REDACTED_PROMPT_RE = re.compile(r"^<redacted:(sha256:[0-9a-f]{64})>$")
_SAFE_PUBLIC_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,127}$")
_EXPECTED_KINDS = Counter({"source": 1, "convert": 1, "quantize": 2, "smoke": 2})
_EXPECTED_TOOLS = frozenset({"python", "convert", "quantize", "runner"})
_MAX_WHEEL_MEMBERS = 10_000
_MAX_PYTHON_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_REVIEW_BYTES = 16 * 1024 * 1024
_PUBLIC_SCHEMA = "modelbake.public-acceptance.v3"
_PUBLIC_EXCLUSIONS = (
    "Digest verification is not a model-quality evaluation.",
    "Runner exit 0 is not a compatibility guarantee for other hosts.",
    "Displayed argv replaces local paths with digest-bound placeholders; prompt text, logs, stdout, and stderr are excluded.",
    "Package hashes bind this record to exact local release files; they do not prove publication.",
    "The publisher-generated CAS acceptance records no identity, authorization, or independent attestation.",
)
_GGUF_HEADER_SIZE = 24
_GGUF_VERSIONS = frozenset({1, 2, 3})


class PublicEvidenceError(ValueError):
    """Raised when acceptance evidence is incomplete, stale, or unsafe."""


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PublicEvidenceError(f"cannot inspect {label}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PublicEvidenceError(f"{label} must be a regular file, not a symlink")
    return info


def _file_record(path: Path, label: str) -> dict[str, Any]:
    """Hash one real file through a no-follow descriptor and detect mutation."""

    before = _require_regular_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicEvidenceError(f"cannot open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise PublicEvidenceError(f"{label} changed while being opened")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise PublicEvidenceError(f"{label} changed while being hashed")
        return {"sha256": f"sha256:{digest.hexdigest()}", "size_bytes": size}
    finally:
        os.close(descriptor)


def _same_file_record(
    before: dict[str, Any], after: dict[str, Any], label: str
) -> None:
    if before != after:
        raise PublicEvidenceError(f"{label} changed during evidence generation")


def _modelbake_file_digest(
    path: Path, expected_record: dict[str, Any], label: str
) -> str:
    """Return ModelBake's tagged bytes digest while detecting file mutation."""

    data = _read_regular_file(path, label, maximum=_MAX_MANIFEST_BYTES)
    _same_file_record(expected_record, _file_record(path, label), label)
    return digest_bytes(data)


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise PublicEvidenceError(f"{label} must be a sha256 digest")
    return value


def _require_public_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_PUBLIC_TEXT_RE.fullmatch(value) is None:
        raise PublicEvidenceError(f"{label} is not safe public text")
    return value


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise PublicEvidenceError(
            f"{label} fields must be exactly {sorted(expected)}; got {actual}"
        )
    return value


def _require_size(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicEvidenceError(f"{label} must be a non-negative integer")
    return value


def _nodes_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(node["id"]): node for node in manifest["nodes"]}


def _stable_stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _regular_file_size_at(
    parent_descriptor: int, name: str, before: os.stat_result, label: str
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise PublicEvidenceError(f"cannot inspect output file {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise PublicEvidenceError(f"output file changed while sizing: {label}")
        after = os.fstat(descriptor)
        if _stable_stat_identity(after) != _stable_stat_identity(opened):
            raise PublicEvidenceError(f"output file changed while sizing: {label}")
        return opened.st_size
    finally:
        os.close(descriptor)


def _directory_regular_bytes(descriptor: int, label: str) -> int:
    before = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode):
        raise PublicEvidenceError(f"output directory changed while sizing: {label}")
    try:
        with os.scandir(descriptor) as entries:
            children = sorted(entries, key=lambda entry: os.fsencode(entry.name))
    except OSError as exc:
        raise PublicEvidenceError(
            f"cannot list output directory {label}: {exc}"
        ) from exc
    total = 0
    for entry in children:
        child_label = f"{label}/{entry.name}"
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise PublicEvidenceError(
                f"cannot inspect output {child_label}: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISREG(info.st_mode):
            total += _regular_file_size_at(descriptor, entry.name, info, child_label)
            continue
        if stat.S_ISDIR(info.st_mode):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child_descriptor = os.open(entry.name, flags, dir_fd=descriptor)
            except OSError as exc:
                raise PublicEvidenceError(
                    f"cannot open output directory {child_label}: {exc}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise PublicEvidenceError(
                        f"output directory changed while sizing: {child_label}"
                    )
                total += _directory_regular_bytes(child_descriptor, child_label)
            finally:
                os.close(child_descriptor)
            continue
        raise PublicEvidenceError(f"special output is not sizeable: {child_label}")
    after = os.fstat(descriptor)
    if _stable_stat_identity(after) != _stable_stat_identity(before):
        raise PublicEvidenceError(f"output directory changed while sizing: {label}")
    return total


def _regular_output_bytes(path: Path, label: str) -> int:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PublicEvidenceError(f"cannot inspect output {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return 0
    if stat.S_ISREG(info.st_mode):
        parent_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            parent_descriptor = os.open(path.parent, parent_flags)
        except OSError as exc:
            raise PublicEvidenceError(
                f"cannot inspect output parent for {label}: {exc}"
            ) from exc
        try:
            return _regular_file_size_at(parent_descriptor, path.name, info, label)
        finally:
            os.close(parent_descriptor)
    if stat.S_ISDIR(info.st_mode):
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise PublicEvidenceError(
                f"cannot open output directory {label}: {exc}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise PublicEvidenceError(
                    f"output directory changed while sizing: {label}"
                )
            return _directory_regular_bytes(descriptor, label)
        finally:
            os.close(descriptor)
    raise PublicEvidenceError(f"special output is not sizeable: {label}")


def _output_sizes(
    manifest: dict[str, Any], manifest_path: Path, artifact_root: Path
) -> dict[str, dict[str, int]]:
    sizes: dict[str, dict[str, int]] = {}
    for node in manifest["nodes"]:
        node_sizes: dict[str, int] = {}
        for name, raw_output in sorted(node["outputs"].items()):
            raw_path = Path(raw_output)
            if ".." in raw_path.parts:
                raise PublicEvidenceError(
                    f"{node['id']}:{name}: output traversal is forbidden"
                )
            candidate = (
                raw_path if raw_path.is_absolute() else manifest_path.parent / raw_path
            ).resolve()
            if not (
                candidate == artifact_root or candidate.is_relative_to(artifact_root)
            ):
                raise PublicEvidenceError(
                    f"{node['id']}:{name}: output is outside the artifact root"
                )
            node_sizes[name] = _regular_output_bytes(candidate, f"{node['id']}:{name}")
        if set(node_sizes) != set(node["output_digests"]):
            raise PublicEvidenceError(f"{node['id']}: output size map is incomplete")
        sizes[node["id"]] = node_sizes
    return sizes


def _resolved_output(
    node: dict[str, Any], manifest_path: Path, artifact_root: Path
) -> Path:
    if len(node["outputs"]) != 1:
        raise PublicEvidenceError(f"{node['id']}: exactly one output is required")
    raw_output = next(iter(node["outputs"].values()))
    raw_path = Path(raw_output)
    if ".." in raw_path.parts:
        raise PublicEvidenceError(f"{node['id']}: output traversal is forbidden")
    candidate = (
        raw_path if raw_path.is_absolute() else manifest_path.parent / raw_path
    ).resolve()
    if not (candidate == artifact_root or candidate.is_relative_to(artifact_root)):
        raise PublicEvidenceError(f"{node['id']}: output is outside the artifact root")
    return candidate


def _read_regular_file(path: Path, label: str, *, maximum: int) -> bytes:
    """Read a bounded regular file without following a final symlink."""

    before = _require_regular_file(path, label)
    if before.st_size > maximum:
        raise PublicEvidenceError(f"{label} exceeds the evidence inspection limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicEvidenceError(f"cannot open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PublicEvidenceError(f"{label} changed while being opened")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0 and (chunk := os.read(descriptor, min(65_536, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if _stable_stat_identity(after) != _stable_stat_identity(opened):
            raise PublicEvidenceError(f"{label} changed while being inspected")
        if len(data) != opened.st_size:
            raise PublicEvidenceError(f"{label} changed while being inspected")
        return data
    finally:
        os.close(descriptor)


def _read_regular_prefix(path: Path, label: str, *, length: int) -> bytes:
    """Read an exact prefix while keeping a stable descriptor identity."""

    before = _require_regular_file(path, label)
    if before.st_size < length:
        raise PublicEvidenceError(f"{label} is truncated")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicEvidenceError(f"cannot open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PublicEvidenceError(f"{label} changed while being opened")
        data = b""
        while len(data) < length:
            chunk = os.read(descriptor, length - len(data))
            if not chunk:
                break
            data += chunk
        after = os.fstat(descriptor)
        if _stable_stat_identity(after) != _stable_stat_identity(opened):
            raise PublicEvidenceError(f"{label} changed while being inspected")
        if len(data) != length:
            raise PublicEvidenceError(f"{label} is truncated")
        return data
    finally:
        os.close(descriptor)


def _validate_gguf_artifacts(
    manifest: dict[str, Any], manifest_path: Path, artifact_root: Path
) -> None:
    """Require the converter and quantizers to have emitted plausible GGUF files.

    This deliberately checks only the fixed GGUF header. It prevents arbitrary bytes
    from being presented as a successful GGUF acceptance, but does not claim that the
    complete tensor or metadata sections are semantically valid.
    """

    for node in manifest["nodes"]:
        if node["kind"] not in {"convert", "quantize"}:
            continue
        path = _resolved_output(node, manifest_path, artifact_root)
        before = _require_regular_file(path, f"{node['id']} GGUF artifact")
        if before.st_size < _GGUF_HEADER_SIZE:
            raise PublicEvidenceError(f"{node['id']}: GGUF artifact is truncated")
        data = _read_regular_prefix(
            path,
            f"{node['id']} GGUF artifact",
            length=_GGUF_HEADER_SIZE,
        )
        magic, version, tensor_count, metadata_count = struct.unpack("<4sIQQ", data)
        if magic != b"GGUF":
            raise PublicEvidenceError(f"{node['id']}: artifact has invalid GGUF magic")
        if version not in _GGUF_VERSIONS:
            raise PublicEvidenceError(f"{node['id']}: unsupported GGUF version {version}")
        if tensor_count <= 0 or metadata_count <= 0:
            raise PublicEvidenceError(
                f"{node['id']}: GGUF header must declare tensors and metadata"
            )


def _validate_smoke_artifacts(
    manifest: dict[str, Any], manifest_path: Path, artifact_root: Path
) -> None:
    """Bind each smoke observation file to nonempty recorded llama-cli stdout."""

    for node in manifest["nodes"]:
        if node["kind"] != "smoke":
            continue
        observations = node["observations"]
        stdout = observations.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            raise PublicEvidenceError(
                f"{node['id']}: llama-cli recorded no nonempty stdout evidence"
            )
        path = _resolved_output(node, manifest_path, artifact_root)
        data = _read_regular_file(path, f"{node['id']} smoke observation", maximum=1024 * 1024)
        try:
            rendered = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicEvidenceError(
                f"{node['id']}: smoke observation is not UTF-8 text"
            ) from exc
        if not rendered.strip() or rendered != stdout:
            raise PublicEvidenceError(
                f"{node['id']}: smoke observation does not match recorded llama-cli stdout"
            )


def _declared_runner(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    declared = manifest["runner"].get("declared")
    if not isinstance(declared, dict) or declared.get("adapter") != "llamacpp":
        raise PublicEvidenceError("acceptance runner must be the llamacpp adapter")
    tools = declared.get("tools")
    if not isinstance(tools, dict) or set(tools) != _EXPECTED_TOOLS | {"revision"}:
        raise PublicEvidenceError("declared llama.cpp tools are incomplete")
    normalized: dict[str, str] = {}
    for name in sorted(_EXPECTED_TOOLS):
        value = tools.get(name)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise PublicEvidenceError(f"declared {name} tool must be an absolute path")
        normalized[name] = value
    if Path(normalized["convert"]).name != "convert_hf_to_gguf.py":
        raise PublicEvidenceError(
            "converter is not the configured official llama.cpp converter"
        )
    if Path(normalized["quantize"]).name != "llama-quantize":
        raise PublicEvidenceError(
            "quantizer is not the configured official llama.cpp binary"
        )
    if Path(normalized["runner"]).name != "llama-cli":
        raise PublicEvidenceError(
            "runner is not the configured official llama-cli binary"
        )
    revision = tools.get("revision")
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise PublicEvidenceError(
            "declared llama.cpp revision must be a full Git commit"
        )
    return declared, normalized


def _observe_live_toolchain(
    declared: dict[str, Any], tools: dict[str, str]
) -> dict[str, Any]:
    """Re-open the configured files and re-observe the pinned local checkout."""

    try:
        adapter = LlamaCppAdapter(declared)
        fingerprint = adapter.fingerprint()
    except (AdapterError, HashingError, OSError) as exc:
        raise PublicEvidenceError(f"cannot observe live llama.cpp toolchain: {exc}") from exc

    fingerprint_tools = fingerprint.get("tools")
    if not isinstance(fingerprint_tools, dict) or set(fingerprint_tools) != _EXPECTED_TOOLS:
        raise PublicEvidenceError("live llama.cpp tool fingerprint is incomplete")
    for name in sorted(_EXPECTED_TOOLS):
        configured = Path(tools[name])
        try:
            configured_digest_before = tree_digest(configured)
            resolved_before = configured.resolve(strict=True)
        except (OSError, HashingError) as exc:
            raise PublicEvidenceError(f"cannot inspect configured {name} tool: {exc}") from exc
        record = _file_record(resolved_before, f"resolved {name} tool")
        observed = fingerprint_tools.get(name)
        expected = {
            "path": os.path.abspath(configured),
            "resolved_path": str(resolved_before),
            "configured_path_digest": configured_digest_before,
            "resolved_file_digest": record["sha256"],
        }
        if observed != expected:
            raise PublicEvidenceError(
                f"live {name} tool fingerprint does not match its configured file"
            )
        try:
            resolved_after = configured.resolve(strict=True)
            configured_digest_after = tree_digest(configured)
        except (OSError, HashingError) as exc:
            raise PublicEvidenceError(f"cannot re-inspect configured {name} tool: {exc}") from exc
        _same_file_record(
            record,
            _file_record(resolved_after, f"resolved {name} tool"),
            f"resolved {name} tool",
        )
        if (
            resolved_after != resolved_before
            or configured_digest_after != configured_digest_before
        ):
            raise PublicEvidenceError(f"configured {name} tool changed during inspection")

    repository = fingerprint.get("tool_source_repository")
    if (
        not isinstance(repository, dict)
        or repository.get("available") is not True
        or repository.get("observed_head") != declared["tools"]["revision"]
        or repository.get("declared_revision_matches_head") is not True
        or repository.get("tracked_or_untracked_changes") is not False
        or repository.get("status_probe_exit_code") != 0
        or repository.get("status_probe_timed_out") is not False
        or repository.get("status_probe_truncated") is not False
    ):
        raise PublicEvidenceError(
            "live llama.cpp checkout revision or clean status is not verified"
        )
    _require_digest(repository.get("status_digest"), "live Git status digest")
    _require_digest(repository.get("worktree_digest"), "live llama.cpp worktree digest")

    python_environment = fingerprint.get("python_environment")
    if (
        not isinstance(python_environment, dict)
        or python_environment.get("probe_exit_code") != 0
        or python_environment.get("probe_timed_out") is not False
        or python_environment.get("probe_truncated") is not False
        or not isinstance(python_environment.get("interpreter"), dict)
        or not isinstance(python_environment.get("dependency_versions"), dict)
    ):
        raise PublicEvidenceError("live configured Python environment is not verified")
    _require_digest(
        python_environment.get("dependency_versions_digest"),
        "live Python dependency versions digest",
    )
    if fingerprint.get("adapter") != "modelbake.llamacpp/v0":
        raise PublicEvidenceError("live llama.cpp adapter fingerprint is invalid")
    if fingerprint.get("revision") != declared["tools"]["revision"]:
        raise PublicEvidenceError("live llama.cpp revision differs from the declared revision")
    return fingerprint


def _require_recorded_execution_status(node: dict[str, Any]) -> None:
    observations = node["observations"]
    if observations.get("exit_code") != 0:
        raise PublicEvidenceError(f"{node['id']}: configured tool did not record exit 0")
    if observations.get("log_truncated") is not False:
        raise PublicEvidenceError(f"{node['id']}: configured tool output was truncated")
    if observations.get("timed_out") not in (None, False):
        raise PublicEvidenceError(f"{node['id']}: configured tool execution timed out")
    if (
        isinstance(observations.get("timeout_seconds"), bool)
        or not isinstance(observations.get("timeout_seconds"), int)
        or observations["timeout_seconds"] <= 0
    ):
        raise PublicEvidenceError(f"{node['id']}: execution timeout is invalid")
    if (
        isinstance(observations.get("log_bytes_recorded"), bool)
        or not isinstance(observations.get("log_bytes_recorded"), int)
        or observations["log_bytes_recorded"] < 0
    ):
        raise PublicEvidenceError(f"{node['id']}: recorded output size is invalid")
    if not isinstance(observations.get("stdout"), str) or not isinstance(
        observations.get("stderr"), str
    ):
        raise PublicEvidenceError(f"{node['id']}: recorded tool output is invalid")


def _validate_live_observations(
    manifest: dict[str, Any], live: dict[str, Any]
) -> None:
    expected_tool_digests = {
        name: live["tools"][name]["resolved_file_digest"]
        for name in sorted(_EXPECTED_TOOLS)
    }
    expected_common = {
        "adapter": live["adapter"],
        "revision": live["revision"],
        "runner": live["runner_name"],
        "tool_digests": expected_tool_digests,
        "tool_versions": live["versions"],
        "python_environment": live["python_environment"],
        "tool_source_repository": live["tool_source_repository"],
    }
    for node in manifest["nodes"]:
        observations = node["observations"]
        for field, expected in expected_common.items():
            if observations.get(field) != expected:
                raise PublicEvidenceError(
                    f"{node['id']}: recorded {field} differs from live toolchain evidence"
                )
        if node["kind"] == "source":
            if observations.get("operation") != "local_source_copied":
                raise PublicEvidenceError(f"{node['id']}: source operation is invalid")
            continue
        _require_recorded_execution_status(node)
        expected_operation = {
            "convert": "converter_executed",
            "quantize": "quantizer_executed",
            "smoke": "artifact_executed_on_named_runner",
        }[node["kind"]]
        if observations.get("operation") != expected_operation:
            raise PublicEvidenceError(f"{node['id']}: recorded operation is invalid")


def _require_exit_zero(node: dict[str, Any]) -> None:
    _require_recorded_execution_status(node)


def _validate_convert_command(node: dict[str, Any], tools: dict[str, str]) -> None:
    command = node["command"]
    if (
        len(command) != 7
        or command[0] != tools["python"]
        or command[1] != tools["convert"]
        or not command[2]
        or command[3:] != ["--outfile", command[4], "--outtype", "f16"]
        or not command[4]
    ):
        raise PublicEvidenceError(
            f"{node['id']}: converter command is not the exact configured form"
        )
    _require_exit_zero(node)


def _validate_quantize_command(node: dict[str, Any], tools: dict[str, str]) -> str:
    command = node["command"]
    if (
        len(command) != 4
        or command[0] != tools["quantize"]
        or not command[1]
        or not command[2]
        or command[3] not in {"Q4_0", "Q8_0"}
    ):
        raise PublicEvidenceError(
            f"{node['id']}: quantizer command is not the exact configured form"
        )
    if node["observations"].get("quantization") != command[3]:
        raise PublicEvidenceError(
            f"{node['id']}: quantization observation does not match command"
        )
    _require_exit_zero(node)
    return command[3]


def _validate_smoke_command(node: dict[str, Any], tools: dict[str, str]) -> str:
    command = node["command"]
    expected_shape = (
        len(command) == 10
        and command[0] == tools["runner"]
        and command[1] == "-m"
        and bool(command[2])
        and command[3] == "-p"
        and command[5] == "-n"
        and command[6].isdigit()
        and int(command[6]) > 0
        and command[7:9] == ["--seed", "0"]
        and command[9] == "--single-turn"
        and command.count("--single-turn") == 1
        and command.count("-p") == 1
    )
    if not expected_shape:
        raise PublicEvidenceError(
            f"{node['id']}: smoke command is not the exact configured form"
        )
    match = _REDACTED_PROMPT_RE.fullmatch(command[4])
    if match is None:
        raise PublicEvidenceError(f"{node['id']}: smoke prompt is not redacted")
    prompt_digest = _require_digest(
        node["observations"].get("prompt_digest"),
        f"{node['id']} prompt digest",
    )
    if prompt_digest != match.group(1):
        raise PublicEvidenceError(
            f"{node['id']}: redacted prompt digest does not match observation"
        )
    _require_exit_zero(node)
    return prompt_digest


def _validate_commands(manifest: dict[str, Any], tools: dict[str, str]) -> None:
    quantizations: set[str] = set()
    smoke_prompt_digests: set[str] = set()
    for node in manifest["nodes"]:
        kind = node.get("kind")
        if kind == "source":
            if node["command"]:
                raise PublicEvidenceError(f"{node['id']}: source command must be empty")
        elif kind == "convert":
            _validate_convert_command(node, tools)
        elif kind == "quantize":
            quantizations.add(_validate_quantize_command(node, tools))
        elif kind == "smoke":
            smoke_prompt_digests.add(_validate_smoke_command(node, tools))
        else:
            raise PublicEvidenceError(f"{node['id']}: unsupported acceptance node kind")
    if quantizations != {"Q4_0", "Q8_0"}:
        raise PublicEvidenceError(
            "acceptance must execute both Q4_0 and Q8_0 quantizers"
        )
    if len(smoke_prompt_digests) != 1:
        raise PublicEvidenceError(
            "both smoke executions must use the same redacted prompt"
        )


def _validate_runner_observations(
    manifest: dict[str, Any], declared: dict[str, Any]
) -> tuple[dict[str, str], dict[str, Any]]:
    tools = declared["tools"]
    revision = str(tools["revision"])
    expected_tool_digests: dict[str, str] | None = None
    expected_repository: dict[str, Any] | None = None
    for node in manifest["nodes"]:
        observations = node["observations"]
        if observations.get("revision") != revision:
            raise PublicEvidenceError(
                f"{node['id']}: observed revision differs from declared revision"
            )
        if observations.get("adapter") != "modelbake.llamacpp/v0":
            raise PublicEvidenceError(f"{node['id']}: unexpected adapter observation")
        digests = observations.get("tool_digests")
        if not isinstance(digests, dict) or set(digests) != _EXPECTED_TOOLS:
            raise PublicEvidenceError(
                f"{node['id']}: observed tool digests are incomplete"
            )
        normalized_digests = {
            name: _require_digest(digests[name], f"{node['id']} {name} tool digest")
            for name in sorted(_EXPECTED_TOOLS)
        }
        if expected_tool_digests is None:
            expected_tool_digests = normalized_digests
        elif normalized_digests != expected_tool_digests:
            raise PublicEvidenceError(
                "tool file digests changed within one acceptance run"
            )

        repository = observations.get("tool_source_repository")
        if (
            not isinstance(repository, dict)
            or repository.get("available") is not True
            or repository.get("observed_head") != revision
            or repository.get("declared_revision_matches_head") is not True
            or repository.get("status_probe_exit_code") != 0
            or repository.get("status_probe_timed_out") is not False
            or repository.get("status_probe_truncated") is not False
        ):
            raise PublicEvidenceError(
                f"{node['id']}: llama.cpp checkout revision or clean status is not verified"
            )
        sanitized_repository = {
            "worktree_digest": _require_digest(
                repository.get("worktree_digest"),
                f"{node['id']} llama.cpp worktree digest",
            ),
            "tracked_or_untracked_changes": repository.get(
                "tracked_or_untracked_changes"
            ),
        }
        if sanitized_repository["tracked_or_untracked_changes"] is not False:
            raise PublicEvidenceError(
                f"{node['id']}: llama.cpp acceptance checkout must be clean"
            )
        if expected_repository is None:
            expected_repository = sanitized_repository
        elif sanitized_repository != expected_repository:
            raise PublicEvidenceError(
                "llama.cpp checkout evidence changed within one run"
            )
    assert expected_tool_digests is not None and expected_repository is not None
    return expected_tool_digests, expected_repository


def _validate_host_and_modelbake(
    manifest: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    observed = manifest["runner"].get("observed")
    if (
        not isinstance(observed, dict)
        or observed.get("schema") != "modelbake.runner.v1"
    ):
        raise PublicEvidenceError("observed host fingerprint is missing")
    host = {
        key: _require_public_text(observed.get(key), f"observed host {key}")
        for key in (
            "os",
            "os_release",
            "machine",
            "python_implementation",
            "python_version",
            "byteorder",
        )
    }
    extra = observed.get("extra")
    modelbake = extra.get("modelbake") if isinstance(extra, dict) else None
    if not isinstance(modelbake, dict) or set(modelbake) != {
        "version",
        "python_source_digest",
    }:
        raise PublicEvidenceError("observed ModelBake code fingerprint is missing")
    code = {
        "version": _require_public_text(
            modelbake.get("version"), "observed ModelBake version"
        ),
        "python_source_digest": _require_digest(
            modelbake.get("python_source_digest"),
            "observed ModelBake Python source digest",
        ),
    }
    return host, code


def _strict_manifest(
    path: Path, artifact_root: Path, expected_states: Counter[str], label: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    initial_record = _file_record(path, f"{label} manifest")
    try:
        manifest = load_manifest(path)
        verification = verify_manifest(path, allowed_roots=(artifact_root,))
    except ManifestError as exc:
        raise PublicEvidenceError(f"{label} manifest is invalid: {exc}") from exc
    final_record = _file_record(path, f"{label} manifest")
    _same_file_record(initial_record, final_record, f"{label} manifest")
    if not verification.ok or verification.checked != 6:
        details = "; ".join(verification.failures) or "wrong artifact count"
        raise PublicEvidenceError(
            f"{label} manifest artifact verification failed: {details}"
        )
    if manifest.get("status") != "succeeded" or len(manifest["nodes"]) != 6:
        raise PublicEvidenceError(f"{label} manifest must be a successful six-node run")
    kinds = Counter(str(node.get("kind")) for node in manifest["nodes"])
    if kinds != _EXPECTED_KINDS:
        raise PublicEvidenceError(
            f"{label} manifest has the wrong node kinds: {dict(kinds)}"
        )
    states = Counter(str(node["state"]) for node in manifest["nodes"])
    if states != expected_states:
        raise PublicEvidenceError(
            f"{label} manifest has the wrong states: {dict(states)}"
        )
    if any(len(node["output_digests"]) != 1 for node in manifest["nodes"]):
        raise PublicEvidenceError(
            f"{label} manifest must record exactly one output per node"
        )
    return (
        manifest,
        final_record,
        {
            "checked": verification.checked,
            "failures": len(verification.failures),
        },
    )


def _safe_wheel_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members or len(members) > _MAX_WHEEL_MEMBERS:
        raise PublicEvidenceError("wheel member count is invalid")
    seen: set[str] = set()
    for member in members:
        path = PurePosixPath(member.filename)
        normalized_name = path.as_posix() + ("/" if member.is_dir() else "")
        if (
            member.filename in seen
            or member.filename != normalized_name
            or "\\" in member.filename
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or any(part in {"", "."} for part in path.parts)
            or member.flag_bits & 0x1
        ):
            raise PublicEvidenceError(
                f"wheel contains unsafe member: {member.filename!r}"
            )
        seen.add(member.filename)
        unix_mode = member.external_attr >> 16
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise PublicEvidenceError(
                f"wheel contains symlink member: {member.filename!r}"
            )
        file_type = stat.S_IFMT(unix_mode)
        if file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
            raise PublicEvidenceError(
                f"wheel contains special member: {member.filename!r}"
            )
    return members


def _wheel_code_record(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = _safe_wheel_members(archive)
            metadata_members = [
                member
                for member in members
                if len(PurePosixPath(member.filename).parts) == 2
                and PurePosixPath(member.filename).parts[0].endswith(".dist-info")
                and PurePosixPath(member.filename).parts[1] == "METADATA"
            ]
            if len(metadata_members) != 1:
                raise PublicEvidenceError(
                    "wheel must contain exactly one dist-info/METADATA"
                )
            metadata_member = metadata_members[0]
            if metadata_member.file_size > _MAX_METADATA_BYTES:
                raise PublicEvidenceError("wheel METADATA exceeds the safety limit")
            metadata = BytesParser().parsebytes(archive.read(metadata_member))
            names = metadata.get_all("Name", [])
            versions = metadata.get_all("Version", [])
            if names != ["modelbake-ai"]:
                raise PublicEvidenceError(
                    "wheel METADATA distribution is not modelbake-ai"
                )
            version = versions[0] if len(versions) == 1 else None
            if (
                not isinstance(version, str)
                or _SAFE_PUBLIC_TEXT_RE.fullmatch(version) is None
            ):
                raise PublicEvidenceError("wheel METADATA version is invalid")

            sources = [
                member
                for member in members
                if not member.is_dir()
                and PurePosixPath(member.filename).parts[0] == "modelbake"
                and PurePosixPath(member.filename).suffix == ".py"
            ]
            if not sources:
                raise PublicEvidenceError("wheel contains no modelbake Python sources")
            total_size = sum(member.file_size for member in sources)
            if total_size > _MAX_PYTHON_SOURCE_BYTES:
                raise PublicEvidenceError(
                    "wheel Python sources exceed the safety limit"
                )
            with tempfile.TemporaryDirectory(prefix="modelbake-wheel-") as temporary:
                package_root = Path(temporary) / "modelbake"
                package_root.mkdir(mode=0o700)
                destinations: set[str] = set()
                for member in sources:
                    relative = PurePosixPath(member.filename).relative_to("modelbake")
                    destination_key = relative.as_posix().casefold()
                    if destination_key in destinations:
                        raise PublicEvidenceError(
                            f"wheel Python sources collide: {member.filename}"
                        )
                    destinations.add(destination_key)
                    destination = package_root.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    data = archive.read(member)
                    if len(data) != member.file_size:
                        raise PublicEvidenceError(
                            f"wheel member size changed: {member.filename}"
                        )
                    destination.write_bytes(data)
                    os.chmod(destination, 0o600)
                source_digest = python_source_digest(package_root)
    except (OSError, zipfile.BadZipFile, RuntimeError, HashingError) as exc:
        raise PublicEvidenceError(f"cannot inspect wheel: {exc}") from exc
    return {"version": version, "python_source_digest": source_digest}


def _validate_lineage(
    cold: dict[str, Any],
    warm: dict[str, Any],
    cold_sizes: dict[str, dict[str, int]],
    warm_sizes: dict[str, dict[str, int]],
) -> None:
    for field in ("project", "recipe_digest", "source_digest"):
        if cold[field] != warm[field]:
            raise PublicEvidenceError(f"cold and warm manifests have different {field}")
    if cold["run_id"] == warm["run_id"]:
        raise PublicEvidenceError("cold and warm runs must have different run IDs")
    cold_nodes = _nodes_by_id(cold)
    warm_nodes = _nodes_by_id(warm)
    if set(cold_nodes) != set(warm_nodes):
        raise PublicEvidenceError("cold and warm manifests have different node IDs")
    for node_id, cold_node in cold_nodes.items():
        warm_node = warm_nodes[node_id]
        if cold_node.get("kind") != warm_node.get("kind"):
            raise PublicEvidenceError(f"{node_id}: node kind changed between runs")
        if cold_node["output_digests"] != warm_node["output_digests"]:
            raise PublicEvidenceError(
                f"{node_id}: output digest map changed between runs"
            )
        if cold_sizes[node_id] != warm_sizes[node_id]:
            raise PublicEvidenceError(
                f"{node_id}: output size map changed between runs"
            )
        if cold_node.get("cache_key") != warm_node.get("cache_key"):
            raise PublicEvidenceError(f"{node_id}: cache key changed between runs")
        if warm_node["observations"].get("cached_state") != cold_node["state"]:
            raise PublicEvidenceError(f"{node_id}: warm cache provenance is invalid")


def _public_nodes(
    manifest: dict[str, Any], sizes: dict[str, dict[str, int]]
) -> list[dict[str, Any]]:
    return [
        {
            "id": node["id"],
            "kind": node["kind"],
            "state": node["state"],
            "cache_key": _require_digest(
                node.get("cache_key"), f"{node['id']} cache key"
            ),
            "output_digests": dict(sorted(node["output_digests"].items())),
            "output_sizes_bytes": dict(sorted(sizes[node["id"]].items())),
        }
        for node in manifest["nodes"]
    ]


def _public_commands(
    manifest: dict[str, Any], tool_digests: dict[str, str]
) -> dict[str, Any]:
    """Expose the validated argv shape without publishing workstation paths."""

    records: list[dict[str, Any]] = []
    for node in manifest["nodes"]:
        kind = node["kind"]
        if kind == "source":
            continue
        input_digests = sorted(node["input_digests"].values())
        output_digests = sorted(node["output_digests"].values())
        if len(input_digests) != 1 or len(output_digests) != 1:
            raise PublicEvidenceError(
                f"{node['id']}: public argv requires one input and one output digest"
            )
        input_token = f"<input:{_require_digest(input_digests[0], 'command input digest')}>"
        output_token = f"<output:{_require_digest(output_digests[0], 'command output digest')}>"
        private_argv = node["command"]
        if kind == "convert":
            display_argv = [
                "python",
                "convert_hf_to_gguf.py",
                input_token,
                "--outfile",
                output_token,
                "--outtype",
                "f16",
            ]
            tool_digest = tool_digests["convert"]
        elif kind == "quantize":
            display_argv = [
                "llama-quantize",
                input_token,
                output_token,
                private_argv[3],
            ]
            tool_digest = tool_digests["quantize"]
        elif kind == "smoke":
            display_argv = [
                "llama-cli",
                "-m",
                input_token,
                "-p",
                private_argv[4],
                "-n",
                private_argv[6],
                "--seed",
                "0",
                "--single-turn",
            ]
            tool_digest = tool_digests["runner"]
        else:  # validated earlier; keep this function fail-closed in isolation
            raise PublicEvidenceError(f"{node['id']}: unsupported public argv kind")
        records.append(
            {
                "node_id": node["id"],
                "kind": kind,
                "display_argv": display_argv,
                "input_digest": input_digests[0],
                "output_digest": output_digests[0],
                "tool_digest": tool_digest,
            }
        )
    return {
        "path_policy": "Local paths replaced with digest-bound placeholders.",
        "nodes": records,
    }


def _assert_sanitized(value: Any, path: str = "$") -> None:
    forbidden_keys = {
        "command",
        "prompt",
        "stdout",
        "stderr",
        "log",
        "log_path",
        "path",
        "root",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if key in forbidden_keys:
                raise PublicEvidenceError(
                    f"internal error: forbidden public field at {path}.{key}"
                )
            _assert_sanitized(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_sanitized(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if value.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
            raise PublicEvidenceError(f"internal error: local path leaked at {path}")
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise PublicEvidenceError(
                f"internal error: multiline text leaked at {path}"
            )


def _baseline_acceptance_binding(
    *,
    baseline_store: str | os.PathLike[str],
    baseline_name: str,
    warm_manifest: Path,
    warm_file_record: dict[str, Any],
    warm_modelbake_digest: str,
    artifact_root: Path,
) -> dict[str, Any]:
    """Bind the warm candidate to the terminal receipt-backed CAS decision.

    The baseline module verifies the complete ledger and performs the live artifact
    check. This function then exposes only digest-addressed identifiers, with a
    second ledger read to detect a concurrent channel advance.
    """

    store = Path(baseline_store).absolute()
    try:
        store_info = store.lstat()
    except OSError as exc:
        raise PublicEvidenceError(f"cannot inspect baseline store: {exc}") from exc
    if not stat.S_ISDIR(store_info.st_mode):
        raise PublicEvidenceError(
            "baseline store must be an existing real directory, not a symlink"
        )

    try:
        ledger_before = verify_ledger(store, baseline_name)
        current = load_current_baseline(store, baseline_name)
    except BaselineError as exc:
        raise PublicEvidenceError(f"baseline ledger verification failed: {exc}") from exc

    decision = dict(current.decision)
    if decision.get("schema") != "modelbake.baseline.decision.v2":
        raise PublicEvidenceError(
            "current baseline must be a receipt-backed v2 decision; legacy v1 is forbidden"
        )
    if ledger_before.decisions < 2:
        raise PublicEvidenceError(
            "publisher acceptance requires a reviewed successor with a predecessor"
        )
    decision_digest = _require_digest(
        current.decision_digest, "baseline decision digest"
    )
    if decision.get("record_digest") != decision_digest:
        raise PublicEvidenceError("terminal baseline decision digest is inconsistent")
    candidate_digest = _require_digest(
        current.manifest_digest, "baseline candidate manifest digest"
    )
    if candidate_digest != warm_modelbake_digest:
        raise PublicEvidenceError(
            "warm candidate is not the exact current accepted manifest"
        )
    warm_bytes = _read_regular_file(
        warm_manifest, "warm manifest", maximum=_MAX_MANIFEST_BYTES
    )
    _same_file_record(
        warm_file_record,
        _file_record(warm_manifest, "warm manifest"),
        "warm manifest",
    )
    if warm_bytes != current.manifest_bytes:
        raise PublicEvidenceError(
            "warm candidate bytes do not match the stored current manifest"
        )

    review_digest = _require_digest(
        decision.get("review_digest"), "baseline review digest"
    )
    review_hex = review_digest.removeprefix("sha256:")
    if decision.get("review_record") != f"reviews/{review_hex}.json":
        raise PublicEvidenceError("terminal baseline review record is inconsistent")
    review_path = store / "reviews" / f"{review_hex}.json"
    review_bytes = _read_regular_file(
        review_path, "stored baseline review", maximum=_MAX_REVIEW_BYTES
    )
    try:
        review = json.loads(review_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicEvidenceError(f"stored baseline review is invalid JSON: {exc}") from exc
    review = _require_exact_keys(
        review,
        {
            "schema",
            "name",
            "project",
            "reviewed_at",
            "candidate_manifest_digest",
            "predecessor_decision_digest",
            "comparison_digest",
            "comparison",
            "initial",
            "verified_artifacts",
            "record_digest",
        },
        "stored baseline review",
    )
    if review_bytes != canonical_json_bytes(review) + b"\n":
        raise PublicEvidenceError("stored baseline review is not canonical JSON")
    if review.get("schema") != "modelbake.baseline.review.v1":
        raise PublicEvidenceError("stored baseline review schema is invalid")
    if review.get("name") != baseline_name or decision.get("name") != baseline_name:
        raise PublicEvidenceError("baseline channel binding is inconsistent")
    if review.get("initial") is not False:
        raise PublicEvidenceError(
            "publisher acceptance requires a non-initial compare-and-swap review"
        )
    predecessor_digest = _require_digest(
        review.get("predecessor_decision_digest"),
        "baseline predecessor decision digest",
    )
    comparison_digest = _require_digest(
        review.get("comparison_digest"), "baseline comparison digest"
    )
    if not isinstance(review.get("comparison"), dict):
        raise PublicEvidenceError("stored baseline comparison must be an object")
    if digest_json(review["comparison"]) != comparison_digest:
        raise PublicEvidenceError("stored baseline comparison digest is inconsistent")
    review_payload = dict(review)
    del review_payload["record_digest"]
    if review.get("record_digest") != review_digest or digest_json(
        review_payload
    ) != review_digest:
        raise PublicEvidenceError("stored baseline review digest is inconsistent")
    if (
        review.get("candidate_manifest_digest") != candidate_digest
        or decision.get("manifest_digest") != candidate_digest
        or decision.get("predecessor_decision_digest") != predecessor_digest
    ):
        raise PublicEvidenceError("baseline decision, review, and candidate do not agree")

    try:
        checked = check_baseline(
            warm_manifest,
            store,
            baseline_name,
            (artifact_root,),
        )
        ledger_after = verify_ledger(store, baseline_name)
    except BaselineError as exc:
        raise PublicEvidenceError(
            f"current baseline live check failed: {exc}"
        ) from exc
    if (
        checked.decision_digest != decision_digest
        or checked.manifest_digest != candidate_digest
        or checked.verified_artifacts != 6
        or review.get("verified_artifacts") != checked.verified_artifacts
        or ledger_after.current_decision_digest != decision_digest
        or ledger_after.current_manifest_digest != candidate_digest
        or ledger_after.decisions != ledger_before.decisions
    ):
        raise PublicEvidenceError(
            "baseline channel changed or its live artifact check was incomplete"
        )

    return {
        "kind": "publisher-generated-cas-acceptance",
        "channel": _require_public_text(baseline_name, "baseline channel"),
        "decision_digest": decision_digest,
        "review_digest": review_digest,
        "candidate_manifest_digest": candidate_digest,
        "predecessor_decision_digest": predecessor_digest,
        "comparison_digest": comparison_digest,
    }


def _validate_public_acceptance(
    value: Any, *, warm_modelbake_digest: str
) -> dict[str, Any]:
    acceptance = _require_exact_keys(
        value,
        {
            "kind",
            "channel",
            "decision_digest",
            "review_digest",
            "candidate_manifest_digest",
            "predecessor_decision_digest",
            "comparison_digest",
        },
        "acceptance",
    )
    if acceptance["kind"] != "publisher-generated-cas-acceptance":
        raise PublicEvidenceError("acceptance kind is invalid")
    _require_public_text(acceptance["channel"], "acceptance channel")
    for field in (
        "decision_digest",
        "review_digest",
        "candidate_manifest_digest",
        "predecessor_decision_digest",
        "comparison_digest",
    ):
        _require_digest(acceptance[field], f"acceptance {field.replace('_', ' ')}")
    if acceptance["candidate_manifest_digest"] != warm_modelbake_digest:
        raise PublicEvidenceError(
            "acceptance candidate digest does not match the warm manifest"
        )
    return acceptance


def _validate_public_node(
    value: Any,
    *,
    expected_state: str,
    label: str,
) -> dict[str, Any]:
    node = _require_exact_keys(
        value,
        {
            "id",
            "kind",
            "state",
            "cache_key",
            "output_digests",
            "output_sizes_bytes",
        },
        label,
    )
    _require_public_text(node["id"], f"{label} id")
    kind = _require_public_text(node["kind"], f"{label} kind")
    if kind not in _EXPECTED_KINDS:
        raise PublicEvidenceError(f"{label} has unsupported kind {kind!r}")
    if node["state"] != expected_state:
        raise PublicEvidenceError(f"{label} must have state {expected_state!r}")
    _require_digest(node["cache_key"], f"{label} cache key")
    digests = node["output_digests"]
    sizes = node["output_sizes_bytes"]
    if (
        not isinstance(digests, dict)
        or len(digests) != 1
        or not isinstance(sizes, dict)
        or set(sizes) != set(digests)
    ):
        raise PublicEvidenceError(
            f"{label} must contain one matching output digest and size"
        )
    if kind == "source":
        expected_output = "source"
    elif kind == "smoke":
        expected_output = "observation"
    else:
        expected_output = "artifact"
    if set(digests) != {expected_output}:
        raise PublicEvidenceError(
            f"{label} output name must be {expected_output!r}"
        )
    for name, digest in digests.items():
        _require_public_text(name, f"{label} output name")
        _require_digest(digest, f"{label} output digest")
        _require_size(sizes[name], f"{label} output size")
    return node


def _validate_public_run(
    value: Any,
    *,
    label: str,
    expected_states: dict[str, int],
    node_state: str | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    run = _require_exact_keys(
        value,
        {"run_id", "manifest", "states", "nodes", "digest_verification"},
        label,
    )
    _require_public_text(run["run_id"], f"{label} run ID")
    manifest = _require_exact_keys(
        run["manifest"],
        {"sha256", "modelbake_digest", "size_bytes"},
        f"{label} manifest",
    )
    _require_digest(manifest["sha256"], f"{label} manifest digest")
    _require_digest(
        manifest["modelbake_digest"], f"{label} ModelBake manifest digest"
    )
    _require_size(manifest["size_bytes"], f"{label} manifest size")
    if run["states"] != expected_states:
        raise PublicEvidenceError(f"{label} states are invalid")
    verification = _require_exact_keys(
        run["digest_verification"],
        {"checked", "failures"},
        f"{label} digest verification",
    )
    if (
        isinstance(verification["checked"], bool)
        or not isinstance(verification["checked"], int)
        or isinstance(verification["failures"], bool)
        or not isinstance(verification["failures"], int)
        or verification != {"checked": 6, "failures": 0}
    ):
        raise PublicEvidenceError(f"{label} digest verification is invalid")
    if not isinstance(run["nodes"], list) or len(run["nodes"]) != 6:
        raise PublicEvidenceError(f"{label} must contain six nodes")
    nodes: dict[str, dict[str, Any]] = {}
    kinds: Counter[str] = Counter()
    for index, value_node in enumerate(run["nodes"]):
        expected = node_state
        if expected is None:
            if not isinstance(value_node, dict):
                raise PublicEvidenceError(f"{label} nodes[{index}] must be an object")
            kind = value_node.get("kind")
            expected = "executed_on_runner" if kind == "smoke" else "produced"
        node = _validate_public_node(
            value_node, expected_state=expected, label=f"{label} nodes[{index}]"
        )
        node_id = node["id"]
        if node_id in nodes:
            raise PublicEvidenceError(f"{label} has duplicate node ID {node_id!r}")
        nodes[node_id] = node
        kinds[node["kind"]] += 1
    if kinds != _EXPECTED_KINDS:
        raise PublicEvidenceError(f"{label} node kinds are invalid: {dict(kinds)}")
    return run, nodes


def _validate_public_commands(
    value: Any,
    *,
    cold_nodes: dict[str, dict[str, Any]],
    tool_digests: dict[str, str],
) -> None:
    commands = _require_exact_keys(value, {"path_policy", "nodes"}, "commands")
    if commands["path_policy"] != "Local paths replaced with digest-bound placeholders.":
        raise PublicEvidenceError("public command path policy is invalid")
    records = commands["nodes"]
    if not isinstance(records, list) or len(records) != 5:
        raise PublicEvidenceError("public commands must contain five executed nodes")
    expected_ids = {
        node_id for node_id, node in cold_nodes.items() if node["kind"] != "source"
    }
    expected_parent = {
        "convert-f16-base": "source",
        "quantize-q4_0": "convert-f16-base",
        "quantize-q8_0": "convert-f16-base",
        "smoke-q4_0": "quantize-q4_0",
        "smoke-q8_0": "quantize-q8_0",
    }
    if expected_ids != set(expected_parent):
        raise PublicEvidenceError("public command nodes do not match the release recipe")
    seen: set[str] = set()
    for index, raw in enumerate(records):
        record = _require_exact_keys(
            raw,
            {
                "node_id",
                "kind",
                "display_argv",
                "input_digest",
                "output_digest",
                "tool_digest",
            },
            f"commands nodes[{index}]",
        )
        node_id = _require_public_text(record["node_id"], "command node ID")
        if node_id in seen or node_id not in expected_ids:
            raise PublicEvidenceError("public command node IDs are invalid")
        seen.add(node_id)
        node = cold_nodes[node_id]
        if record["kind"] != node["kind"]:
            raise PublicEvidenceError(f"{node_id}: public command kind is invalid")
        input_digest = _require_digest(record["input_digest"], f"{node_id} input digest")
        output_digest = _require_digest(record["output_digest"], f"{node_id} output digest")
        parent_outputs = cold_nodes[expected_parent[node_id]]["output_digests"].values()
        if input_digest not in parent_outputs:
            raise PublicEvidenceError(
                f"{node_id}: public command input does not match its recorded parent"
            )
        if output_digest not in node["output_digests"].values():
            raise PublicEvidenceError(f"{node_id}: public command output is invalid")
        argv = record["display_argv"]
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise PublicEvidenceError(f"{node_id}: displayed argv is invalid")
        input_token = f"<input:{input_digest}>"
        output_token = f"<output:{output_digest}>"
        kind = node["kind"]
        if kind == "convert":
            expected = [
                "python",
                "convert_hf_to_gguf.py",
                input_token,
                "--outfile",
                output_token,
                "--outtype",
                "f16",
            ]
            expected_tool = tool_digests["convert"]
        elif kind == "quantize":
            expected_quantization = {
                "quantize-q4_0": "Q4_0",
                "quantize-q8_0": "Q8_0",
            }.get(node_id)
            valid = (
                expected_quantization is not None
                and len(argv) == 4
                and argv[:3] == ["llama-quantize", input_token, output_token]
                and argv[3] == expected_quantization
            )
            if not valid:
                raise PublicEvidenceError(f"{node_id}: displayed quantize argv is invalid")
            expected = argv
            expected_tool = tool_digests["quantize"]
        elif kind == "smoke":
            valid = (
                len(argv) == 10
                and argv[:4] == ["llama-cli", "-m", input_token, "-p"]
                and _REDACTED_PROMPT_RE.fullmatch(argv[4]) is not None
                and argv[5] == "-n"
                and argv[6].isdigit()
                and int(argv[6]) > 0
                and argv[7:] == ["--seed", "0", "--single-turn"]
            )
            if not valid:
                raise PublicEvidenceError(f"{node_id}: displayed smoke argv is invalid")
            expected = argv
            expected_tool = tool_digests["runner"]
        else:
            raise PublicEvidenceError(f"{node_id}: unsupported public command kind")
        if argv != expected or record["tool_digest"] != expected_tool:
            raise PublicEvidenceError(f"{node_id}: displayed argv binding is invalid")
    if seen != expected_ids:
        raise PublicEvidenceError("public command coverage is incomplete")


def validate_public_evidence(
    payload: Any,
    *,
    wheel: str | os.PathLike[str],
    sdist: str | os.PathLike[str],
) -> None:
    """Strictly validate a public record against exact distribution bytes."""

    root = _require_exact_keys(
        payload,
        {
            "schema",
            "release",
            "acceptance",
            "lineage",
            "runner",
            "commands",
            "cold_run",
            "warm_run",
            "exclusions",
        },
        "public evidence",
    )
    if root["schema"] != _PUBLIC_SCHEMA:
        raise PublicEvidenceError(f"public evidence schema must be {_PUBLIC_SCHEMA}")
    if root["exclusions"] != list(_PUBLIC_EXCLUSIONS):
        raise PublicEvidenceError("public evidence exclusions are invalid")
    _assert_sanitized(root)

    wheel_path = Path(wheel)
    sdist_path = Path(sdist)
    wheel_record = _file_record(wheel_path, "wheel")
    sdist_record = _file_record(sdist_path, "sdist")
    wheel_code = _wheel_code_record(wheel_path)
    expected_release = {
        "distribution": "modelbake-ai",
        "version": wheel_code["version"],
        "python_source_digest": wheel_code["python_source_digest"],
        "wheel": {"filename": wheel_path.name, **wheel_record},
        "sdist": {"filename": sdist_path.name, **sdist_record},
    }
    if root["release"] != expected_release:
        raise PublicEvidenceError(
            "public evidence release files do not match the exact wheel and sdist"
        )

    lineage = _require_exact_keys(
        root["lineage"], {"recipe_digest", "source_digest", "node_ids"}, "lineage"
    )
    _require_digest(lineage["recipe_digest"], "lineage recipe digest")
    _require_digest(lineage["source_digest"], "lineage source digest")
    node_ids = lineage["node_ids"]
    if (
        not isinstance(node_ids, list)
        or len(node_ids) != 6
        or any(not isinstance(node_id, str) for node_id in node_ids)
        or len(set(node_ids)) != 6
    ):
        raise PublicEvidenceError("lineage must contain six unique node IDs")

    runner = _require_exact_keys(
        root["runner"],
        {
            "adapter",
            "declared_revision",
            "observed_revision",
            "revision_match",
            "tool_file_digests",
            "tool_source_worktree_digest",
            "tool_source_had_changes",
            "host",
        },
        "runner",
    )
    if runner["adapter"] != "llamacpp":
        raise PublicEvidenceError("runner adapter must be llamacpp")
    declared_revision = runner["declared_revision"]
    if (
        not isinstance(declared_revision, str)
        or _REVISION_RE.fullmatch(declared_revision) is None
        or runner["observed_revision"] != declared_revision
        or runner["revision_match"] is not True
    ):
        raise PublicEvidenceError("runner revision evidence is invalid")
    tool_digests = _require_exact_keys(
        runner["tool_file_digests"], set(_EXPECTED_TOOLS), "runner tool digests"
    )
    for name, digest in tool_digests.items():
        _require_digest(digest, f"runner {name} tool digest")
    _require_digest(
        runner["tool_source_worktree_digest"], "runner worktree digest"
    )
    if runner["tool_source_had_changes"] is not False:
        raise PublicEvidenceError("runner worktree must be recorded as clean")
    host_keys = {
        "os",
        "os_release",
        "machine",
        "python_implementation",
        "python_version",
        "byteorder",
    }
    host = _require_exact_keys(runner["host"], host_keys, "runner host")
    for name, value in host.items():
        _require_public_text(value, f"runner host {name}")

    cold, cold_nodes = _validate_public_run(
        root["cold_run"],
        label="cold run",
        expected_states={"produced": 4, "executed_on_runner": 2},
        node_state=None,
    )
    warm, warm_nodes = _validate_public_run(
        root["warm_run"],
        label="warm run",
        expected_states={"cache_hit": 6},
        node_state="cache_hit",
    )
    _validate_public_acceptance(
        root["acceptance"],
        warm_modelbake_digest=warm["manifest"]["modelbake_digest"],
    )
    if cold["run_id"] == warm["run_id"]:
        raise PublicEvidenceError("cold and warm public run IDs must differ")
    _validate_public_commands(
        root["commands"], cold_nodes=cold_nodes, tool_digests=tool_digests
    )
    if list(cold_nodes) != node_ids or list(warm_nodes) != node_ids:
        raise PublicEvidenceError("public run node IDs do not match lineage order")
    for node_id, cold_node in cold_nodes.items():
        warm_node = warm_nodes[node_id]
        for field in (
            "kind",
            "cache_key",
            "output_digests",
            "output_sizes_bytes",
        ):
            if cold_node[field] != warm_node[field]:
                raise PublicEvidenceError(
                    f"{node_id}: public cold and warm {field} differ"
                )


def _final_manifest_verification(
    path: Path,
    artifact_root: Path,
    expected_record: dict[str, Any],
    label: str,
) -> None:
    _same_file_record(
        expected_record,
        _file_record(path, f"{label} manifest"),
        f"{label} manifest",
    )
    try:
        verification = verify_manifest(path, allowed_roots=(artifact_root,))
    except ManifestError as exc:
        raise PublicEvidenceError(f"{label} manifest changed: {exc}") from exc
    if not verification.ok or verification.checked != 6:
        details = "; ".join(verification.failures) or "wrong artifact count"
        raise PublicEvidenceError(
            f"{label} artifacts changed during evidence generation: {details}"
        )


def generate_public_evidence(
    *,
    cold_manifest: str | os.PathLike[str],
    warm_manifest: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
    baseline_store: str | os.PathLike[str],
    baseline_name: str,
    wheel: str | os.PathLike[str],
    sdist: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate private evidence and return its deliberately narrow public form."""

    cold_path = Path(cold_manifest)
    warm_path = Path(warm_manifest)
    wheel_path = Path(wheel)
    sdist_path = Path(sdist)
    root = Path(artifact_root).resolve()
    try:
        if not root.is_dir():
            raise PublicEvidenceError("artifact root must be an existing directory")
    except OSError as exc:
        raise PublicEvidenceError(f"cannot inspect artifact root: {exc}") from exc
    if cold_path.resolve() == warm_path.resolve():
        raise PublicEvidenceError("cold and warm manifests must be different files")
    if wheel_path.suffix != ".whl":
        raise PublicEvidenceError("wheel must have a .whl filename")
    if not (sdist_path.name.endswith(".tar.gz") or sdist_path.suffix == ".zip"):
        raise PublicEvidenceError("sdist must have a .tar.gz or .zip filename")

    cold, cold_record, cold_verification = _strict_manifest(
        cold_path,
        root,
        Counter({"produced": 4, "executed_on_runner": 2}),
        "cold",
    )
    warm, warm_record, warm_verification = _strict_manifest(
        warm_path,
        root,
        Counter({"cache_hit": 6}),
        "warm",
    )
    cold_modelbake_digest = _modelbake_file_digest(
        cold_path, cold_record, "cold manifest"
    )
    warm_modelbake_digest = _modelbake_file_digest(
        warm_path, warm_record, "warm manifest"
    )
    cold_sizes = _output_sizes(cold, cold_path, root)
    warm_sizes = _output_sizes(warm, warm_path, root)
    _validate_lineage(cold, warm, cold_sizes, warm_sizes)

    cold_declared, cold_tools = _declared_runner(cold)
    warm_declared, warm_tools = _declared_runner(warm)
    if cold_declared != warm_declared or cold_tools != warm_tools:
        raise PublicEvidenceError("declared runner changed between cold and warm runs")
    live_toolchain = _observe_live_toolchain(cold_declared, cold_tools)
    _validate_commands(cold, cold_tools)
    _validate_commands(warm, warm_tools)
    cold_tool_digests, cold_repository = _validate_runner_observations(
        cold, cold_declared
    )
    warm_tool_digests, warm_repository = _validate_runner_observations(
        warm, warm_declared
    )
    if cold_tool_digests != warm_tool_digests or cold_repository != warm_repository:
        raise PublicEvidenceError("observed llama.cpp toolchain changed between runs")
    _validate_live_observations(cold, live_toolchain)
    _validate_live_observations(warm, live_toolchain)
    cold_host, cold_code = _validate_host_and_modelbake(cold)
    warm_host, warm_code = _validate_host_and_modelbake(warm)
    if cold_host != warm_host or cold_code != warm_code:
        raise PublicEvidenceError(
            "observed host or ModelBake code changed between runs"
        )
    _validate_gguf_artifacts(cold, cold_path, root)
    _validate_gguf_artifacts(warm, warm_path, root)
    _validate_smoke_artifacts(cold, cold_path, root)
    _validate_smoke_artifacts(warm, warm_path, root)
    acceptance_binding = _baseline_acceptance_binding(
        baseline_store=baseline_store,
        baseline_name=baseline_name,
        warm_manifest=warm_path,
        warm_file_record=warm_record,
        warm_modelbake_digest=warm_modelbake_digest,
        artifact_root=root,
    )

    initial_wheel_record = _file_record(wheel_path, "wheel")
    wheel_code = _wheel_code_record(wheel_path)
    final_wheel_record = _file_record(wheel_path, "wheel")
    _same_file_record(initial_wheel_record, final_wheel_record, "wheel")
    sdist_record = _file_record(sdist_path, "sdist")
    if wheel_code != cold_code:
        raise PublicEvidenceError(
            "wheel version or Python source digest does not match both runs"
        )
    _final_manifest_verification(cold_path, root, cold_record, "cold")
    _final_manifest_verification(warm_path, root, warm_record, "warm")
    if _observe_live_toolchain(cold_declared, cold_tools) != live_toolchain:
        raise PublicEvidenceError(
            "live llama.cpp toolchain changed during evidence generation"
        )
    if (
        _baseline_acceptance_binding(
            baseline_store=baseline_store,
            baseline_name=baseline_name,
            warm_manifest=warm_path,
            warm_file_record=warm_record,
            warm_modelbake_digest=warm_modelbake_digest,
            artifact_root=root,
        )
        != acceptance_binding
    ):
        raise PublicEvidenceError(
            "baseline acceptance changed during evidence generation"
        )

    wheel_filename = wheel_path.name
    sdist_filename = sdist_path.name
    if any(
        separator in wheel_filename + sdist_filename
        for separator in ("/", "\\", "\n", "\r")
    ):
        raise PublicEvidenceError("package filename is unsafe")

    revision = str(cold_declared["tools"]["revision"])
    payload: dict[str, Any] = {
        "schema": _PUBLIC_SCHEMA,
        "release": {
            "distribution": "modelbake-ai",
            "version": wheel_code["version"],
            "python_source_digest": wheel_code["python_source_digest"],
            "wheel": {"filename": wheel_filename, **final_wheel_record},
            "sdist": {"filename": sdist_filename, **sdist_record},
        },
        "acceptance": acceptance_binding,
        "lineage": {
            "recipe_digest": _require_digest(cold["recipe_digest"], "recipe digest"),
            "source_digest": _require_digest(cold["source_digest"], "source digest"),
            "node_ids": [node["id"] for node in cold["nodes"]],
        },
        "runner": {
            "adapter": "llamacpp",
            "declared_revision": revision,
            "observed_revision": revision,
            "revision_match": True,
            "tool_file_digests": cold_tool_digests,
            "tool_source_worktree_digest": cold_repository["worktree_digest"],
            "tool_source_had_changes": cold_repository["tracked_or_untracked_changes"],
            "host": cold_host,
        },
        "commands": _public_commands(cold, cold_tool_digests),
        "cold_run": {
            "run_id": cold["run_id"],
            "manifest": {
                **cold_record,
                "modelbake_digest": cold_modelbake_digest,
            },
            "states": {"produced": 4, "executed_on_runner": 2},
            "nodes": _public_nodes(cold, cold_sizes),
            "digest_verification": cold_verification,
        },
        "warm_run": {
            "run_id": warm["run_id"],
            "manifest": {
                **warm_record,
                "modelbake_digest": warm_modelbake_digest,
            },
            "states": {"cache_hit": 6},
            "nodes": _public_nodes(warm, warm_sizes),
            "digest_verification": warm_verification,
        },
        "exclusions": list(_PUBLIC_EXCLUSIONS),
    }
    validate_public_evidence(payload, wheel=wheel_path, sdist=sdist_path)
    return payload


def write_private_json(path: str | os.PathLike[str], payload: dict[str, Any]) -> Path:
    """Atomically replace one output file with owner-only canonical JSON."""

    destination = Path(path)
    parent = destination.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    if not parent.is_dir():
        raise PublicEvidenceError("output parent is not a directory")
    data = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
        + b"\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a sanitized public record, or validate one against exact "
            "ModelBake release archives."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cold-manifest")
    mode.add_argument(
        "--validate-public",
        metavar="PATH",
        help="validate an existing public record instead of generating one",
    )
    parser.add_argument("--warm-manifest")
    parser.add_argument("--artifact-root")
    parser.add_argument(
        "--baseline-store",
        help="owner-private baseline store containing the current accepted candidate",
    )
    parser.add_argument(
        "--baseline-name",
        help="baseline channel whose terminal decision accepted the warm candidate",
    )
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--sdist", required=True)
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.validate_public is not None:
            incompatible = {
                "--warm-manifest": arguments.warm_manifest,
                "--artifact-root": arguments.artifact_root,
                "--baseline-store": arguments.baseline_store,
                "--baseline-name": arguments.baseline_name,
                "--output": arguments.output,
            }
            supplied = [name for name, value in incompatible.items() if value is not None]
            if supplied:
                parser.error(
                    "--validate-public cannot be combined with " + ", ".join(supplied)
                )
            path = Path(arguments.validate_public)
            payload = json.loads(path.read_text(encoding="utf-8"))
            validate_public_evidence(
                payload, wheel=arguments.wheel, sdist=arguments.sdist
            )
            print(f"validated {path}")
            return 0
        missing = [
            name
            for name, value in (
                ("--warm-manifest", arguments.warm_manifest),
                ("--artifact-root", arguments.artifact_root),
                ("--baseline-store", arguments.baseline_store),
                ("--baseline-name", arguments.baseline_name),
                ("--output", arguments.output),
            )
            if value is None
        ]
        if missing:
            parser.error("generation requires " + ", ".join(missing))
        payload = generate_public_evidence(
            cold_manifest=arguments.cold_manifest,
            warm_manifest=arguments.warm_manifest,
            artifact_root=arguments.artifact_root,
            baseline_store=arguments.baseline_store,
            baseline_name=arguments.baseline_name,
            wheel=arguments.wheel,
            sdist=arguments.sdist,
        )
        destination = write_private_json(arguments.output, payload)
    except (
        PublicEvidenceError,
        ManifestError,
        HashingError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"public evidence rejected: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
