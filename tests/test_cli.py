from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from modelbake.cli import (
    EXIT_BUILD_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VERIFY_FAILED,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RECIPE = ROOT / "examples/modelbake.fixture.yaml"


def test_version_is_available_without_a_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "modelbake 0.1.1\n"


def test_plan_json_exposes_only_validated_graph(capsys) -> None:
    assert main(["plan", str(FIXTURE_RECIPE), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "modelbake/v0"
    assert payload["runner"]["adapter"] == "fixture"
    assert [node["kind"] for node in payload["nodes"]].count("smoke") == 3


def test_invalid_config_uses_usage_exit_code(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema: modelbake/v0\ncommand: whoami\n", encoding="utf-8")
    assert main(["plan", str(bad)]) == EXIT_USAGE
    assert "configuration error" in capsys.readouterr().err


def test_demo_build_status_report_and_digest_verification(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "run"
    cache_dir = tmp_path / "cache"
    assert (
        main(
            [
                "demo",
                "--recipe",
                str(FIXTURE_RECIPE),
                "--run-dir",
                str(run_dir),
                "--cache-dir",
                str(cache_dir),
            ]
        )
        == EXIT_OK
    )
    build_output = capsys.readouterr().out
    assert "no compatibility, quality" in build_output
    manifest = run_dir / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert {node["state"] for node in payload["nodes"]} == {
        "produced",
        "fixture_observed",
    }

    assert main(["status", str(manifest)]) == EXIT_OK
    assert "succeeded" in capsys.readouterr().out
    assert main(["report", str(manifest)]) == EXIT_OK
    report = capsys.readouterr().out
    assert "truth boundary" in report
    assert "deterministic-local-fixture" in report
    assert "fixture_observed" in report
    assert main(["verify", str(manifest), "--allow-artifact-root", str(cache_dir)]) == EXIT_OK
    assert "digest verified: 7 artifact(s)" in capsys.readouterr().out


def test_installed_style_demo_materializes_its_own_fixture(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["demo"]) == EXIT_OK
    capsys.readouterr()
    manifest = tmp_path / ".modelbake/demo/manifest.json"
    demo_recipe = tmp_path / ".modelbake/demo-input-v1/modelbake.fixture.yaml"
    assert manifest.is_file()
    assert demo_recipe.is_file()
    assert (
        main(
            [
                "verify",
                str(manifest),
                "--allow-artifact-root",
                str(tmp_path / ".modelbake/cache"),
            ]
        )
        == EXIT_OK
    )
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".modelbake/demo").stat().st_mode) == 0o700
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_tour_runs_both_releases_and_prints_the_diff(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".modelbake/tour"
    assert main(["tour", "--run-root", str(root)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "RELEASE DIFF" in output
    assert "quantize-q8" in output
    assert "added" in output
    assert "Fixture only" in output
    assert (root / "accepted/manifest.json").is_file()
    assert (root / "candidate/manifest.json").is_file()


def test_baseline_cli_accepts_verifies_loads_and_compares(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / ".modelbake/cache"
    first = tmp_path / ".modelbake/first"
    second = tmp_path / ".modelbake/second"
    store = tmp_path / ".modelbake/baselines"
    assert main(["demo", "--story", "baseline", "--run-dir", str(first), "--cache-dir", str(cache)]) == EXIT_OK
    assert main(["demo", "--story", "candidate", "--run-dir", str(second), "--cache-dir", str(cache)]) == EXIT_OK
    capsys.readouterr()

    review = first / "review.json"
    assert main([
        "baseline",
        "review",
        str(first / "manifest.json"),
        "--store",
        str(store),
        "--name",
        "release",
        "--review",
        str(review),
        "--allow-artifact-root",
        str(cache),
        "--initial",
        "--json",
    ]) == EXIT_OK
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["initial"] is True
    assert reviewed["verified_artifacts"] == 4
    assert review.is_file()

    accept_args = [
        "baseline",
        "accept",
        str(first / "manifest.json"),
        "--store",
        str(store),
        "--name",
        "release",
        "--allow-artifact-root",
        str(cache),
        "--note",
        "fixture accepted",
        "--review",
        str(review),
        "--json",
    ]
    assert main(accept_args) == EXIT_OK
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["created"] is True
    assert accepted["verified_artifacts"] == 4

    assert main(["baseline", "current", "--store", str(store), "--name", "release", "--json"]) == EXIT_OK
    current = json.loads(capsys.readouterr().out)
    assert current["run_id"] == accepted["run_id"]
    assert Path(current["manifest_path"]).is_file()

    assert main([
        "baseline",
        "check",
        str(first / "manifest.json"),
        "--store",
        str(store),
        "--name",
        "release",
        "--allow-artifact-root",
        str(cache),
        "--json",
    ]) == EXIT_OK
    checked = json.loads(capsys.readouterr().out)
    assert checked["current"] is True
    assert checked["verified_artifacts"] == 4

    assert main(["baseline", "verify", "--store", str(store), "--name", "release"]) == EXIT_OK
    assert "live artifacts were not re-read" in capsys.readouterr().out

    assert main([
        "baseline",
        "compare",
        str(second / "manifest.json"),
        "--store",
        str(store),
        "--name",
        "release",
        "--json",
    ]) == EXIT_OK
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["before"]["run_id"] == accepted["run_id"]
    assert comparison["after"]["run_id"] != accepted["run_id"]
    assert comparison["counts"]["added"] == 2
    assert comparison["counts"]["reused_from_cache"] == 4

    candidate_review = second / "review.json"
    assert main([
        "baseline", "review", str(second / "manifest.json"),
        "--store", str(store), "--name", "release",
        "--review", str(candidate_review),
        "--allow-artifact-root", str(cache), "--json",
    ]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["verified_artifacts"] == 6
    capsys.readouterr()
    assert main([
        "baseline", "accept", str(second / "manifest.json"),
        "--store", str(store), "--name", "release",
        "--review", str(candidate_review),
        "--allow-artifact-root", str(cache), "--json",
    ]) == EXIT_OK
    capsys.readouterr()
    assert main([
        "baseline", "check", str(second / "manifest.json"),
        "--store", str(store), "--name", "release",
        "--allow-artifact-root", str(cache), "--json",
    ]) == EXIT_OK


def test_cli_baseline_accept_requires_review_receipt(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main([
            "baseline", "accept", str(tmp_path / "manifest.json"),
            "--store", str(tmp_path / "store"),
            "--name", "release",
            "--allow-artifact-root", str(tmp_path),
        ])


def test_tampered_artifact_fails_verification(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "run"
    cache_dir = tmp_path / "cache"
    assert (
        main(
            [
                "build",
                str(FIXTURE_RECIPE),
                "--run-dir",
                str(run_dir),
                "--cache-dir",
                str(cache_dir),
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()
    manifest = run_dir / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = Path(data["nodes"][1]["outputs"]["artifact"])
    artifact.write_bytes(b"tampered")

    assert (
        main(["verify", str(manifest), "--allow-artifact-root", str(cache_dir)])
        == EXIT_VERIFY_FAILED
    )
    assert "digest mismatch" in capsys.readouterr().err


def test_failed_manifest_status_uses_build_failure_exit(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "modelbake.run.v1",
                "run_id": "failed",
                "project": "failed-test",
                "status": "failed",
                "recipe_digest": "sha256:" + "1" * 64,
                "source_digest": "sha256:" + "2" * 64,
                "runner": {},
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "nodes": [
                    {
                        "id": "source",
                        "state": "failed",
                        "outputs": {},
                        "output_digests": {},
                        "command": [],
                        "observations": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["status", str(manifest)]) == EXIT_BUILD_FAILED


def test_relative_run_and_cache_paths_still_verify_from_another_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert (
        main(
            [
                "demo",
                "--recipe",
                str(FIXTURE_RECIPE),
                "--run-dir",
                "runs/demo",
                "--cache-dir",
                "cache",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()
    manifest = workspace / "runs/demo/manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert Path(payload["run_dir"]).is_absolute()
    assert all(
        Path(output).is_absolute()
        for node in payload["nodes"]
        for output in node["outputs"].values()
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert (
        main(
            [
                "verify",
                str(manifest),
                "--allow-artifact-root",
                str(workspace / "cache"),
            ]
        )
        == EXIT_OK
    )
    assert "digest verified: 7 artifact(s)" in capsys.readouterr().out


def test_llamacpp_adapter_records_exact_argv_revision_versions_and_capped_logs(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "model.safetensors").write_bytes(b"test-only-safe-source")
    convert = tmp_path / "convert.py"
    convert.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "out = Path(sys.argv[sys.argv.index('--outfile') + 1])\n"
        "out.write_bytes(b'fake-gguf-f16')\n",
        encoding="utf-8",
    )
    quantize = tmp_path / "llama-quantize"
    quantize.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('quantize test v1')\n"
        "else:\n"
        "    Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes() + sys.argv[3].encode())\n",
        encoding="utf-8",
    )
    runner = tmp_path / "llama-cli"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('runner test v1')\n"
        "else:\n"
        "    print('x' * 2048)\n",
        encoding="utf-8",
    )
    quantize.chmod(0o755)
    runner.chmod(0o755)
    recipe = tmp_path / "modelbake.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema": "modelbake/v0",
                "project": "llamacpp-argv-test",
                "source": {
                    "kind": "local",
                    "path": "source",
                    "architecture": "llama",
                    "format": "safetensors",
                },
                "targets": [{"name": "q4", "quantization": "Q4_0"}],
                "smoke": {"prompt": "hello; touch forbidden", "max_tokens": 2},
                "runner": {
                    "adapter": "llamacpp",
                    "name": "fake-explicit-runner",
                    "timeout_seconds": 10,
                    "max_log_bytes": 256,
                    "tools": {
                        "python": sys.executable,
                        "convert": str(convert),
                        "quantize": str(quantize),
                        "runner": str(runner),
                        "revision": "test-pinned-revision",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    assert (
        main(
            [
                "build",
                str(recipe),
                "--run-dir",
                str(run_dir),
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()
    nodes = {
        node["id"]: node
        for node in json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["nodes"]
    }
    assert nodes["convert-f16-base"]["command"][:2] == [
        str(Path(sys.executable).absolute()),
        str(convert),
    ]
    assert nodes["quantize-q4"]["command"][-1] == "Q4_0"
    assert nodes["smoke-q4"]["command"][-3:] == ["--seed", "0", "--single-turn"]
    assert nodes["smoke-q4"]["command"][4].startswith("<redacted:sha256:")
    assert "hello; touch forbidden" not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert nodes["smoke-q4"]["observations"]["revision"] == "test-pinned-revision"
    assert nodes["smoke-q4"]["observations"]["tool_versions"]["runner"] == "runner test v1"
    assert nodes["smoke-q4"]["observations"]["log_bytes_recorded"] <= 256
    assert nodes["smoke-q4"]["observations"]["log_truncated"] is True
    assert nodes["smoke-q4"]["observations"]["timeout_seconds"] == 10
    assert Path(nodes["smoke-q4"]["log_path"]).is_file()
    if os.name == "posix":
        assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(nodes["smoke-q4"]["log_path"]).stat().st_mode) == 0o600
    assert not (tmp_path / "forbidden").exists()


def test_fixture_observation_fills_the_supported_token_upper_bound(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "weights.fixture").write_bytes(b"fixture-token-bound")
    recipe = tmp_path / "modelbake.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema": "modelbake/v0",
                "project": "fixture-token-bound",
                "source": {
                    "kind": "local",
                    "path": "source",
                    "architecture": "llama",
                    "format": "fixture",
                },
                "targets": [{"name": "f16", "quantization": "F16"}],
                "smoke": {"prompt": "bounded fixture", "max_tokens": 128},
                "runner": {
                    "adapter": "fixture",
                    "name": "fixture",
                    "timeout_seconds": 10,
                    "max_log_bytes": 4096,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    assert main(
        [
            "build",
            str(recipe),
            "--run-dir",
            str(run_dir),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    ) == EXIT_OK
    capsys.readouterr()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    smoke = next(node for node in manifest["nodes"] if node["kind"] == "smoke")
    observation = json.loads(Path(smoke["outputs"]["observation"]).read_text(encoding="utf-8"))
    assert len(observation["observed_tokens"]) == 128
    assert all(len(token) == 8 for token in observation["observed_tokens"])
