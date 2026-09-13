from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from modelbake.adapters.llamacpp import LlamaCppAdapter
from modelbake.baseline import accept_baseline, review_baseline
from modelbake.hashing import python_source_digest, tree_digest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_public_evidence.py"
SPEC = importlib.util.spec_from_file_location("generate_public_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
public_evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = public_evidence
previous_bytecode_setting = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(public_evidence)
finally:
    sys.dont_write_bytecode = previous_bytecode_setting

PROMPT_DIGEST = "sha256:" + "2" * 64
NODE_SPECS = (
    ("source", "source", "produced"),
    ("convert-f16-base", "convert", "produced"),
    ("quantize-q4_0", "quantize", "produced"),
    ("quantize-q8_0", "quantize", "produced"),
    ("smoke-q4_0", "smoke", "executed_on_runner"),
    ("smoke-q8_0", "smoke", "executed_on_runner"),
)


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_toolchain(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    checkout = tmp_path / "llama.cpp"
    checkout.mkdir()
    _git("init", "-q", cwd=checkout)
    _git("config", "user.email", "modelbake-tests@example.invalid", cwd=checkout)
    _git("config", "user.name", "ModelBake Tests", cwd=checkout)
    convert = checkout / "convert_hf_to_gguf.py"
    convert.write_text("# deterministic test converter\n", encoding="utf-8")
    quantize = checkout / "llama-quantize"
    runner = checkout / "llama-cli"
    executable = "#!/bin/sh\nprintf '%s\\n' 'llama.cpp-test'\n"
    for path in (quantize, runner):
        path.write_text(executable, encoding="utf-8")
        path.chmod(0o755)
    _git("add", ".", cwd=checkout)
    _git("commit", "-qm", "test toolchain", cwd=checkout)
    revision = _git("rev-parse", "HEAD", cwd=checkout)
    tools = {
        "python": str(Path(sys.executable).resolve()),
        "convert": str(convert.resolve()),
        "quantize": str(quantize.resolve()),
        "runner": str(runner.resolve()),
        "revision": revision,
    }
    declared = {
        "adapter": "llamacpp",
        "name": "llama.cpp-test",
        "timeout_seconds": 60,
        "max_log_bytes": 65_536,
        "tools": tools,
    }
    # The first status can refresh Git's index stat cache. Record the stable pass.
    LlamaCppAdapter(declared).fingerprint()
    return declared, LlamaCppAdapter(declared).fingerprint()


def _write_wheel(path: Path, package_root: Path, *, stale: bool = False) -> str:
    sources = {
        "__init__.py": '__version__ = "1.2.3"\n',
        "core.py": "VALUE = 2\n" if stale else "VALUE = 1\n",
    }
    package_root.mkdir()
    for name, content in sources.items():
        (package_root / name).write_text(content, encoding="utf-8")
    source_digest = python_source_digest(package_root)
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, content in sources.items():
            archive.writestr(f"modelbake/{name}", content)
        archive.writestr(
            "modelbake_ai-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: modelbake-ai\nVersion: 1.2.3\n",
        )
        archive.writestr(
            "modelbake_ai-1.2.3.dist-info/WHEEL",
            "Wheel-Version: 1.0\nTag: py3-none-any\n",
        )
    return source_digest


def _base_observations(fingerprint: dict[str, Any]) -> dict[str, Any]:
    return {
        "adapter": fingerprint["adapter"],
        "revision": fingerprint["revision"],
        "runner": fingerprint["runner_name"],
        "tool_versions": fingerprint["versions"],
        "tool_digests": {
            name: value["resolved_file_digest"]
            for name, value in fingerprint["tools"].items()
        },
        "python_environment": fingerprint["python_environment"],
        "tool_source_repository": fingerprint["tool_source_repository"],
    }


def _command(kind: str, node_id: str, tools: dict[str, str]) -> list[str]:
    if kind == "source":
        return []
    if kind == "convert":
        return [
            tools["python"],
            tools["convert"],
            "/private/source",
            "--outfile",
            "/private/model-F16.gguf",
            "--outtype",
            "f16",
        ]
    if kind == "quantize":
        quantization = "Q4_0" if "q4" in node_id else "Q8_0"
        return [
            tools["quantize"],
            "/private/input.gguf",
            "/private/output.gguf",
            quantization,
        ]
    return [
        tools["runner"],
        "-m",
        "/private/model.gguf",
        "-p",
        f"<redacted:{PROMPT_DIGEST}>",
        "-n",
        "8",
        "--seed",
        "0",
        "--single-turn",
    ]


def _manifest(
    artifact_root: Path,
    code_digest: str,
    declared: dict[str, Any],
    fingerprint: dict[str, Any],
    *,
    warm: bool,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    output_digests = {
        node_id: tree_digest(artifact_root / f"{node_id}.bin")
        for node_id, _, _ in NODE_SPECS
    }
    parent_by_node = {
        "convert-f16-base": "source",
        "quantize-q4_0": "convert-f16-base",
        "quantize-q8_0": "convert-f16-base",
        "smoke-q4_0": "quantize-q4_0",
        "smoke-q8_0": "quantize-q8_0",
    }
    for index, (node_id, kind, cold_state) in enumerate(NODE_SPECS):
        artifact = artifact_root / f"{node_id}.bin"
        observations = _base_observations(fingerprint)
        if kind != "source":
            stdout = "llama-cli produced test tokens\n" if kind == "smoke" else ""
            observations.update(
                {
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "log_bytes_recorded": len(stdout.encode()),
                    "log_truncated": False,
                    "timed_out": False,
                    "timeout_seconds": 60,
                }
            )
        observations["operation"] = {
            "source": "local_source_copied",
            "convert": "converter_executed",
            "quantize": "quantizer_executed",
            "smoke": "artifact_executed_on_named_runner",
        }[kind]
        if kind == "quantize":
            observations["quantization"] = "Q4_0" if "q4" in node_id else "Q8_0"
        if kind == "smoke":
            observations["prompt_digest"] = PROMPT_DIGEST
        if warm:
            observations["cached_state"] = cold_state
        output_name = (
            "source"
            if kind == "source"
            else "observation"
            if kind == "smoke"
            else "artifact"
        )
        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "state": "cache_hit" if warm else cold_state,
                "cache_key": f"sha256:{index + 20:064x}",
                "input_digests": (
                    {}
                    if kind == "source"
                    else {
                        f"{parent_by_node[node_id]}:output": output_digests[
                            parent_by_node[node_id]
                        ]
                    }
                ),
                "outputs": {output_name: str(artifact)},
                "output_digests": {output_name: output_digests[node_id]},
                "command": _command(kind, node_id, declared["tools"]),
                "observations": observations,
                "duration_ms": 0,
                "log_path": None,
                "error": None,
            }
        )
    return {
        "schema": "modelbake.run.v1",
        "run_id": "warm-run" if warm else "cold-run",
        "project": "public-evidence-test",
        "status": "succeeded",
        "recipe_digest": "sha256:" + "4" * 64,
        "source_digest": "sha256:" + "5" * 64,
        "runner": {
            "declared": declared,
            "observed": {
                "schema": "modelbake.runner.v1",
                "os": "Darwin",
                "os_release": "25.5.0",
                "machine": "arm64",
                "python_implementation": "CPython",
                "python_version": "3.13.9",
                "byteorder": "little",
                "extra": {
                    "modelbake": {
                        "version": "1.2.3",
                        "python_source_digest": code_digest,
                    }
                },
            },
        },
        "started_at": "2026-09-13T00:00:00Z",
        "finished_at": "2026-09-13T00:00:01Z",
        "nodes": nodes,
        "claims": [],
        "exclusions": [],
    }


@pytest.fixture
def acceptance(tmp_path: Path) -> dict[str, Any]:
    declared, fingerprint = _write_toolchain(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    for index, (node_id, _, _) in enumerate(NODE_SPECS):
        artifact = artifact_root / f"{node_id}.bin"
        if node_id == "source":
            artifact.mkdir()
            (artifact / "config.json").write_bytes(b"config")
            weights = artifact / "weights"
            weights.mkdir()
            (weights / "model.bin").write_bytes(b"weights")
            (artifact / "not-counted.link").symlink_to("config.json")
        elif node_id.startswith(("convert-", "quantize-")):
            artifact.write_bytes(
                struct.pack("<4sIQQ", b"GGUF", 3, 1, 1)
                + f"artifact-{index}".encode()
            )
        elif node_id.startswith("smoke-"):
            artifact.write_text("llama-cli produced test tokens\n", encoding="utf-8")
        else:
            artifact.write_bytes(f"artifact-{index}".encode())
    wheel = tmp_path / "modelbake_ai-1.2.3-py3-none-any.whl"
    code_digest = _write_wheel(wheel, tmp_path / "package")
    sdist = tmp_path / "modelbake_ai-1.2.3.tar.gz"
    sdist.write_bytes(b"exact source distribution bytes")
    cold = _manifest(
        artifact_root, code_digest, declared, fingerprint, warm=False
    )
    warm = _manifest(
        artifact_root, code_digest, declared, fingerprint, warm=True
    )
    cold_path = tmp_path / "cold.json"
    warm_path = tmp_path / "warm.json"
    cold_path.write_text(json.dumps(cold), encoding="utf-8")
    warm_path.write_text(json.dumps(warm), encoding="utf-8")
    baseline_store = tmp_path / "baselines"
    baseline_name = "release"
    cold_review = tmp_path / "cold-review.json"
    warm_review = tmp_path / "warm-review.json"
    review_baseline(
        cold_path,
        baseline_store,
        baseline_name,
        (artifact_root,),
        cold_review,
        initial=True,
    )
    accept_baseline(
        cold_path,
        baseline_store,
        baseline_name,
        (artifact_root,),
        review=cold_review,
    )
    review_baseline(
        warm_path,
        baseline_store,
        baseline_name,
        (artifact_root,),
        warm_review,
    )
    accepted_warm = accept_baseline(
        warm_path,
        baseline_store,
        baseline_name,
        (artifact_root,),
        review=warm_review,
    )
    return {
        "artifact_root": artifact_root,
        "wheel": wheel,
        "sdist": sdist,
        "cold": cold,
        "warm": warm,
        "cold_path": cold_path,
        "warm_path": warm_path,
        "declared": declared,
        "fingerprint": fingerprint,
        "baseline_store": baseline_store,
        "baseline_name": baseline_name,
        "accepted_warm": accepted_warm,
    }


def _generate(acceptance: dict[str, Any]) -> dict[str, Any]:
    return public_evidence.generate_public_evidence(
        cold_manifest=acceptance["cold_path"],
        warm_manifest=acceptance["warm_path"],
        artifact_root=acceptance["artifact_root"],
        baseline_store=acceptance["baseline_store"],
        baseline_name=acceptance["baseline_name"],
        wheel=acceptance["wheel"],
        sdist=acceptance["sdist"],
    )


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return [value] if isinstance(value, str) else []


def test_generation_cli_requires_explicit_baseline_store_and_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="2"):
        public_evidence.main(
            [
                "--cold-manifest",
                "cold.json",
                "--warm-manifest",
                "warm.json",
                "--artifact-root",
                "artifacts",
                "--wheel",
                "release.whl",
                "--sdist",
                "release.tar.gz",
                "--output",
                "evidence.json",
            ]
        )
    error = capsys.readouterr().err
    assert "--baseline-store" in error
    assert "--baseline-name" in error


def test_generates_exact_sanitized_package_bound_evidence(
    acceptance: dict[str, Any], tmp_path: Path
) -> None:
    payload = _generate(acceptance)
    assert payload["schema"] == "modelbake.public-acceptance.v3"
    assert payload["release"]["version"] == "1.2.3"
    assert payload["cold_run"]["digest_verification"] == {"checked": 6, "failures": 0}
    assert payload["warm_run"]["states"] == {"cache_hit": 6}
    assert payload["acceptance"] == {
        "kind": "publisher-generated-cas-acceptance",
        "channel": acceptance["baseline_name"],
        "decision_digest": acceptance["accepted_warm"].decision_digest,
        "review_digest": acceptance["accepted_warm"].decision["review_digest"],
        "candidate_manifest_digest": acceptance["accepted_warm"].manifest_digest,
        "predecessor_decision_digest": acceptance["accepted_warm"].decision[
            "predecessor_decision_digest"
        ],
        "comparison_digest": json.loads(
            (acceptance["baseline_store"] / acceptance["accepted_warm"].decision["review_record"])
            .read_text(encoding="utf-8")
        )["comparison_digest"],
    }
    assert (
        payload["warm_run"]["manifest"]["modelbake_digest"]
        == payload["acceptance"]["candidate_manifest_digest"]
    )
    source = next(
        node for node in payload["cold_run"]["nodes"] if node["id"] == "source"
    )
    assert source["output_sizes_bytes"] == {"source": len(b"config") + len(b"weights")}
    assert {node["id"] for node in payload["cold_run"]["nodes"]} == {
        node_id for node_id, _, _ in NODE_SPECS
    }
    assert payload["commands"]["path_policy"] == (
        "Local paths replaced with digest-bound placeholders."
    )
    assert len(payload["commands"]["nodes"]) == 5
    q4_command = next(
        item
        for item in payload["commands"]["nodes"]
        if item["node_id"] == "quantize-q4_0"
    )
    assert q4_command["display_argv"][0] == "llama-quantize"
    assert q4_command["display_argv"][-1] == "Q4_0"
    assert not {
        "command",
        "prompt",
        "stdout",
        "stderr",
        "log",
        "log_path",
        "path",
        "root",
    }.intersection(_keys(payload))
    assert all(not value.startswith("/private/") for value in _strings(payload))

    output = public_evidence.write_private_json(
        tmp_path / "public/evidence.json", payload
    )
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload.__setitem__(
                "schema", "modelbake.public-acceptance.v1"
            ),
            "schema must be modelbake.public-acceptance.v3",
        ),
        (
            lambda payload: payload.__setitem__("unexpected", True),
            "public evidence fields must be exactly",
        ),
        (
            lambda payload: payload["release"]["wheel"].__setitem__(
                "size_bytes", payload["release"]["wheel"]["size_bytes"] + 1
            ),
            "release files do not match",
        ),
        (
            lambda payload: payload["cold_run"]["nodes"][0][
                "output_sizes_bytes"
            ].__setitem__("source", -1),
            "output size must be a non-negative integer",
        ),
        (
            lambda payload: payload["warm_run"]["nodes"][0][
                "output_digests"
            ].__setitem__("source", "sha256:" + "f" * 64),
            "public cold and warm output_digests differ",
        ),
        (
            lambda payload: payload["acceptance"].__setitem__(
                "candidate_manifest_digest", "sha256:" + "f" * 64
            ),
            "acceptance candidate digest does not match",
        ),
        (
            lambda payload: payload["acceptance"].pop("review_digest"),
            "acceptance fields must be exactly",
        ),
        (
            lambda payload: payload["commands"]["nodes"][1][
                "display_argv"
            ].__setitem__(-1, "Q8_0"),
            "quantize-q4_0: displayed quantize argv is invalid",
        ),
        (
            lambda payload: payload["commands"]["nodes"][0].__setitem__(
                "tool_digest", "sha256:" + "f" * 64
            ),
            "displayed argv binding is invalid",
        ),
        (
            lambda payload: payload["commands"]["nodes"].pop(),
            "public commands must contain five executed nodes",
        ),
    ],
)
def test_strict_public_validator_rejects_stale_or_malformed_records(
    acceptance: dict[str, Any], mutation: Any, message: str
) -> None:
    payload = _generate(acceptance)
    mutation(payload)

    with pytest.raises(public_evidence.PublicEvidenceError, match=message):
        public_evidence.validate_public_evidence(
            payload,
            wheel=acceptance["wheel"],
            sdist=acceptance["sdist"],
        )


