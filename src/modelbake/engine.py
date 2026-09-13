"""Resumable execution of validated ModelBake plans."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .adapters.base import Adapter, AdapterContext, DependencyOutput
from .cache import CacheCorruptionError, LocalCache
from .graph import topological_sort
from .hashing import (
    canonical_json_bytes,
    digest_json,
    python_source_digest,
    runner_fingerprint,
    tree_digest,
)
from .manifest import ManifestError, load_manifest
from .types import BuildPlan, BuildResult, NodeResult, NodeSpec, NodeState

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_LOCK_NAME = ".modelbake-run.lock"
_RUN_MARKER_NAME = ".modelbake-run.json"
_RUN_MARKER_SCHEMA = "modelbake.run-root.v1"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _private_directory(path: Path, *, parents: bool = False) -> None:
    """Create a product-owned directory and remove group/other access on POSIX."""

    path.mkdir(parents=parents, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def _require_real_directory(path: Path, label: str) -> os.stat_result:
    """Inspect a directory without accepting a symlink or special object."""

    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect {label} at {path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} is not a real directory: {path}")
    return info


def _make_verified_directory_private(
    path: Path, expected: os.stat_result, label: str
) -> None:
    """Harden a directory only after its identity and ownership were established."""

    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        expected_identity = (expected.st_dev, expected.st_ino)
        if not stat.S_ISDIR(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != expected_identity:
            raise ValueError(f"{label} changed while being secured: {path}")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o700)
        else:  # pragma: no cover - unusual non-POSIX runtime
            os.chmod(path, 0o700)
        current = _require_real_directory(path, label)
        if (current.st_dev, current.st_ino) != expected_identity:
            raise ValueError(f"{label} changed while being secured: {path}")
    except OSError as exc:
        raise ValueError(f"cannot secure {label} at {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_tree(path: Path) -> None:
    """Make generated work content owner-only without following symlinks."""

    if os.name != "posix" or not path.exists():
        return
    for item in sorted(path.rglob("*"), reverse=True):
        if item.is_symlink():
            continue
        item.chmod(0o700 if item.is_dir() else 0o600)
    path.chmod(0o700)


def _absolute_run_directory(path: str | os.PathLike[str]) -> Path:
    """Normalize a run path while allowing an OS root-level directory alias.

    macOS exposes temporary directories through ``/var`` even though ``/var`` is
    a root-level alias for ``/private/var``. Only that first ancestor position is
    canonicalized; a symlink supplied as the run root or a deeper parent remains
    visible to strict component validation and is rejected.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if absolute.is_absolute() and len(parts) > 2:
        first_ancestor = Path(absolute.anchor) / parts[1]
        try:
            first_info = first_ancestor.lstat()
        except OSError:
            return absolute
        if stat.S_ISLNK(first_info.st_mode):
            resolved_ancestor = first_ancestor.resolve(strict=True)
            if not resolved_ancestor.is_dir():
                return absolute
            return resolved_ancestor.joinpath(*parts[2:])
    return absolute


def _reject_broad_run_directory(path: Path) -> None:
    """Reject roots whose mutation could affect a whole user or workspace."""

    anchor = Path(path.anchor)
    home = Path.home().resolve()
    current = Path.cwd().resolve()
    forbidden = {anchor, home, current, *home.parents, *current.parents}
    if path in forbidden or path.parent == anchor:
        raise ValueError(f"run directory is an unsafe broad path: {path}")


