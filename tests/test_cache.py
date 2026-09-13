from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from modelbake.cache import CacheCorruptionError, CacheError, LocalCache
from modelbake.hashing import tree_digest


def _key(character: str = "a") -> str:
    return "sha256:" + character * 64


def _write_entry(cache: LocalCache, key: str, manifest: object) -> Path:
    entry = cache.entry_path(key)
    (entry / "outputs").mkdir(parents=True)
    (entry / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return entry


def _manifest(key: str, digest: str) -> dict[str, object]:
    return {
        "schema": "modelbake.cache.v1",
        "key": key,
        "outputs": {
            "artifact": {"path": "outputs/artifact", "digest": digest},
        },
        "metadata": {
            "state": "produced",
            "command": ["test-adapter"],
            "observations": {},
        },
    }


def _permission_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_load_rejects_non_object_json_manifest(tmp_path: Path) -> None:
    cache = LocalCache(tmp_path / "cache")
    entry = cache.entry_path(_key())
    entry.mkdir()
    (entry / "manifest.json").write_text("[]", encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="manifest is not an object"):
        cache.load(_key())


def test_load_normalizes_non_utf8_manifest(tmp_path: Path) -> None:
    cache = LocalCache(tmp_path / "cache")
    entry = cache.entry_path(_key())
    entry.mkdir()
    (entry / "manifest.json").write_bytes(b"\xff\xfe")

    with pytest.raises(CacheCorruptionError, match="cannot read cache manifest"):
        cache.load(_key())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are not available")
def test_load_normalizes_special_cached_output(tmp_path: Path) -> None:
    cache = LocalCache(tmp_path / "cache")
    key = _key()
    entry = _write_entry(cache, key, _manifest(key, "sha256:" + "0" * 64))
    os.mkfifo(entry / "outputs/artifact")

    with pytest.raises(CacheCorruptionError, match="cannot verify cached output"):
        cache.load(key)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_load_normalizes_unreadable_cached_output(tmp_path: Path) -> None:
    cache = LocalCache(tmp_path / "cache")
    key = _key()
    entry = cache.entry_path(key)
    output = entry / "outputs/artifact"
    output.parent.mkdir(parents=True)
    output.write_text("private", encoding="utf-8")
    digest = tree_digest(output)
    (entry / "manifest.json").write_text(
        json.dumps(_manifest(key, digest)), encoding="utf-8"
    )
    output.chmod(0)
    if os.access(output, os.R_OK):
        output.chmod(0o600)
        pytest.skip("test process can read mode-000 files")

    try:
        with pytest.raises(CacheCorruptionError, match="cannot verify cached output"):
            cache.load(key)
    finally:
        output.chmod(0o600)


def test_commit_makes_owned_cache_data_private_without_chmodding_existing_root(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "shared-cache-root"
    cache_root.mkdir(mode=0o755)
    cache = LocalCache(cache_root)
    source_root = tmp_path / "adapter-output"
    source_root.mkdir()
    artifact = source_root / "artifact.bin"
    artifact.write_bytes(b"artifact")
    artifact.chmod(0o755)

    result = cache.commit(
        _key(),
        {"artifact": artifact},
        {"state": "produced", "command": ["adapter"], "observations": {}},
        allowed_root=source_root,
    )

    entry = cache.entry_path(_key())
    assert result.created is True
    assert _permission_bits(cache_root) == 0o755
    assert _permission_bits(cache.entries_dir) == 0o700
    assert _permission_bits(entry) == 0o700
    assert _permission_bits(entry / "outputs") == 0o700
    assert _permission_bits(entry / "manifest.json") == 0o600
    assert _permission_bits(result.entry.outputs["artifact"]) == 0o700


def test_new_cache_root_is_private(tmp_path: Path) -> None:
    cache = LocalCache(tmp_path / "cache")

    assert _permission_bits(cache.root) == 0o700
    assert _permission_bits(cache.entries_dir) == 0o700


def test_cache_rejects_symlinked_entries_directory(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (cache_root / "entries").symlink_to(unrelated, target_is_directory=True)

    with pytest.raises(CacheError, match="cache entries path is not a real directory"):
        LocalCache(cache_root)


def test_cache_rejects_nondirectory_entries_path(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "entries").write_text("not a directory", encoding="utf-8")

    with pytest.raises(CacheError, match="cache entries path is not a real directory"):
        LocalCache(cache_root)