def test_strict_public_validator_detects_package_replacement(
    acceptance: dict[str, Any]
) -> None:
    payload = _generate(acceptance)
    acceptance["sdist"].write_bytes(b"replaced source distribution bytes")

    with pytest.raises(
        public_evidence.PublicEvidenceError, match="release files do not match"
    ):
        public_evidence.validate_public_evidence(
            payload,
            wheel=acceptance["wheel"],
            sdist=acceptance["sdist"],
        )


def test_generation_rejects_legacy_unreviewed_baseline(
    acceptance: dict[str, Any], tmp_path: Path
) -> None:
    legacy_store = tmp_path / "legacy-baselines"
    accept_baseline(
        acceptance["warm_path"],
        legacy_store,
        "legacy",
        (acceptance["artifact_root"],),
    )
    acceptance["baseline_store"] = legacy_store
    acceptance["baseline_name"] = "legacy"

    with pytest.raises(
        public_evidence.PublicEvidenceError,
        match="receipt-backed v2 decision; legacy v1 is forbidden",
    ):
        _generate(acceptance)


def test_generation_rejects_initial_decision_without_predecessor(
    acceptance: dict[str, Any], tmp_path: Path
) -> None:
    initial_store = tmp_path / "initial-baselines"
    receipt = tmp_path / "initial-only-review.json"
    review_baseline(
        acceptance["warm_path"],
        initial_store,
        "initial-only",
        (acceptance["artifact_root"],),
        receipt,
        initial=True,
    )
    accept_baseline(
        acceptance["warm_path"],
        initial_store,
        "initial-only",
        (acceptance["artifact_root"],),
        review=receipt,
    )
    acceptance["baseline_store"] = initial_store
    acceptance["baseline_name"] = "initial-only"

    with pytest.raises(
        public_evidence.PublicEvidenceError,
        match="reviewed successor with a predecessor",
    ):
        _generate(acceptance)