def _inspect_run_path_components(path: Path) -> tuple[os.stat_result | None, list[Path]]:
    """Validate existing path components and identify missing components."""

    missing: list[Path] = []
    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect run directory path at {current}: {exc}") from exc
        if missing:
            raise ValueError(f"run directory path changed while being inspected: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"run directory path component is not a real directory: {current}")
    if missing:
        return None, missing
    return _require_real_directory(path, "run directory"), []


def _create_private_run_directory(path: Path, missing: list[Path]) -> os.stat_result:
    """Create and harden only path components created by this invocation."""

    if not missing or missing[-1] != path:
        raise ValueError(f"cannot safely create run directory: {path}")
    for directory in missing:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError(
                f"run directory path changed while being created: {directory}"
            ) from exc
        created = _require_real_directory(directory, "new run directory path")
        _make_verified_directory_private(directory, created, "new run directory path")
    return _require_real_directory(path, "run directory")


def _read_regular_json(path: Path, label: str) -> tuple[dict[str, Any], os.stat_result]:
    """Read JSON from one regular, non-linked file identity."""

    try:
        expected = path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect {label} at {path}: {exc}") from exc
    if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
        raise ValueError(f"{label} is not a single-link regular file: {path}")
    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (
            opened.st_dev,
            opened.st_ino,
        ) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"{label} changed while being opened: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"{label} changed while being read: {path}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} at {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object: {path}")
    return value, opened


def _run_marker_payload(path: Path, directory_info: os.stat_result) -> dict[str, Any]:
    return {
        "schema": _RUN_MARKER_SCHEMA,
        "run_dir": str(path),
        "directory_device": directory_info.st_dev,
        "directory_inode": directory_info.st_ino,
        "owner_uid": os.getuid() if hasattr(os, "getuid") else None,
    }


def _write_run_marker(path: Path, directory_info: os.stat_result) -> None:
    """Claim a newly created or manifest-proven run root without replacement."""

    marker = path / _RUN_MARKER_NAME
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(marker, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(canonical_json_bytes(_run_marker_payload(path, directory_info)) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ValueError(f"cannot create run ownership marker at {marker}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_run_marker(path: Path, directory_info: os.stat_result) -> None:
    marker_path = path / _RUN_MARKER_NAME
    marker, _ = _read_regular_json(marker_path, "run ownership marker")
    if marker != _run_marker_payload(path, directory_info):
        raise ValueError(f"run ownership marker does not match its directory: {marker_path}")


def _validate_resume_manifest(path: Path, plan: BuildPlan) -> None:
    manifest_path = path / "manifest.json"
    try:
        before = manifest_path.lstat()
    except OSError as exc:
        raise ValueError("resume requires a readable interrupted ModelBake manifest") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("resume requires a regular, single-link ModelBake manifest")
    try:
        prior = load_manifest(manifest_path)
        after = manifest_path.lstat()
    except (OSError, ManifestError) as exc:
        raise ValueError("resume requires a readable interrupted ModelBake manifest") from exc
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("resume manifest changed while being validated")
    if (
        prior.get("status") != "interrupted"
        or prior.get("recipe_digest") != plan.recipe_digest
        or prior.get("run_dir") != str(path)
    ):
        raise ValueError("resume requires an interrupted manifest for the same recipe and run directory")


class _RunDirectoryLock:
    """An advisory cross-process lock that is released automatically on exit."""

    def __init__(
        self, run_dir: Path, *, expected_run_dir: os.stat_result | None = None
    ) -> None:
        self.run_dir = run_dir
        self.path = run_dir / _RUN_LOCK_NAME
        self.expected_run_dir = expected_run_dir
        self.directory_descriptor: int | None = None
        self.handle: BinaryIO | None = None

    def _inspect_path(
        self, *, missing_ok: bool, directory_descriptor: int | None = None
    ) -> os.stat_result | None:
        try:
            if directory_descriptor is None:
                info = self.path.lstat()
            else:
                info = os.stat(
                    _RUN_LOCK_NAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise FileExistsError(f"run lock path disappeared: {self.path}") from None
        if not stat.S_ISREG(info.st_mode):
            raise FileExistsError(f"run lock path is not a regular file: {self.path}")
        return info

    def _validate_opened_identity(
        self,
        descriptor_info: os.stat_result,
        *,
        expected: os.stat_result | None = None,
        directory_descriptor: int | None = None,
    ) -> None:
        if not stat.S_ISREG(descriptor_info.st_mode):
            raise FileExistsError(f"run lock descriptor is not a regular file: {self.path}")
        if descriptor_info.st_nlink != 1:
            raise FileExistsError(f"run lock path has multiple hard links: {self.path}")
        current = self._inspect_path(
            missing_ok=False, directory_descriptor=directory_descriptor
        )
        assert current is not None
        opened_identity = (descriptor_info.st_dev, descriptor_info.st_ino)
        if (current.st_dev, current.st_ino) != opened_identity:
            raise FileExistsError(f"run lock path changed while opening: {self.path}")
        if expected is not None and (expected.st_dev, expected.st_ino) != opened_identity:
            raise FileExistsError(f"run lock path changed while opening: {self.path}")

    def __enter__(self) -> None:
        if os.name == "posix":
            directory_flags = os.O_RDONLY
            for optional_flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
                directory_flags |= getattr(os, optional_flag, 0)
            try:
                self.directory_descriptor = os.open(self.run_dir, directory_flags)
                opened_run_dir = os.fstat(self.directory_descriptor)
            except OSError as exc:
                if self.directory_descriptor is not None:
                    os.close(self.directory_descriptor)
                    self.directory_descriptor = None
                raise FileExistsError(
                    f"cannot open validated run directory for locking: {self.run_dir}"
                ) from exc
            if self.expected_run_dir is not None and (
                opened_run_dir.st_dev,
                opened_run_dir.st_ino,
            ) != (self.expected_run_dir.st_dev, self.expected_run_dir.st_ino):
                os.close(self.directory_descriptor)
                self.directory_descriptor = None
                raise FileExistsError(
                    f"run directory changed before locking: {self.run_dir}"
                )

        try:
            expected = self._inspect_path(
                missing_ok=True, directory_descriptor=self.directory_descriptor
            )
        except BaseException:
            if self.directory_descriptor is not None:
                os.close(self.directory_descriptor)
                self.directory_descriptor = None
            raise
        flags = os.O_CREAT | os.O_RDWR
        for optional_flag in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
            flags |= getattr(os, optional_flag, 0)
        descriptor: int | None = None
        try:
            try:
                if self.directory_descriptor is None:
                    descriptor = os.open(self.path, flags, 0o600)
                else:
                    descriptor = os.open(
                        _RUN_LOCK_NAME,
                        flags,
                        0o600,
                        dir_fd=self.directory_descriptor,
                    )
            except OSError as exc:
                try:
                    self._inspect_path(
                        missing_ok=True,
                        directory_descriptor=self.directory_descriptor,
                    )
                except FileExistsError as unsafe:
                    raise unsafe from exc
                raise
            descriptor_info = os.fstat(descriptor)
            self._validate_opened_identity(
                descriptor_info,
                expected=expected,
                directory_descriptor=self.directory_descriptor,
            )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            self.handle = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None

            if os.name == "posix":
                import fcntl

                try:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise FileExistsError(
                        f"run directory is locked: {self.path.parent}"
                    ) from exc
            else:  # pragma: no cover - Windows locking semantics
                import msvcrt

                if descriptor_info.st_size == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise FileExistsError(
                        f"run directory is locked: {self.path.parent}"
                    ) from exc

            self._validate_opened_identity(
                os.fstat(self.handle.fileno()),
                directory_descriptor=self.directory_descriptor,
            )
            if self.expected_run_dir is not None:
                current_run_dir = _require_real_directory(self.run_dir, "run directory")
                if (current_run_dir.st_dev, current_run_dir.st_ino) != (
                    self.expected_run_dir.st_dev,
                    self.expected_run_dir.st_ino,
                ):
                    raise FileExistsError(
                        f"run directory changed while locking: {self.run_dir}"
                    )
        except BaseException:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            elif descriptor is not None:
                os.close(descriptor)
            if self.directory_descriptor is not None:
                os.close(self.directory_descriptor)
                self.directory_descriptor = None
            raise

    def __exit__(self, *_exc: object) -> None:
        if self.handle is None:
            return
        if os.name == "posix":
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows locking semantics
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
        self.handle = None
        if self.directory_descriptor is not None:
            os.close(self.directory_descriptor)
            self.directory_descriptor = None


class BuildEngine:
    """Execute registered adapters in dependency order with verified caching."""

    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        adapters: Mapping[str, Adapter],
        *,
        runner: Mapping[str, Any] | None = None,
    ) -> None:
        self.cache = LocalCache(cache_dir)
        self.adapters = dict(adapters)
        self.observed_runner = (
            dict(runner)
            if runner is not None
            else runner_fingerprint(
                {
                    "modelbake": {
                        "version": __version__,
                        "python_source_digest": python_source_digest(Path(__file__).parent),
                    }
                }
            )
        )

    def _cache_key(
        self,
        plan: BuildPlan,
        node: NodeSpec,
        adapter_fingerprint: Mapping[str, Any],
        dependencies: Mapping[str, DependencyOutput],
        *,
        root_input_digest: str | None,
    ) -> str:
        dependency_digests = {
            dependency_id: dict(sorted(dependency.output_digests.items()))
            for dependency_id, dependency in sorted(dependencies.items())
        }
        return digest_json(
            {
                "schema": "modelbake.node-key.v1",
                "engine_semantics": "modelbake.engine/v3",
                "engine_implementation_digest": tree_digest(__file__),
                "node": node.to_dict(),
                "root_input_digest": root_input_digest,
                "dependency_output_digests": dependency_digests,
                "toolchain": {
                    "plan": plan.toolchain,
                    "adapter": dict(adapter_fingerprint),
                },
                "runner": {
                    "plan": plan.runner,
                    "observed": self.observed_runner,
                },
            }
        )

    @staticmethod
    def _source_digest(plan: BuildPlan) -> str:
        """Digest the declared source bytes when they are locally observable."""

        declared = plan.source.get("digest")
        source_path = plan.source.get("path")
        if isinstance(source_path, str) and source_path:
            observed = tree_digest(source_path)
            if isinstance(declared, str) and declared and observed != declared:
                raise ValueError(
                    "source bytes changed after planning; rebuild the plan before execution"
                )
            return observed
        if isinstance(declared, str) and declared:
            return declared
        return digest_json(plan.source)

    @staticmethod
    def _write_manifest(result: BuildResult) -> None:
        _private_directory(result.run_dir, parents=True)
        destination = result.run_dir / "manifest.json"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".manifest-", suffix=".json", dir=result.run_dir
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json_bytes(result.to_dict()) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _snapshot_dependencies(
        dependencies: Mapping[str, DependencyOutput], input_root: Path
    ) -> dict[str, DependencyOutput]:
        """Copy verified cache inputs into private, per-node snapshots."""

        _private_directory(input_root, parents=True)
        snapshots: dict[str, DependencyOutput] = {}
        for dependency_id, dependency in sorted(dependencies.items()):
            dependency_root = input_root / dependency_id
            _private_directory(dependency_root)
            copied: dict[str, Path] = {}
            for name, source_value in sorted(dependency.outputs.items()):
                source = Path(source_value)
                destination = dependency_root / name
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=True)
                elif source.is_file():
                    shutil.copy2(source, destination, follow_symlinks=False)
                else:
                    raise CacheCorruptionError(
                        f"dependency snapshot source is not a regular file or directory: "
                        f"{dependency_id}:{name}"
                    )
                observed = tree_digest(destination)
                expected = dependency.output_digests.get(name)
                if observed != expected:
                    raise CacheCorruptionError(
                        f"dependency changed while snapshotting {dependency_id}:{name}"
                    )
                copied[name] = destination
            _private_tree(dependency_root)
            snapshots[dependency_id] = DependencyOutput(copied, dependency.output_digests)
        return snapshots

    @staticmethod
    def _verify_dependency_snapshots(
        dependencies: Mapping[str, DependencyOutput],
    ) -> None:
        for dependency_id, dependency in sorted(dependencies.items()):
            for name, path in sorted(dependency.outputs.items()):
                if tree_digest(path) != dependency.output_digests[name]:
                    raise RuntimeError(
                        f"adapter modified dependency snapshot {dependency_id}:{name}; "
                        "outputs were not cached"
                    )

    @staticmethod
    def _prepare_run_directory(
        path: Path, plan: BuildPlan, *, resume: bool
    ) -> os.stat_result:
        """Prove run-root ownership before chmod, marker creation, or locking."""

        _reject_broad_run_directory(path)
        directory_info, missing = _inspect_run_path_components(path)
        if missing:
            if resume:
                raise ValueError("resume requires an existing ModelBake run directory")
            directory_info = _create_private_run_directory(path, missing)
            _write_run_marker(path, directory_info)
            return directory_info

        assert directory_info is not None
        try:
            entries = list(path.iterdir())
        except OSError as exc:
            raise ValueError(f"cannot list run directory at {path}: {exc}") from exc

        lock_path = path / _RUN_LOCK_NAME
        if any(entry.name == _RUN_LOCK_NAME for entry in entries):
            lock_info = _RunDirectoryLock(path)._inspect_path(missing_ok=False)
            assert lock_info is not None
            if lock_info.st_nlink != 1:
                raise FileExistsError(f"run lock path has multiple hard links: {lock_path}")

        has_marker = any(entry.name == _RUN_MARKER_NAME for entry in entries)
        if has_marker:
            _validate_run_marker(path, directory_info)

        content = [
            entry
            for entry in entries
            if entry.name not in {_RUN_LOCK_NAME, _RUN_MARKER_NAME}
        ]
        if content:
            if not resume:
                raise FileExistsError(
                    f"run directory is not empty: {path}; pass resume=True only for an interrupted run"
                )
            _validate_resume_manifest(path, plan)
            if not has_marker and not any(
                entry.name == _RUN_LOCK_NAME for entry in entries
            ):
                raise ValueError(
                    "legacy resume requires both an interrupted manifest and ModelBake run lock"
                )
        elif resume:
            raise ValueError("resume requires a readable interrupted ModelBake manifest")
        elif not has_marker:
            raise ValueError(
                f"refusing pre-existing unowned run directory, even though it is empty: {path}"
            )

        _make_verified_directory_private(path, directory_info, "owned run directory")
        if not has_marker:
            _write_run_marker(path, directory_info)
        return directory_info

    def run(
        self,
        plan: BuildPlan,
        *,
        run_dir: str | os.PathLike[str] | None = None,
        run_id: str | None = None,
        resume: bool = False,
    ) -> BuildResult:
        effective_run_id = run_id or uuid.uuid4().hex
        if not _RUN_ID_RE.fullmatch(effective_run_id):
            raise ValueError("run_id must be 1-128 filename-safe characters")
        effective_run_dir = _absolute_run_directory(
            Path(run_dir) if run_dir else Path(".modelbake/runs") / effective_run_id
        )
        run_directory_info = self._prepare_run_directory(
            effective_run_dir, plan, resume=resume
        )
        with _RunDirectoryLock(
            effective_run_dir, expected_run_dir=run_directory_info
        ):
            return self._run_owned(
                plan,
                run_dir=effective_run_dir,
                run_id=effective_run_id,
                resume=resume,
            )

    def _run_owned(
        self,
        plan: BuildPlan,
        *,
        run_dir: str | os.PathLike[str],
        run_id: str,
        resume: bool,
    ) -> BuildResult:
        ordered = topological_sort(plan.nodes)
        effective_run_id = run_id
        effective_run_dir = _absolute_run_directory(run_dir)
        existing_entries = [
            entry
            for entry in effective_run_dir.iterdir()
            if entry.name not in {_RUN_LOCK_NAME, _RUN_MARKER_NAME}
        ]
        if existing_entries and not resume:
            raise FileExistsError(f"run directory ownership changed before locking: {effective_run_dir}")
        _private_directory(effective_run_dir, parents=True)
        work_root = effective_run_dir / "work"
        _private_directory(work_root)
        started = _utc_now()
        results: list[NodeResult] = []
        by_id: dict[str, NodeResult] = {}
        completed: dict[str, DependencyOutput] = {}
        source_digest = self._source_digest(plan)

        build_result = BuildResult(
            schema="modelbake.run.v1",
            run_id=effective_run_id,
            project=plan.project,
            status="running",
            recipe_digest=plan.recipe_digest,
            source_digest=source_digest,
            runner={"declared": plan.runner, "observed": self.observed_runner},
            started_at=started,
            finished_at=started,
            run_dir=effective_run_dir,
            nodes=results,
            claims=["States record adapter execution or digest-verified cache bytes observed by this run."],
            exclusions=[
                "No state asserts compatibility beyond the exact recorded adapter execution.",
                "No artifact quality or numerical equivalence is inferred.",
            ],
        )
        self._write_manifest(build_result)

        try:
            for node in ordered:
                blocked_by = [
                    dependency
                    for dependency in node.dependencies
                    if by_id[dependency].state in (NodeState.FAILED, NodeState.BLOCKED)
                ]
                if blocked_by:
                    result = NodeResult(
                        id=node.id,
                        kind=node.kind,
                        state=NodeState.BLOCKED,
                        cache_key="",
                        observations={"blocked_by": blocked_by},
                        error="dependency did not produce an output",
                    )
                    results.append(result)
                    by_id[node.id] = result
                    self._write_manifest(build_result)
                    continue

                adapter = self.adapters.get(node.kind)
                if adapter is None:
                    result = NodeResult(
                        id=node.id,
                        kind=node.kind,
                        state=NodeState.FAILED,
                        cache_key="",
                        error=f"no adapter registered for node kind {node.kind!r}",
                    )
                    results.append(result)
                    by_id[node.id] = result
                    self._write_manifest(build_result)
                    continue

                dependencies = {dependency: completed[dependency] for dependency in node.dependencies}
                adapter_fingerprint = dict(adapter.fingerprint())
                cache_key = self._cache_key(
                    plan,
                    node,
                    adapter_fingerprint,
                    dependencies,
                    root_input_digest=source_digest if not node.dependencies else None,
                )
                input_digests = {
                    f"{dependency_id}:{name}": digest
                    for dependency_id, dependency in dependencies.items()
                    for name, digest in dependency.output_digests.items()
                }
                try:
                    cached = self.cache.load(cache_key)
                except CacheCorruptionError as exc:
                    result = NodeResult(
                        id=node.id,
                        kind=node.kind,
                        state=NodeState.FAILED,
                        cache_key=cache_key,
                        input_digests=input_digests,
                        error=str(exc),
                    )
                    results.append(result)
                    by_id[node.id] = result
                    self._write_manifest(build_result)
                    continue

                if cached is not None:
                    metadata = dict(cached.metadata)
                    observations = dict(metadata.get("observations", {}))
                    observations["cached_state"] = metadata.get("state", NodeState.PRODUCED.value)
                    result = NodeResult(
                        id=node.id,
                        kind=node.kind,
                        state=NodeState.CACHE_HIT,
                        cache_key=cache_key,
                        input_digests=input_digests,
                        output_digests=dict(cached.output_digests),
                        outputs={name: str(path.resolve()) for name, path in cached.outputs.items()},
                        command=list(metadata.get("command", [])),
                        observations=observations,
                        duration_ms=0,
                    )
                    results.append(result)
                    by_id[node.id] = result
                    completed[node.id] = DependencyOutput(cached.outputs, cached.output_digests)
                    self._write_manifest(build_result)
                    continue

                node_root = work_root / node.id
                if node_root.exists():
                    shutil.rmtree(node_root)
                output_dir = node_root / "outputs"
                _private_directory(node_root, parents=True)
                _private_directory(output_dir)
                input_root = node_root / "inputs"
                log_path = node_root / "adapter.log"
                began = time.monotonic_ns()
                commit = None
                try:
                    snapshotted_dependencies = self._snapshot_dependencies(
                        dependencies, input_root
                    )
                    context = AdapterContext(
                        node=node,
                        plan=plan,
                        work_dir=node_root,
                        output_dir=output_dir,
                        log_path=log_path,
                        dependencies=snapshotted_dependencies,
                    )
                    execution = adapter.run(context)
                    self._verify_dependency_snapshots(snapshotted_dependencies)
                    if dict(adapter.fingerprint()) != adapter_fingerprint:
                        raise RuntimeError(
                            "adapter fingerprint changed during execution; outputs were not cached"
                        )
                    elapsed = (time.monotonic_ns() - began) // 1_000_000
                    commit = self.cache.commit(
                        cache_key,
                        execution.outputs,
                        {
                            "state": execution.state.value,
                            "command": list(execution.command),
                            "observations": dict(execution.observations),
                        },
                        allowed_root=output_dir,
                    )
                    cache_entry = commit.entry
                    authoritative_metadata = dict(cache_entry.metadata)
                    authoritative_observations = dict(
                        authoritative_metadata.get("observations", {})
                    )
                    authoritative_command = list(authoritative_metadata.get("command", []))
                    if not commit.created:
                        authoritative_observations["cache_race_lost"] = True
                    result = NodeResult(
                        id=node.id,
                        kind=node.kind,
                        state=execution.state if commit.created else NodeState.CACHE_HIT,
                        cache_key=cache_key,
                        input_digests=input_digests,
                        output_digests=dict(cache_entry.output_digests),
                        outputs={
                            name: str(path.resolve()) for name, path in cache_entry.outputs.items()
                        },
                        command=(list(execution.command) if commit.created else authoritative_command),
                        observations=(
                            dict(execution.observations)
                            if commit.created
                            else authoritative_observations
                        ),
                        duration_ms=int(elapsed) if commit.created else 0,
                        log_path=(
                            str(log_path) if commit.created and log_path.exists() else None
                        ),
                    )
                    completed[node.id] = DependencyOutput(
                        cache_entry.outputs, cache_entry.output_digests
                    )
                except KeyboardInterrupt:
                    build_result.status = "interrupted"
                    build_result.finished_at = _utc_now()
                    self._write_manifest(build_result)
                    raise
                except Exception as exc:  # noqa: BLE001 - adapters are a failure boundary
                    elapsed = (time.monotonic_ns() - began) // 1_000_000
                    result = NodeResult(
                        id=node.id,
                        kind=node.kind,
                        state=NodeState.FAILED,
                        cache_key=cache_key,
                        input_digests=input_digests,
                        duration_ms=int(elapsed),
                        log_path=str(log_path) if log_path.exists() else None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                finally:
                    if input_root.exists():
                        shutil.rmtree(input_root)
                    _private_tree(node_root)
                if commit is not None and not commit.created and node_root.exists():
                    shutil.rmtree(node_root)
                results.append(result)
                by_id[node.id] = result
                self._write_manifest(build_result)

            build_result.status = (
                "failed"
                if any(result.state in (NodeState.FAILED, NodeState.BLOCKED) for result in results)
                else "succeeded"
            )
            build_result.finished_at = _utc_now()
            self._write_manifest(build_result)
            return build_result
        except BaseException:
            if build_result.status == "running":
                build_result.status = "interrupted"
                build_result.finished_at = _utc_now()
                self._write_manifest(build_result)
            raise
