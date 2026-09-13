"""Deterministic hashing helpers for build inputs and observed filesystem trees."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import stat
import sys
from pathlib import Path
from typing import Any, BinaryIO


class HashingError(ValueError):
    """Raised when a value or filesystem object cannot be hashed safely."""


def _validate_json(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HashingError(f"non-finite float at {path}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HashingError(f"non-string object key at {path}: {key!r}")
            _validate_json(item, f"{path}.{key}")
        return
    raise HashingError(f"unsupported canonical JSON value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes for JSON-compatible data.

    Unknown Python objects and non-finite floats are rejected instead of being
    converted through ``repr`` or another process-dependent representation.
    Tuples are intentionally encoded as JSON arrays.
    """

    _validate_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _tagged_digest(tag: bytes, chunks: list[bytes] | tuple[bytes, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(tag)
    for chunk in chunks:
        digest.update(len(chunk).to_bytes(8, "big"))
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def digest_bytes(data: bytes) -> str:
    return _tagged_digest(b"modelbake-bytes-v1", (data,))


def digest_json(value: Any) -> str:
    return _tagged_digest(b"modelbake-json-v1", (canonical_json_bytes(value),))


def python_source_digest(root: str | os.PathLike[str]) -> str:
    """Digest every Python source file below a package root, excluding bytecode."""

    package_root = Path(root)
    try:
        info = package_root.lstat()
    except OSError as exc:
        raise HashingError(f"cannot inspect Python source root: {package_root}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise HashingError(f"Python source root is not a real directory: {package_root}")
    files: dict[str, str] = {}
    for path in sorted(package_root.rglob("*.py"), key=lambda item: item.as_posix()):
        try:
            source_info = path.lstat()
        except OSError as exc:
            raise HashingError(f"cannot inspect Python source: {path}: {exc}") from exc
        if not stat.S_ISREG(source_info.st_mode):
            raise HashingError(f"Python source is not a regular file: {path}")
        files[path.relative_to(package_root).as_posix()] = (
            "sha256:" + _file_digest(path, source_info).hex()
        )
    if not files:
        raise HashingError(f"Python source root contains no .py files: {package_root}")
    return digest_json({"schema": "modelbake.python-source.v1", "files": files})


def _stream_digest(stream: BinaryIO) -> bytes:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.digest()


def _file_digest(path: Path, before: os.stat_result) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HashingError(f"cannot open regular file safely: {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise HashingError(f"filesystem object changed while hashing: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise HashingError(f"filesystem object changed while hashing: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content_digest = _stream_digest(stream)
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise HashingError(f"file changed while hashing: {path}")
        return content_digest
    finally:
        os.close(descriptor)


def _tree_node_digest(path: Path, relative: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HashingError(f"cannot stat filesystem object: {path}: {exc}") from exc

    mode = stat.S_IMODE(info.st_mode).to_bytes(4, "big")
    name = relative.encode("utf-8", "surrogateescape")
    if stat.S_ISREG(info.st_mode):
        payload = _file_digest(path, info)
        kind = b"file"
    elif stat.S_ISLNK(info.st_mode):
        try:
            payload = os.readlink(path).encode("utf-8", "surrogateescape")
        except OSError as exc:
            raise HashingError(f"cannot read symlink: {path}: {exc}") from exc
        kind = b"symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = b"directory"
        children: list[bytes] = []
        try:
            entries = sorted(os.scandir(path), key=lambda entry: os.fsencode(entry.name))
        except OSError as exc:
            raise HashingError(f"cannot list directory: {path}: {exc}") from exc
        for entry in entries:
            child_relative = entry.name if not relative else f"{relative}/{entry.name}"
            children.append(_tree_node_digest(Path(entry.path), child_relative))
        payload = b"".join(len(child).to_bytes(8, "big") + child for child in children)
    else:
        raise HashingError(f"special filesystem objects are not cacheable: {path}")

    digest = hashlib.sha256()
    for part in (b"modelbake-tree-node-v1", kind, name, mode, payload):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.digest()


def tree_digest(path: str | os.PathLike[str]) -> str:
    """Hash a file, directory, or symlink without following symlinks.

    Relative names, object kinds, permission bits, symlink targets, and regular
    file bytes are covered. Sockets, devices, and FIFOs are rejected.
    """

    target = Path(path)
    if not target.exists() and not target.is_symlink():
        raise HashingError(f"path does not exist: {target}")
    digest = _tree_node_digest(target, "")
    return _tagged_digest(b"modelbake-tree-v1", (digest,))


def runner_fingerprint(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe the local execution platform used in node cache keys."""

    fingerprint: dict[str, Any] = {
        "schema": "modelbake.runner.v1",
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "byteorder": sys.byteorder,
    }
    if extra:
        _validate_json(extra, "$.extra")
        fingerprint["extra"] = extra
    return fingerprint


def runner_digest(extra: dict[str, Any] | None = None) -> str:
    return digest_json(runner_fingerprint(extra))