def test_generation_rejects_warm_candidate_that_is_not_current(
    acceptance: dict[str, Any], tmp_path: Path
) -> None:
    successor = copy.deepcopy(acceptance["warm"])
    successor["run_id"] = "newer-run"
    successor_path = tmp_path / "newer.json"
    successor_path.write_text(json.dumps(successor), encoding="utf-8")
    receipt = tmp_path / "newer-review.json"
    review_baseline(
        successor_path,
        acceptance["baseline_store"],
        acceptance["baseline_name"],
        (acceptance["artifact_root"],),
        receipt,
    )
    accept_baseline(
        successor_path,
        acceptance["baseline_store"],
        acceptance["baseline_name"],
        (acceptance["artifact_root"],),
        review=receipt,
    )

    with pytest.raises(
        public_evidence.PublicEvidenceError,
        match="warm candidate is not the exact current accepted manifest",
    ):
        _generate(acceptance)


def test_generation_rejects_tampered_stored_review(
    acceptance: dict[str, Any]
) -> None:
    review_path = (
        acceptance["baseline_store"]
        / acceptance["accepted_warm"].decision["review_record"]
    )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["comparison_digest"] = "sha256:" + "f" * 64
    review_path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(
        public_evidence.PublicEvidenceError,
        match="baseline ledger verification failed",
    ):
        _generate(acceptance)


