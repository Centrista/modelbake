from __future__ import annotations

import io
import json
import stat
from pathlib import Path

from modelbake.update_check import maybe_print_update_notice, newer_version


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_newer_version_fetches_once_then_uses_private_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "update.json"
    calls: list[float] = []

    def fetch(timeout: float) -> str:
        calls.append(timeout)
        return "0.2.0"

    assert newer_version(
        "0.1.0", cache_path=cache, now=1000, fetch_latest=fetch
    ) == "0.2.0"
    assert newer_version(
        "0.1.0",
        cache_path=cache,
        now=1001,
        fetch_latest=lambda _timeout: (_ for _ in ()).throw(AssertionError()),
    ) == "0.2.0"
    assert len(calls) == 1
    assert json.loads(cache.read_text())["latest_version"] == "0.2.0"
    if hasattr(stat, "S_IMODE"):
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_notice_is_short_and_does_not_install_anything(tmp_path: Path) -> None:
    stream = _TTY()
    maybe_print_update_notice(
        "0.1.0",
        stream=stream,
        environment={},
        cache_path=tmp_path / "update.json",
        now=1000,
        fetch_latest=lambda _timeout: "0.1.1",
    )
    assert stream.getvalue() == (
        "ModelBake 0.1.1 is available. Update with: python -m pip install --upgrade "
        "https://modelbake.dev/downloads/modelbake_ai-0.1.1-py3-none-any.whl\n"
    )


def test_notice_skips_ci_disabled_and_noninteractive_runs(tmp_path: Path) -> None:
    for environment, stream in (
        ({"CI": "true"}, _TTY()),
        ({"MODELBAKE_NO_UPDATE_CHECK": "1"}, _TTY()),
        ({}, io.StringIO()),
    ):
        calls: list[float] = []
        maybe_print_update_notice(
            "0.1.0",
            stream=stream,
            environment=environment,
            cache_path=tmp_path / f"update-{len(calls)}.json",
            now=1000,
            fetch_latest=lambda timeout, target=calls: target.append(timeout) or "0.1.1",
        )
        assert calls == []
        assert stream.getvalue() == ""


def test_invalid_or_older_versions_are_ignored(tmp_path: Path) -> None:
    for latest in ("0.1.0", "0.0.9", "latest", "1.0.0rc1"):
        assert newer_version(
            "0.1.0",
            cache_path=tmp_path / latest.replace("/", "-") / "update.json",
            now=1000,
            fetch_latest=lambda _timeout, value=latest: value,
        ) is None


def test_notice_never_breaks_the_cli_when_the_check_fails(tmp_path: Path) -> None:
    stream = _TTY()

    def fail(_timeout: float) -> str:
        raise RuntimeError("network helper failed")

    maybe_print_update_notice(
        "0.1.0",
        stream=stream,
        environment={},
        cache_path=tmp_path / "update.json",
        now=1000,
        fetch_latest=fail,
    )
    assert stream.getvalue() == ""
