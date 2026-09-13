from __future__ import annotations

import json
from pathlib import Path

from modelbake.cli import EXIT_OK, main
from modelbake.compare import compare_manifests, render_comparison


def _node(node_id: str, *, state: str, digest: str, observation: str) -> dict:
    return {
        "id": node_id,
        "kind": "smoke",
        "state": state,
        "cache_key": "sha256:" + "3" * 64,
        "input_digests": {"source:artifact": "sha256:" + "4" * 64},
        "outputs": {"observation": "/tmp/modelbake-test/observation"},
        "output_digests": {"observation": digest},
        "command": ["runner", "--single-turn"],
        "observations": {"record": observation},
    }


def _manifest(run_id: str, node: dict, *, source: str = "2", recipe: str = "1") -> dict:
    return {
        "schema": "modelbake.run.v1",
        "run_id": run_id,
        "project": "compare-test",
        "status": "succeeded",
        "recipe_digest": "sha256:" + recipe * 64,
        "source_digest": "sha256:" + source * 64,
        "runner": {"declared": {"name": "runner"}, "observed": {"os": "test"}},
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "nodes": [node],
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_compare_distinguishes_cache_reuse_from_fresh_execution(tmp_path: Path) -> None:
    digest = "sha256:" + "5" * 64
    before = _write(
        tmp_path / "before.json",
        _manifest("before", _node("smoke", state="executed_on_runner", digest=digest, observation="fresh")),
    )
    cached = _node("smoke", state="cache_hit", digest=digest, observation="fresh")
    cached["observations"]["cached_state"] = "executed_on_runner"
    after = _write(tmp_path / "after.json", _manifest("after", cached))

    comparison = compare_manifests(before, after)
    assert comparison["nodes"][0]["change"] == "reused_from_cache"
    assert comparison["nodes"][0]["recorded_output_digests"] == "same"
    assert comparison["promotion_decision"] == "not_inferred"


def test_compare_separates_evidence_change_from_artifact_change(tmp_path: Path) -> None:
    digest = "sha256:" + "5" * 64
    before = _write(
        tmp_path / "before.json",
        _manifest("before", _node("smoke", state="executed_on_runner", digest=digest, observation="a")),
    )
    evidence_after = _write(
        tmp_path / "evidence.json",
        _manifest("evidence", _node("smoke", state="executed_on_runner", digest=digest, observation="b")),
    )
    artifact_after = _write(
        tmp_path / "artifact.json",
        _manifest(
            "artifact",
            _node("smoke", state="executed_on_runner", digest="sha256:" + "6" * 64, observation="b"),
        ),
    )

    assert compare_manifests(before, evidence_after)["nodes"][0]["change"] == (
        "evidence_changed_same_recorded_output"
    )
    assert compare_manifests(before, artifact_after)["nodes"][0]["change"] == (
        "recorded_output_changed"
    )


def test_compare_never_claims_current_bytes_were_verified(tmp_path: Path) -> None:
    digest = "sha256:" + "5" * 64
    before_node = _node("smoke", state="executed_on_runner", digest=digest, observation="a")
    after_node = _node("smoke", state="executed_on_runner", digest=digest, observation="a")
    before_node["outputs"] = {"observation": "/definitely/missing/before"}
    after_node["outputs"] = {"observation": "/definitely/missing/after"}
    before = _write(tmp_path / "before.json", _manifest("before", before_node))
    after = _write(tmp_path / "after.json", _manifest("after", after_node))

    comparison = compare_manifests(before, after)
    assert comparison["nodes"][0]["recorded_output_digests"] == "same"
    rendered = render_comparison(comparison)
    assert "recorded output digests same" in rendered
    assert "bytes identical" not in rendered


def test_compare_renders_terminal_markdown_and_json_cli(tmp_path: Path, capsys) -> None:
    digest = "sha256:" + "5" * 64
    before = _write(
        tmp_path / "before.json",
        _manifest("before", _node("smoke", state="executed_on_runner", digest=digest, observation="a")),
    )
    after = _write(
        tmp_path / "after.json",
        _manifest("after", _node("smoke", state="executed_on_runner", digest=digest, observation="a")),
    )
    comparison = compare_manifests(before, after)
    assert "truth boundary" in render_comparison(comparison)
    assert "## ModelBake release comparison" in render_comparison(comparison, markdown=True)

    assert main(["compare", str(before), str(after), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "modelbake.compare.v1"
