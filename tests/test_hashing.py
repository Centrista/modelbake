from __future__ import annotations

import math
import os

import pytest

from modelbake.hashing import (
    HashingError,
    canonical_json_bytes,
    digest_json,
    python_source_digest,
    tree_digest,
)


def test_canonical_json_is_key_order_independent() -> None:
    left = {"z": [3, {"b": True, "a": None}], "a": "héllo"}
    right = {"a": "héllo", "z": [3, {"a": None, "b": True}]}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert digest_json(left) == digest_json(right)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {1: "bad"}, object()])
def test_canonical_json_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(HashingError):
        canonical_json_bytes(value)


def test_tree_digest_covers_names_content_modes_and_symlink_target(tmp_path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    payload = tree / "payload.bin"
    payload.write_bytes(b"one")
    link = tree / "alias"
    link.symlink_to("payload.bin")
    first = tree_digest(tree)

    payload.write_bytes(b"two")
    assert tree_digest(tree) != first
    payload.write_bytes(b"one")
    assert tree_digest(tree) == first

    link.unlink()
    link.symlink_to("elsewhere.bin")
    assert tree_digest(tree) != first

    link.unlink()
    link.symlink_to("payload.bin")
    os.chmod(payload, 0o755)
    assert tree_digest(tree) != first


def test_tree_digest_rejects_special_files(tmp_path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(HashingError, match="special filesystem"):
        tree_digest(fifo)


def test_python_source_digest_covers_only_regular_python_sources(tmp_path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    source = package / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (package / "data.txt").write_text("ignored", encoding="utf-8")
    initial = python_source_digest(package)
    (package / "data.txt").write_text("still ignored", encoding="utf-8")
    assert python_source_digest(package) == initial
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert python_source_digest(package) != initial


def test_python_source_digest_rejects_symlinked_source(tmp_path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (package / "linked.py").symlink_to(target)
    with pytest.raises(HashingError, match="not a regular file"):
        python_source_digest(package)