def test_rejects_artifact_tampering(acceptance: dict[str, Any]) -> None:
    (acceptance["artifact_root"] / "quantize-q4_0.bin").write_bytes(b"tampered")
    with pytest.raises(
        public_evidence.PublicEvidenceError, match="artifact verification failed"
    ):
        _generate(acceptance)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status_probe_exit_code", 1, "clean status is not verified"),
        ("status_probe_timed_out", True, "clean status is not verified"),
        ("status_probe_truncated", True, "clean status is not verified"),
        ("tracked_or_untracked_changes", True, "acceptance checkout must be clean"),
    ],
)
def test_rejects_unverified_or_dirty_tool_checkout(
    acceptance: dict[str, Any], field: str, value: Any, message: str
) -> None:
    cold = copy.deepcopy(acceptance["cold"])
    cold["nodes"][0]["observations"]["tool_source_repository"][field] = value
    acceptance["cold_path"].write_text(json.dumps(cold), encoding="utf-8")

    with pytest.raises(public_evidence.PublicEvidenceError, match=message):
        _generate(acceptance)


def test_rejects_cold_warm_output_mismatch(acceptance: dict[str, Any]) -> None:
    alternate = acceptance["artifact_root"] / "alternate.bin"
    alternate.write_bytes(b"different but internally verified")
    warm = copy.deepcopy(acceptance["warm"])
    warm["nodes"][2]["outputs"] = {"artifact": str(alternate)}
    warm["nodes"][2]["output_digests"] = {"artifact": tree_digest(alternate)}
    acceptance["warm_path"].write_text(json.dumps(warm), encoding="utf-8")
    with pytest.raises(
        public_evidence.PublicEvidenceError, match="output digest map changed"
    ):
        _generate(acceptance)


