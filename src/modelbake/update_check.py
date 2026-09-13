"""Small, privacy-bounded update notices for interactive CLI use."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

_LATEST_URL = "https://modelbake.dev/latest.json"
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_NETWORK_TIMEOUT_SECONDS = 0.6
_MAX_RESPONSE_BYTES = 4096
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_CACHE_SCHEMA = "modelbake.update-cache.v1"
_REMOTE_SCHEMA = "modelbake.update.v1"


def _version_key(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _default_cache_path(environment: Mapping[str, str]) -> Path:
    configured = environment.get("XDG_CACHE_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "modelbake" / "update-check.json"


def _read_cache(path: Path) -> dict[str, object] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _CACHE_SCHEMA:
        return None
    checked_at = payload.get("checked_at")
    latest = payload.get("latest_version")
    if not isinstance(checked_at, (int, float)) or not isinstance(latest, str):
        return None
    if _version_key(latest) is None:
        return None
    return payload


def _write_cache(path: Path, checked_at: float, latest: str) -> None:
    parent = path.parent
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode):
            return
        descriptor, name = tempfile.mkstemp(prefix=".update-", suffix=".json", dir=parent)
        temporary = Path(name)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        content = json.dumps(
            {
                "schema": _CACHE_SCHEMA,
                "checked_at": checked_at,
                "latest_version": latest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _fetch_latest_version(timeout: float) -> str | None:
    request = urllib.request.Request(
        _LATEST_URL,
        headers={"User-Agent": "ModelBake update check"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError):
        return None
    if len(raw) > _MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _REMOTE_SCHEMA:
        return None
    latest = payload.get("version")
    return latest if isinstance(latest, str) and _version_key(latest) is not None else None


def newer_version(
    current: str,
    *,
    cache_path: Path,
    now: float,
    fetch_latest: Callable[[float], str | None] = _fetch_latest_version,
) -> str | None:
    """Return a newer stable version, using a one-day local cache."""

    current_key = _version_key(current)
    if current_key is None:
        return None
    cached = _read_cache(cache_path)
    latest: str | None = None
    if cached is not None:
        latest = str(cached["latest_version"])
        if now - float(cached["checked_at"]) < _CHECK_INTERVAL_SECONDS:
            latest_key = _version_key(latest)
            return latest if latest_key is not None and latest_key > current_key else None

    observed = fetch_latest(_NETWORK_TIMEOUT_SECONDS)
    if observed is not None:
        latest = observed
    if latest is None:
        latest = current
    _write_cache(cache_path, now, latest)
    latest_key = _version_key(latest)
    return latest if latest_key is not None and latest_key > current_key else None


def maybe_print_update_notice(
    current: str,
    *,
    stream: TextIO = sys.stderr,
    environment: Mapping[str, str] | None = None,
    cache_path: Path | None = None,
    now: float | None = None,
    fetch_latest: Callable[[float], str | None] = _fetch_latest_version,
) -> None:
    """Print one non-blocking notice for interactive users when an update exists."""

    env = os.environ if environment is None else environment
    disabled = env.get("MODELBAKE_NO_UPDATE_CHECK", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if disabled or env.get("CI") or not stream.isatty():
        return
    try:
        latest = newer_version(
            current,
            cache_path=cache_path or _default_cache_path(env),
            now=time.time() if now is None else now,
            fetch_latest=fetch_latest,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        # An optional update notice must never block a build or hide its result.
        return
    if latest is None:
        return
    wheel = (
        "https://github.com/Centrista/modelbake/releases/download/"
        f"v{latest}/modelbake_ai-{latest}-py3-none-any.whl"
    )
    print(
        f"ModelBake {latest} is available. Update with: "
        f"python -m pip install --upgrade {wheel}",
        file=stream,
    )
