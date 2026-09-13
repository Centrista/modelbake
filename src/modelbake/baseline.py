"""Owner-private, tamper-evident accepted-baseline ledgers.

The ledger records a narrow release decision: a caller accepted a succeeded
manifest whose declared artifacts matched their digests inside caller-selected
trusted roots. It does not authenticate an actor, sign a release, or establish
model quality or compatibility.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .compare import compare_manifests
from .hashing import canonical_json_bytes, digest_bytes, digest_json
from .manifest import ManifestError, load_manifest, verify_manifest

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_DECISION_FILE_RE = re.compile(r"^(\d{20})-([0-9a-f]{64})\.json$")
_MAX_NOTE_CHARACTERS = 240
_MAX_NOTE_BYTES = 512


class BaselineError(RuntimeError):
    """Raised when a baseline cannot be accepted or loaded safely."""


class BaselineCorruptionError(BaselineError):
    """Raised when stored ledger content is malformed or has been changed."""


@dataclass(frozen=True, slots=True)
class BaselineAcceptance:
    name: str
    decision_digest: str
    manifest_digest: str
    decision: Mapping[str, Any]
    manifest_path: Path
    created: bool
    verified_artifacts: int


@dataclass(frozen=True, slots=True)
class CurrentBaseline:
    name: str
    decision_digest: str
    manifest_digest: str
    decision: Mapping[str, Any]
    manifest: Mapping[str, Any]
    manifest_bytes: bytes
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class LedgerVerification:
    name: str
    decisions: int
    current_decision_digest: str | None
    current_manifest_digest: str | None


@dataclass(frozen=True, slots=True)
class BaselineReview:
    name: str
    project: str
    receipt_digest: str
    candidate_manifest_digest: str
    predecessor_decision_digest: str | None
    comparison_digest: str
    receipt: Mapping[str, Any]
    path: Path
    verified_artifacts: int


@dataclass(frozen=True, slots=True)
class BaselineCheck:
    name: str
    decision_digest: str
    manifest_digest: str
    run_id: str
    verified_artifacts: int


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or name in {".", ".."} or not _NAME_RE.fullmatch(name):
        raise BaselineError(
            "baseline name must be 1-64 filename-safe ASCII characters"
        )
    return name


def _validate_note(note: str | None) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str) or not note:
        raise BaselineError("baseline note must be a non-empty string or omitted")
    if len(note) > _MAX_NOTE_CHARACTERS or len(note.encode("utf-8")) > _MAX_NOTE_BYTES:
        raise BaselineError("baseline note is too long")
    if any(character in note for character in ("\r", "\n")):
        raise BaselineError("baseline note must be a single line")
    if any(ord(character) < 32 or ord(character) == 127 for character in note):
        raise BaselineError("baseline note cannot contain control characters")
    return note


def _inspect_directory(path: Path, *, label: str) -> bool:
    """Return whether an existing path is a safe directory."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BaselineError(f"cannot inspect {label}: {path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise BaselineError(f"{label} is not a real directory: {path}")
    return True


def _ensure_directory(path: Path, *, label: str) -> None:
    if _inspect_directory(path, label=label):
        return
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        if not _inspect_directory(path, label=label):
            raise AssertionError("unreachable")
    except OSError as exc:
        raise BaselineError(f"cannot create {label}: {path}: {exc}") from exc


def _initialize_store(store: str | os.PathLike[str]) -> Path:
    root = Path(store).absolute()
    if not _inspect_directory(root, label="baseline store root"):
        try:
            root.mkdir(parents=True, mode=0o700)
            os.chmod(root, 0o700)
        except FileExistsError:
            if not _inspect_directory(root, label="baseline store root"):
                raise AssertionError("unreachable")
        except OSError as exc:
            raise BaselineError(f"cannot create baseline store root: {root}: {exc}") from exc

    # Never chmod a pre-existing caller-owned root. Every child below it is
    # ModelBake-created and private.
    for child, label in (
        (root / "manifests", "manifest record directory"),
        (root / "reviews", "review record directory"),
        (root / "ledgers", "ledger directory"),
        (root / "locks", "ledger lock directory"),
    ):
        _ensure_directory(child, label=label)
    return root


def _open_regular(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BaselineCorruptionError(f"cannot open regular file safely: {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BaselineCorruptionError(f"stored path is not a regular file: {path}")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _read_regular_bytes(path: Path) -> bytes:
    with _open_regular(path) as stream:
        try:
            return stream.read()
        except OSError as exc:
            raise BaselineCorruptionError(f"cannot read stored file: {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BaselineError(f"cannot sync baseline directory {path}: {exc}") from exc


def _write_immutable(path: Path, data: bytes) -> bool:
    """Publish bytes without overwriting. Return False for identical content."""

    try:
        existing = _read_regular_bytes(path)
    except BaselineCorruptionError:
        if path.exists() or path.is_symlink():
            raise
    else:
        if existing != data:
            raise BaselineCorruptionError(f"immutable record content mismatch: {path}")
        return False

    temporary_descriptor, temporary_name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(temporary_descriptor, 0o600)
        with os.fdopen(temporary_descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing = _read_regular_bytes(path)
            if existing != data:
                raise BaselineCorruptionError(f"immutable record content mismatch: {path}")
            return False
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
        return True
    except OSError as exc:
        raise BaselineError(f"cannot write immutable baseline record {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _replace_private(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            try:
                info = path.lstat()
            except OSError as exc:
                raise BaselineCorruptionError(f"cannot inspect current pointer: {exc}") from exc
            if not stat.S_ISREG(info.st_mode):
                raise BaselineCorruptionError("current pointer is not a regular file")
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise BaselineError(f"cannot atomically update current baseline: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_private_atomic(path: Path, data: bytes) -> None:
    """Atomically publish a private regular file, refusing link/special targets."""

    path = path.absolute()
    missing: list[Path] = []
    ancestor = path.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        missing.append(ancestor)
        ancestor = ancestor.parent
    if not _inspect_directory(ancestor, label="review output ancestor"):
        raise AssertionError("unreachable")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
    if not _inspect_directory(path.parent, label="review output directory"):
        raise AssertionError("unreachable")
    if path.exists() or path.is_symlink():
        try:
            info = path.lstat()
        except OSError as exc:
            raise BaselineError(f"cannot inspect review output: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise BaselineError("review output must be a regular, non-symlink file")
    _replace_private(path, data)


@contextmanager
def _ledger_lock(root: Path, name: str) -> Iterator[None]:
    path = root / "locks" / f"{name}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BaselineCorruptionError("ledger lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except BaselineError:
        raise
    except OSError as exc:
        raise BaselineError(f"cannot lock baseline ledger {name!r}: {exc}") from exc
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _digest_hex(value: str, *, label: str) -> str:
    match = _DIGEST_RE.fullmatch(value)
    if not match:
        raise BaselineCorruptionError(f"{label} is not a sha256 digest")
    return match.group(1)


def _load_canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    raw = _read_regular_bytes(path)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineCorruptionError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BaselineCorruptionError(f"{label} must be a JSON object")
    if raw != canonical_json_bytes(value) + b"\n":
        raise BaselineCorruptionError(f"{label} is not in canonical stored form")
    return value


def _validate_receipt_object(value: Mapping[str, Any], *, label: str) -> str:
    required = {
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
    }
    if set(value) != required or value.get("schema") != "modelbake.baseline.review.v1":
        raise BaselineCorruptionError(f"{label} fields or schema are invalid")
    try:
        _validate_name(value["name"])
    except BaselineError as exc:
        raise BaselineCorruptionError(f"{label} channel is invalid: {exc}") from exc
    for field in ("project", "reviewed_at"):
        if not isinstance(value[field], str) or not value[field]:
            raise BaselineCorruptionError(f"{label} {field} is invalid")
    _digest_hex(value["candidate_manifest_digest"], label="candidate manifest digest")
    predecessor = value["predecessor_decision_digest"]
    if predecessor is not None:
        _digest_hex(predecessor, label="predecessor decision digest")
    if not isinstance(value["initial"], bool):
        raise BaselineCorruptionError(f"{label} initial flag is invalid")
    if value["initial"] != (predecessor is None):
        raise BaselineCorruptionError(f"{label} initial/predecessor fields disagree")
    if (
        not isinstance(value["verified_artifacts"], int)
        or isinstance(value["verified_artifacts"], bool)
        or value["verified_artifacts"] < 1
    ):
        raise BaselineCorruptionError(f"{label} verified artifact count is invalid")
    comparison = value["comparison"]
    if not isinstance(comparison, Mapping):
        raise BaselineCorruptionError(f"{label} comparison is invalid")
    comparison_digest = value["comparison_digest"]
    _digest_hex(comparison_digest, label="comparison digest")
    if digest_json(comparison) != comparison_digest:
        raise BaselineCorruptionError(f"{label} comparison digest mismatch")
    record_digest = value["record_digest"]
    _digest_hex(record_digest, label="review record digest")
    payload = dict(value)
    del payload["record_digest"]
    if digest_json(payload) != record_digest:
        raise BaselineCorruptionError(f"{label} record digest mismatch")
    return record_digest


def _load_review(path: Path, *, label: str = "review receipt") -> dict[str, Any]:
    value = _load_canonical_object(path, label=label)
    _validate_receipt_object(value, label=label)
    return value


def _verify_review_records(root: Path) -> None:
    reviews = root / "reviews"
    try:
        entries = list(reviews.iterdir())
    except OSError as exc:
        raise BaselineCorruptionError(f"cannot list stored reviews: {exc}") from exc
    for path in entries:
        match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
        if match is None:
            raise BaselineCorruptionError("stored review filename is invalid")
        value = _load_review(path, label="stored review")
        if _digest_hex(value["record_digest"], label="review record digest") != match.group(1):
            raise BaselineCorruptionError("stored review filename does not match its record digest")


def _decision_path(channel: Path, sequence: int, decision_digest: str) -> Path:
    return channel / "decisions" / f"{sequence:020d}-{_digest_hex(decision_digest, label='decision digest')}.json"


def _verify_manifest_records(root: Path) -> None:
    manifests = root / "manifests"
    try:
        entries = list(manifests.iterdir())
    except OSError as exc:
        raise BaselineCorruptionError(f"cannot list stored manifests: {exc}") from exc
    for path in entries:
        match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
        if match is None:
            raise BaselineCorruptionError("stored manifest filename is invalid")
        raw = _read_regular_bytes(path)
        if _digest_hex(digest_bytes(raw), label="stored manifest digest") != match.group(1):
            raise BaselineCorruptionError("stored manifest filename does not match its bytes")
        try:
            load_manifest(path)
        except ManifestError as exc:
            raise BaselineCorruptionError(f"stored manifest is invalid: {exc}") from exc


def _verify_ledger_at(root: Path, name: str) -> tuple[LedgerVerification, dict[str, Any] | None]:
    _verify_manifest_records(root)
    _verify_review_records(root)
    channel = root / "ledgers" / name
    if not channel.exists() and not channel.is_symlink():
        return LedgerVerification(name, 0, None, None), None
    if not _inspect_directory(channel, label="baseline channel"):
        raise AssertionError("unreachable")
    try:
        channel_entries = {entry.name for entry in channel.iterdir()}
    except OSError as exc:
        raise BaselineCorruptionError(f"cannot list baseline channel: {exc}") from exc
    if not channel_entries.issubset({"decisions", "current.json"}):
        raise BaselineCorruptionError("baseline channel contains an unexpected path")
    decisions_dir = channel / "decisions"
    if not _inspect_directory(decisions_dir, label="baseline decisions directory"):
        raise BaselineCorruptionError("baseline decisions directory is missing")

    try:
        entries = sorted(decisions_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise BaselineCorruptionError(f"cannot list baseline decisions: {exc}") from exc
    predecessor: str | None = None
    terminal: dict[str, Any] | None = None
    for expected_sequence, path in enumerate(entries, start=1):
        match = _DECISION_FILE_RE.fullmatch(path.name)
        if not match or int(match.group(1)) != expected_sequence:
            raise BaselineCorruptionError("decision filenames must form a contiguous sequence")
        value = _load_canonical_object(path, label=f"decision {expected_sequence}")
        schema = value.get("schema")
        if schema not in {
            "modelbake.baseline.decision.v1",
            "modelbake.baseline.decision.v2",
        }:
            raise BaselineCorruptionError("decision schema is invalid")
        required = {
            "schema",
            "name",
            "sequence",
            "predecessor_decision_digest",
            "manifest_digest",
            "manifest_record",
            "run_id",
            "project",
            "status",
            "accepted_at",
            "record_digest",
        }
        optional = {"note"}
        if schema == "modelbake.baseline.decision.v2":
            required |= {"review_digest", "review_record"}
        if not required.issubset(value) or not set(value).issubset(required | optional):
            raise BaselineCorruptionError("decision fields are invalid")
        if value["name"] != name or value["sequence"] != expected_sequence:
            raise BaselineCorruptionError("decision channel or sequence mismatch")
        if value["predecessor_decision_digest"] != predecessor:
            raise BaselineCorruptionError("decision hash chain is discontinuous")
        if value["status"] != "succeeded":
            raise BaselineCorruptionError("accepted decision status must be succeeded")
        for field in ("run_id", "project", "accepted_at"):
            if not isinstance(value[field], str) or not value[field]:
                raise BaselineCorruptionError(f"decision {field} is invalid")
        try:
            _validate_note(value.get("note"))
        except BaselineError as exc:
            raise BaselineCorruptionError(str(exc)) from exc
        manifest_hex = _digest_hex(value["manifest_digest"], label="manifest digest")
        if value["manifest_record"] != f"manifests/{manifest_hex}.json":
            raise BaselineCorruptionError("manifest record path does not match its digest")
        manifest_path = root / value["manifest_record"]
        manifest_bytes = _read_regular_bytes(manifest_path)
        if digest_bytes(manifest_bytes) != value["manifest_digest"]:
            raise BaselineCorruptionError("stored manifest digest mismatch")
        try:
            stored_manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            raise BaselineCorruptionError(f"stored manifest is invalid: {exc}") from exc
        if (
            stored_manifest["status"] != "succeeded"
            or stored_manifest["run_id"] != value["run_id"]
            or stored_manifest["project"] != value["project"]
        ):
            raise BaselineCorruptionError("decision identity does not match stored manifest")

        if schema == "modelbake.baseline.decision.v2":
            review_hex = _digest_hex(value["review_digest"], label="review digest")
            if value["review_record"] != f"reviews/{review_hex}.json":
                raise BaselineCorruptionError("review record path does not match its digest")
            stored_review = _load_review(root / value["review_record"], label="stored review")
            if stored_review["record_digest"] != value["review_digest"]:
                raise BaselineCorruptionError("decision review digest mismatch")
            if (
                stored_review["name"] != name
                or stored_review["project"] != value["project"]
                or stored_review["candidate_manifest_digest"] != value["manifest_digest"]
                or stored_review["predecessor_decision_digest"]
                != value["predecessor_decision_digest"]
            ):
                raise BaselineCorruptionError("decision identity does not match stored review")

        record_digest = value["record_digest"]
        _digest_hex(record_digest, label="record digest")
        payload = dict(value)
        del payload["record_digest"]
        if digest_json(payload) != record_digest or match.group(2) != _digest_hex(
            record_digest, label="record digest"
        ):
            raise BaselineCorruptionError("decision record digest mismatch")
        predecessor = record_digest
        terminal = value

    pointer_path = channel / "current.json"
    if terminal is None:
        if pointer_path.exists() or pointer_path.is_symlink():
            raise BaselineCorruptionError("empty ledger cannot have a current pointer")
        return LedgerVerification(name, 0, None, None), None
    if not pointer_path.exists() and not pointer_path.is_symlink():
        raise BaselineCorruptionError("non-empty ledger has no current pointer")
    pointer = _load_canonical_object(pointer_path, label="current pointer")
    expected_pointer = {
        "schema": "modelbake.baseline.current.v1",
        "name": name,
        "decision_digest": terminal["record_digest"],
        "decision_record": str(
            _decision_path(channel, len(entries), terminal["record_digest"]).relative_to(root)
        ),
        "manifest_digest": terminal["manifest_digest"],
    }
    if pointer != expected_pointer:
        raise BaselineCorruptionError("current pointer does not identify the terminal decision")
    return (
        LedgerVerification(
            name,
            len(entries),
            terminal["record_digest"],
            terminal["manifest_digest"],
        ),
        terminal,
    )


def verify_ledger(
    store: str | os.PathLike[str], name: str
) -> LedgerVerification:
    """Verify canonical records, the full hash chain, pointer, and manifest bytes."""

    safe_name = _validate_name(name)
    root = _initialize_store(store)
    result, _ = _verify_ledger_at(root, safe_name)
    return result


def load_current_baseline(
    store: str | os.PathLike[str], name: str
) -> CurrentBaseline:
    """Load the current baseline only after verifying its complete local ledger."""

    safe_name = _validate_name(name)
    root = _initialize_store(store)
    result, decision = _verify_ledger_at(root, safe_name)
    if decision is None or result.current_manifest_digest is None:
        raise BaselineError(f"baseline {safe_name!r} has not been accepted")
    manifest_hex = _digest_hex(result.current_manifest_digest, label="manifest digest")
    manifest_path = root / "manifests" / f"{manifest_hex}.json"
    manifest_bytes = _read_regular_bytes(manifest_path)
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        raise BaselineCorruptionError(f"stored manifest is invalid: {exc}") from exc
    return CurrentBaseline(
        safe_name,
        result.current_decision_digest or "",
        result.current_manifest_digest,
        decision,
        manifest,
        manifest_bytes,
        manifest_path,
    )


def _inspect_candidate(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BaselineError(f"cannot inspect candidate manifest: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BaselineError("candidate manifest must be a regular, non-symlink file")
    return _read_regular_bytes(path)


def _load_and_live_verify_candidate(
    path: Path, allowed_roots: Sequence[str | os.PathLike[str]]
) -> tuple[bytes, dict[str, Any], int]:
    if not allowed_roots:
        raise BaselineError("operation requires explicit trusted artifact roots")
    before = _inspect_candidate(path)
    try:
        parsed = load_manifest(path)
    except ManifestError as exc:
        raise BaselineError(f"candidate manifest is invalid: {exc}") from exc
    if parsed["status"] != "succeeded":
        raise BaselineError("only a succeeded manifest can be reviewed or accepted")
    try:
        verification = verify_manifest(path, allowed_roots=tuple(allowed_roots))
    except ManifestError as exc:
        raise BaselineError(f"candidate manifest verification failed: {exc}") from exc
    after = _read_regular_bytes(path)
    if before != after:
        raise BaselineError("candidate manifest changed during live verification")
    if not verification.ok:
        details = "; ".join(verification.failures)
        raise BaselineError(f"candidate manifest artifact verification failed: {details}")
    return before, parsed, verification.checked


def _initial_comparison(parsed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "modelbake.compare.initial.v1",
        "before": None,
        "after": {
            "run_id": parsed["run_id"],
            "status": parsed["status"],
            "project": parsed["project"],
        },
        "promotion_decision": "not_inferred",
        "truth_boundary": (
            "Initial empty-channel review. Artifact digests were live-verified, but no "
            "predecessor exists and no model quality, compatibility, or authorization is inferred."
        ),
    }


def _recompute_receipt_comparison(
    root: Path,
    name: str,
    receipt: Mapping[str, Any],
    candidate_path: Path,
    parsed: Mapping[str, Any],
) -> dict[str, Any]:
    predecessor = receipt["predecessor_decision_digest"]
    if predecessor is None:
        return _initial_comparison(parsed)
    predecessor_hex = _digest_hex(predecessor, label="predecessor decision digest")
    decisions = root / "ledgers" / name / "decisions"
    matches = sorted(decisions.glob(f"*-{predecessor_hex}.json"))
    if len(matches) != 1:
        raise BaselineCorruptionError("review predecessor decision is not in this channel")
    decision = _load_canonical_object(matches[0], label="review predecessor decision")
    manifest_hex = _digest_hex(decision["manifest_digest"], label="manifest digest")
    return compare_manifests(
        root / "manifests" / f"{manifest_hex}.json", candidate_path
    )


def review_baseline(
    manifest: str | os.PathLike[str],
    store: str | os.PathLike[str],
    name: str,
    allowed_roots: Sequence[str | os.PathLike[str]],
    review: str | os.PathLike[str],
    *,
    initial: bool = False,
) -> BaselineReview:
    """Create an exact, live-verified compare-and-swap review receipt."""

    safe_name = _validate_name(name)
    candidate_path = Path(manifest).absolute()
    review_path = Path(review).absolute()
    root = _initialize_store(store)
    with _ledger_lock(root, safe_name):
        candidate_bytes, parsed, checked = _load_and_live_verify_candidate(
            candidate_path, allowed_roots
        )
        ledger, terminal = _verify_ledger_at(root, safe_name)
        if initial:
            if terminal is not None or ledger.decisions:
                raise BaselineError("--initial is valid only for an empty baseline channel")
            comparison = _initial_comparison(parsed)
        else:
            if terminal is None or ledger.current_manifest_digest is None:
                raise BaselineError(
                    "baseline channel is empty; use --initial for its first reviewed release"
                )
            current_hex = _digest_hex(
                ledger.current_manifest_digest, label="current manifest digest"
            )
            current_path = root / "manifests" / f"{current_hex}.json"
            current = load_manifest(current_path)
            if current["project"] != parsed["project"]:
                raise BaselineError(
                    "candidate project does not match the current baseline project"
                )
            comparison = compare_manifests(current_path, candidate_path)

        payload: dict[str, Any] = {
            "schema": "modelbake.baseline.review.v1",
            "name": safe_name,
            "project": parsed["project"],
            "reviewed_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "candidate_manifest_digest": digest_bytes(candidate_bytes),
            "predecessor_decision_digest": ledger.current_decision_digest,
            "comparison_digest": digest_json(comparison),
            "comparison": comparison,
            "initial": initial,
            "verified_artifacts": checked,
        }
        receipt_digest = digest_json(payload)
        receipt = {**payload, "record_digest": receipt_digest}
        _write_private_atomic(
            review_path, canonical_json_bytes(receipt) + b"\n"
        )
        return BaselineReview(
            safe_name,
            parsed["project"],
            receipt_digest,
            payload["candidate_manifest_digest"],
            ledger.current_decision_digest,
            payload["comparison_digest"],
            receipt,
            review_path,
            checked,
        )


def check_baseline(
    manifest: str | os.PathLike[str],
    store: str | os.PathLike[str],
    name: str,
    allowed_roots: Sequence[str | os.PathLike[str]],
) -> BaselineCheck:
    """Require exact current acceptance and current live artifact bytes."""

    safe_name = _validate_name(name)
    candidate_path = Path(manifest).absolute()
    root = _initialize_store(store)
    with _ledger_lock(root, safe_name):
        candidate_bytes, parsed, checked = _load_and_live_verify_candidate(
            candidate_path, allowed_roots
        )
        ledger, terminal = _verify_ledger_at(root, safe_name)
        candidate_digest = digest_bytes(candidate_bytes)
        if terminal is None or ledger.current_manifest_digest != candidate_digest:
            raise BaselineError(
                "candidate is not the exact current accepted manifest"
            )
        if terminal.get("schema") != "modelbake.baseline.decision.v2" or not terminal.get(
            "review_digest"
        ):
            raise BaselineError(
                "current baseline is a legacy unreviewed decision; create and consume a review receipt"
            )
        return BaselineCheck(
            safe_name,
            terminal["record_digest"],
            candidate_digest,
            parsed["run_id"],
            checked,
        )


def accept_baseline(
    manifest: str | os.PathLike[str],
    store: str | os.PathLike[str],
    name: str,
    allowed_roots: Sequence[str | os.PathLike[str]],
    note: str | None = None,
    review: str | os.PathLike[str] | None = None,
) -> BaselineAcceptance:
    """Verify and append one accepted baseline, or return the current identical one.

    ``allowed_roots`` is mandatory and caller-selected. ``note`` is the only
    caller-provided annotation; ModelBake never harvests environment data or
    credentials into a decision record.
    """

    safe_name = _validate_name(name)
    safe_note = _validate_note(note)
    manifest_path = Path(manifest).absolute()

    root = _initialize_store(store)
    with _ledger_lock(root, safe_name):
        before, parsed, checked = _load_and_live_verify_candidate(
            manifest_path, allowed_roots
        )
        ledger, terminal = _verify_ledger_at(root, safe_name)
        manifest_digest = digest_bytes(before)
        receipt: dict[str, Any] | None = None
        receipt_bytes: bytes | None = None
        if review is not None:
            review_path = Path(review).absolute()
            receipt_bytes = _read_regular_bytes(review_path)
            receipt = _load_review(review_path)
            mismatch: str | None = None
            if receipt["name"] != safe_name:
                mismatch = "review channel"
            elif receipt["project"] != parsed["project"]:
                mismatch = "review project"
            elif receipt["candidate_manifest_digest"] != manifest_digest:
                mismatch = "candidate manifest"
            elif receipt["verified_artifacts"] != checked:
                mismatch = "live artifact set"

            # An exact retry after the reviewed decision won is idempotent even
            # though the receipt predecessor is now one decision behind current.
            is_exact_retry = bool(
                terminal is not None
                and terminal.get("review_digest") == receipt["record_digest"]
                and terminal["manifest_digest"] == manifest_digest
            )
            if not is_exact_retry:
                if receipt["predecessor_decision_digest"] != ledger.current_decision_digest:
                    mismatch = "accepted predecessor"
                elif receipt["initial"] != (terminal is None):
                    mismatch = "initial channel state"
                comparison = _recompute_receipt_comparison(
                    root, safe_name, receipt, manifest_path, parsed
                )
                if mismatch is None and digest_json(comparison) != receipt["comparison_digest"]:
                    mismatch = "recorded comparison"
            else:
                comparison = _recompute_receipt_comparison(
                    root, safe_name, receipt, manifest_path, parsed
                )
                if digest_json(comparison) != receipt["comparison_digest"]:
                    mismatch = "recorded comparison"
            if mismatch is not None:
                raise BaselineError(
                    f"release changed since this review ({mismatch} no longer matches)"
                )
        elif terminal is not None and terminal["project"] != parsed["project"]:
            raise BaselineError("candidate project does not match the current baseline project")

        manifest_hex = _digest_hex(manifest_digest, label="manifest digest")
        stored_manifest = root / "manifests" / f"{manifest_hex}.json"
        _write_immutable(stored_manifest, before)

        if terminal is not None and terminal["manifest_digest"] == manifest_digest:
            if review is not None and terminal.get("review_digest") != receipt["record_digest"]:
                raise BaselineError(
                    "release changed since this review (current acceptance used another review)"
                )
            return BaselineAcceptance(
                safe_name,
                terminal["record_digest"],
                manifest_digest,
                terminal,
                stored_manifest,
                False,
                checked,
            )

        channel = root / "ledgers" / safe_name
        if not channel.exists() and not channel.is_symlink():
            _ensure_directory(channel, label="baseline channel")
            _ensure_directory(channel / "decisions", label="baseline decisions directory")
        sequence = ledger.decisions + 1
        payload: dict[str, Any] = {
            "schema": (
                "modelbake.baseline.decision.v2"
                if receipt is not None
                else "modelbake.baseline.decision.v1"
            ),
            "name": safe_name,
            "sequence": sequence,
            "predecessor_decision_digest": ledger.current_decision_digest,
            "manifest_digest": manifest_digest,
            "manifest_record": f"manifests/{manifest_hex}.json",
            "run_id": parsed["run_id"],
            "project": parsed["project"],
            "status": parsed["status"],
            "accepted_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
        }
        if receipt is not None and receipt_bytes is not None:
            review_hex = _digest_hex(receipt["record_digest"], label="review digest")
            stored_review = root / "reviews" / f"{review_hex}.json"
            _write_immutable(stored_review, receipt_bytes)
            payload["review_digest"] = receipt["record_digest"]
            payload["review_record"] = f"reviews/{review_hex}.json"
        if safe_note is not None:
            payload["note"] = safe_note
        decision_digest = digest_json(payload)
        decision = {**payload, "record_digest": decision_digest}
        decision_path = _decision_path(channel, sequence, decision_digest)
        created = _write_immutable(
            decision_path, canonical_json_bytes(decision) + b"\n"
        )
        if not created:
            raise BaselineCorruptionError("new decision path already exists")
        pointer = {
            "schema": "modelbake.baseline.current.v1",
            "name": safe_name,
            "decision_digest": decision_digest,
            "decision_record": str(decision_path.relative_to(root)),
            "manifest_digest": manifest_digest,
        }
        _replace_private(channel / "current.json", canonical_json_bytes(pointer) + b"\n")
        _verify_ledger_at(root, safe_name)
        return BaselineAcceptance(
            safe_name,
            decision_digest,
            manifest_digest,
            decision,
            stored_manifest,
            True,
            checked,
        )