def test_rejects_cold_warm_size_mismatch(acceptance: dict[str, Any]) -> None:
    cold_sizes = {
        node["id"]: {name: 10 for name in node["output_digests"]}
        for node in acceptance["cold"]["nodes"]
    }
    warm_sizes = copy.deepcopy(cold_sizes)
    warm_sizes["quantize-q8_0"]["artifact"] += 1
    with pytest.raises(
        public_evidence.PublicEvidenceError, match="output size map changed"
    ):
        public_evidence._validate_lineage(
            acceptance["cold"], acceptance["warm"], cold_sizes, warm_sizes
        )


def test_rejects_stale_wheel_code(acceptance: dict[str, Any], tmp_path: Path) -> None:
    stale_wheel = tmp_path / "modelbake_ai-1.2.3-stale-py3-none-any.whl"
    _write_wheel(stale_wheel, tmp_path / "stale-package", stale=True)
    acceptance["wheel"] = stale_wheel
    with pytest.raises(
        public_evidence.PublicEvidenceError, match="does not match both runs"
    ):
        _generate(acceptance)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda command: command.__setitem__(4, "raw secret prompt"),
            "prompt is not redacted",
        ),
        (
            lambda command: command.pop(),
            "smoke command is not the exact configured form",
        ),
        (
            lambda command: command.__setitem__(0, "/opt/wrappers/llama-cli"),
            "smoke command",
        ),
    ],
)
def test_rejects_non_direct_or_unredacted_smoke_command(
    acceptance: dict[str, Any], mutation: Any, message: str
) -> None:
    cold = copy.deepcopy(acceptance["cold"])
    command = cold["nodes"][4]["command"]
    mutation(command)
    acceptance["cold_path"].write_text(json.dumps(cold), encoding="utf-8")
    with pytest.raises(public_evidence.PublicEvidenceError, match=message):
        _generate(acceptance)


