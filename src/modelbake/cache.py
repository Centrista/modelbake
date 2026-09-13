"""Content-addressed, verifying local artifact cache."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .hashing import HashingError, canonical_json_bytes, tree_digest

_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CACHEABLE_STATES = frozenset({"produced", "fixture_observed", "executed_on_runner"})


class CacheError(RuntimeError):
    pass


class CacheCorruptionError(CacheError):
    pass


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    outputs: Mapping[str, Path]
    output_digests: Mapping[str, str]
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CacheCommitResult:
    """The authoritative entry and whether this caller won its atomic commit."""

    entry: CacheEntry
    created: bool


class LocalCache:
    """Store immutable node outputs and verify every read against its manifest."""

    @staticmethod
    def _require_real_directory(path: Path, label: str) -> os.stat_result:
        """Validate a directory entry with lstat so symlinks are never accepted."""

        try:
            info = path.lstat()
        except OSError as exc:
            raise CacheError(f"cannot inspect {label} at {path}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise CacheError(f"{label} is not a real directory: {path}")
        return info

    @staticmethod
    def _make_created_directory_private(path: Path, label: str) -> None:
        """Harden a newly created directory through its verified descriptor."""

        expected = LocalCache._require_real_directory(path, label)
        if os.name != "posix":  # pragma: no cover - Windows mode semantics
            os.chmod(path, 0o700)
            current = LocalCache._require_real_directory(path, label)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise CacheError(f"{label} changed while being created: {path}")
            return
        flags = os.O_RDONLY
        for optional_flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
            flags |= getattr(os, optional_flag, 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (expected.st_dev, expected.st_ino):
                raise CacheError(f"{label} changed while being created: {path}")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o700)
            elif os.name == "posix":  # pragma: no cover - unusual POSIX runtime
                raise CacheError(f"cannot make {label} private: fchmod unavailable")
            current = LocalCache._require_real_directory(path, label)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise CacheError(f"{label} changed while being created: {path}")
        except OSError as exc:
            raise CacheError(f"cannot secure {label} at {path}: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()
        self.entries_dir = self.root / "entries"
        try:
            try:
                self.root.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                self._require_real_directory(self.root, "cache root")
            else:
                self._make_created_directory_private(self.root, "cache root")

            try:
                self.entries_dir.mkdir(mode=0o700)
            except FileExistsError:
                self._require_real_directory(self.entries_dir, "cache entries path")
            else:
                self._make_created_directory_private(
                    self.entries_dir, "cache entries path"
                )
        except CacheError:
            raise
        except OSError as exc:
            raise CacheError(f"cannot initialize private cache at {self.root}: {exc}") from exc

    @staticmethod
    def _make_private(path: Path) -> None:
        """Strip group/other access without following cached symlinks."""

        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return
            if stat.S_ISDIR(info.st_mode):
                os.chmod(path, 0o700)
                with os.scandir(path) as children:
                    for child in children:
                        LocalCache._make_private(Path(child.path))
                return
            if stat.S_ISREG(info.st_mode):
                owner_execute = stat.S_IMODE(info.st_mode) & stat.S_IXUSR
                os.chmod(path, 0o600 | owner_execute)
                return
            raise CacheError(f"special filesystem objects are not cacheable: {path}")
        except OSError as exc:
            raise CacheError(f"cannot make cached output private: {path}: {exc}") from exc

    @staticmethod
    def _digest_hex(key: str) -> str:
        match = _DIGEST_RE.fullmatch(key)
        if not match:
            raise CacheError(f"invalid cache key: {key!r}")
        return match.group(1)

    def entry_path(self, key: str) -> Path:
        self._require_real_directory(self.entries_dir, "cache entries path")
        return self.entries_dir / self._digest_hex(key)

    def contains(self, key: str) -> bool:
        return (self.entry_path(key) / "manifest.json").is_file()

    def load(self, key: str) -> CacheEntry | None:
        entry = self.entry_path(key)
        manifest_path = entry / "manifest.json"
        try:
            entry_info = entry.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CacheCorruptionError(f"cannot inspect cache entry for {key}: {exc}") from exc
        if not stat.S_ISDIR(entry_info.st_mode):
            raise CacheCorruptionError(f"cache entry is not a directory: {key}")
        try:
            manifest_info = manifest_path.lstat()
        except FileNotFoundError as exc:
            raise CacheCorruptionError(f"cache entry has no manifest: {key}") from exc
        except OSError as exc:
            raise CacheCorruptionError(f"cannot inspect cache manifest for {key}: {exc}") from exc
        if not stat.S_ISREG(manifest_info.st_mode):
            raise CacheCorruptionError(f"cache manifest is not a regular file for {key}")
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                parse_constant=_reject_nonfinite_json,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise CacheCorruptionError(f"cannot read cache manifest for {key}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise CacheCorruptionError(f"cache manifest is not an object for {key}")
        if manifest.get("schema") != "modelbake.cache.v1" or manifest.get("key") != key:
            raise CacheCorruptionError(f"cache manifest identity mismatch for {key}")
        records = manifest.get("outputs")
        if not isinstance(records, dict):
            raise CacheCorruptionError(f"cache manifest outputs are invalid for {key}")

        outputs: dict[str, Path] = {}
        output_digests: dict[str, str] = {}
        for name, record in sorted(records.items()):
            if (
                not isinstance(name, str)
                or not _OUTPUT_NAME_RE.fullmatch(name)
                or not isinstance(record, dict)
            ):
                raise CacheCorruptionError(f"invalid cached output record {name!r} for {key}")
            relative = record.get("path")
            expected = record.get("digest")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise CacheCorruptionError(f"incomplete cached output record {name!r} for {key}")
            relative_path = PurePosixPath(relative)
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative_path != PurePosixPath("outputs") / name
            ):
                raise CacheCorruptionError(f"cached output path is invalid: {name!r} for {key}")
            candidate = entry / relative
            try:
                candidate.relative_to(entry)
            except ValueError as exc:
                raise CacheCorruptionError(f"cached output escapes entry: {name!r}") from exc
            try:
                if not candidate.exists() and not candidate.is_symlink():
                    raise CacheCorruptionError(f"cached output is missing: {name!r} for {key}")
                observed = tree_digest(candidate)
            except CacheCorruptionError:
                raise
            except (OSError, HashingError) as exc:
                raise CacheCorruptionError(
                    f"cannot verify cached output {name!r} for {key}: {exc}"
                ) from exc
            if observed != expected:
                raise CacheCorruptionError(
                    f"cached output digest mismatch for {name!r}: expected {expected}, observed {observed}"
                )
            outputs[name] = candidate
            output_digests[name] = observed
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise CacheCorruptionError(f"cache metadata is invalid for {key}")
        state = metadata.get("state")
        command = metadata.get("command")
        observations = metadata.get("observations")
        if state not in _CACHEABLE_STATES:
            raise CacheCorruptionError(f"cache metadata state is invalid for {key}")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise CacheCorruptionError(f"cache metadata command is invalid for {key}")
        if not isinstance(observations, dict) or not all(
            isinstance(name, str) for name in observations
        ):
            raise CacheCorruptionError(f"cache metadata observations are invalid for {key}")
        if not records:
            raise CacheCorruptionError(f"cache entry has no outputs: {key}")
        return CacheEntry(key, outputs, output_digests, metadata)

    def commit(
        self,
        key: str,
        outputs: Mapping[str, Path],
        metadata: Mapping[str, Any] | None = None,
        *,
        allowed_root: Path,
    ) -> CacheCommitResult:
        """Atomically commit outputs after copying and hashing observed bytes."""

        existing = self.load(key)
        if existing is not None:
            return CacheCommitResult(existing, created=False)
        self._digest_hex(key)
        if not outputs:
            raise CacheError("a successful node must declare at least one output")
        root = allowed_root.resolve(strict=True)
        target = self.entry_path(key)
        temporary = Path(tempfile.mkdtemp(prefix=".commit-", dir=self.entries_dir))
        try:
            output_dir = temporary / "outputs"
            output_dir.mkdir(mode=0o700)
            records: dict[str, dict[str, str]] = {}
            for name, source_value in sorted(outputs.items()):
                if not _OUTPUT_NAME_RE.fullmatch(name):
                    raise CacheError(f"invalid output name: {name!r}")
                source = Path(source_value)
                if not source.exists() and not source.is_symlink():
                    raise CacheError(f"declared output does not exist: {source}")
                if source.is_symlink():
                    raise CacheError(
                        f"top-level outputs cannot be symlinks; declare their containing directory: {source}"
                    )
                resolved = source.resolve(strict=True)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise CacheError(f"declared output escapes adapter output directory: {source}") from exc
                destination = output_dir / name
                try:
                    tree_digest(source)
                except (OSError, HashingError) as exc:
                    raise CacheError(
                        f"declared output cannot be cached safely: {source}: {exc}"
                    ) from exc
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=True)
                elif source.is_file():
                    shutil.copy2(source, destination, follow_symlinks=False)
                else:
                    raise CacheError(f"declared output is not a regular file or directory: {source}")
                self._make_private(destination)
                records[name] = {
                    "path": f"outputs/{name}",
                    "digest": tree_digest(destination),
                }

            manifest = {
                "schema": "modelbake.cache.v1",
                "key": key,
                "outputs": records,
                "metadata": dict(metadata or {}),
            }
            manifest_path = temporary / "manifest.json"
            manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
            os.chmod(manifest_path, 0o600)
            self._make_private(output_dir)
            os.chmod(temporary, 0o700)
            created = True
            try:
                os.rename(temporary, target)
            except OSError:
                if not target.exists():
                    raise
                created = False
                shutil.rmtree(temporary)
            loaded = self.load(key)
            if loaded is None:  # pragma: no cover - defensive after atomic rename
                raise CacheCorruptionError(f"atomic cache commit disappeared: {key}")
            return CacheCommitResult(loaded, created=created)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
