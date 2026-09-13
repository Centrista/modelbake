from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelbake.manifest import ManifestError, load_manifest, verify_manifest


def _manifest(*, status: str = "succeeded", nodes: list[dict] | None = None) -> dict:
    return {
        "schema": "modelbake.run.v1",
        "run_id": "manifest-test",
        "project": "manifest-test",
        "status": status,
        "recipe_digest": "sha256:" + "1" * 64,
        "source_digest": "sha256:" + "2" * 64,
        "runner": {},
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "nodes": [] if nodes is None else nodes,
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_succeeded_manifest_cannot_be_artifact_free(tmp_path: Path) -> None:
    path = _write(tmp_path / "manifest.json", _manifest())
    with pytest.raises(ManifestError, match="at least one artifact"):
        load_manifest(path)


def test_manifest_rejects_nonfinite_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(_manifest(nodes=[])).replace('"runner": {}', '"runner": {"bad": NaN}'),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="non-finite"):
        load_manifest(path)


def test_output_and_digest_keys_must_be_bijective(tmp_path: Path) -> None:
    node = {
        "id": "source",
        "state": "produced",
        "outputs": {"source": str(tmp_path / "source")},
        "output_digests": {},
        "command": [],
        "observations": {},
    }
    path = _write(tmp_path / "manifest.json", _manifest(nodes=[node]))
    with pytest.raises(ManifestError, match="identical keys"):
        load_manifest(path)


def test_verifier_rejects_outputs_without_a_safely_bounded_common_root(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"bytes")
    node = {
        "id": "source",
        "state": "produced",
        "outputs": {"source": str(artifact)},
        "output_digests": {"source": "sha256:" + "0" * 64},
        "command": [],
        "observations": {},
    }
    path = _write(tmp_path / "manifest.json", _manifest(nodes=[node]))
    result = verify_manifest(path, allowed_roots=(tmp_path / "different",))
    assert result.ok is False
    assert "outside allowed artifact roots" in result.failures[0]


def test_verifier_requires_a_caller_selected_trust_boundary(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"bytes")
    node = {
        "id": "source",
        "state": "produced",
        "outputs": {"source": str(artifact)},
        "output_digests": {"source": "sha256:" + "0" * 64},
        "command": [],
        "observations": {},
    }
    path = _write(tmp_path / "manifest.json", _manifest(nodes=[node]))

    with pytest.raises(ManifestError, match="explicit trusted artifact root"):
        verify_manifest(path, allowed_roots=())