def test_public_validator_rejects_dirty_checkout_claim(
    acceptance: dict[str, Any]
) -> None:
    payload = _generate(acceptance)
    payload["runner"]["tool_source_had_changes"] = True

    with pytest.raises(public_evidence.PublicEvidenceError, match="recorded as clean"):
        public_evidence.validate_public_evidence(
            payload,
            wheel=acceptance["wheel"],
            sdist=acceptance["sdist"],
        )


def test_generation_rejects_forged_nonexistent_toolchain(
    acceptance: dict[str, Any]
) -> None:
    forged_tools = {
        **acceptance["declared"]["tools"],
        "python": "/definitely-missing/modelbake-python",
        "convert": "/definitely-missing/convert_hf_to_gguf.py",
        "quantize": "/definitely-missing/llama-quantize",
        "runner": "/definitely-missing/llama-cli",
    }
    for key in ("cold", "warm"):
        manifest = copy.deepcopy(acceptance[key])
        manifest["runner"]["declared"]["tools"] = forged_tools
        for node in manifest["nodes"]:
            command = node["command"]
            if node["kind"] == "convert":
                command[0:2] = [forged_tools["python"], forged_tools["convert"]]
            elif node["kind"] == "quantize":
                command[0] = forged_tools["quantize"]
            elif node["kind"] == "smoke":
                command[0] = forged_tools["runner"]
        acceptance[f"{key}_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        public_evidence.PublicEvidenceError,
        match="cannot observe live llama.cpp toolchain",
    ):
        _generate(acceptance)


def test_generation_rejects_digest_consistent_non_gguf_artifacts(
    acceptance: dict[str, Any]
) -> None:
    for node_id in ("convert-f16-base", "quantize-q4_0", "quantize-q8_0"):
        (acceptance["artifact_root"] / f"{node_id}.bin").write_bytes(
            b"arbitrary bytes presented as a model"
        )
    for key in ("cold", "warm"):
        manifest = copy.deepcopy(acceptance[key])
        for node in manifest["nodes"]:
            if node["kind"] in {"convert", "quantize"}:
                output = next(iter(node["outputs"].values()))
                node["output_digests"] = {"artifact": tree_digest(output)}
        acceptance[f"{key}_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(public_evidence.PublicEvidenceError, match="invalid GGUF magic"):
        _generate(acceptance)


def test_generation_rejects_empty_llama_cli_observation(
    acceptance: dict[str, Any]
) -> None:
    for node_id in ("smoke-q4_0", "smoke-q8_0"):
        (acceptance["artifact_root"] / f"{node_id}.bin").write_bytes(b"")
    for key in ("cold", "warm"):
        manifest = copy.deepcopy(acceptance[key])
        for node in manifest["nodes"]:
            if node["kind"] == "smoke":
                node["observations"]["stdout"] = ""
                node["observations"]["log_bytes_recorded"] = 0
                output = next(iter(node["outputs"].values()))
                node["output_digests"] = {"observation": tree_digest(output)}
        acceptance[f"{key}_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(public_evidence.PublicEvidenceError, match="no nonempty stdout"):
        _generate(acceptance)
