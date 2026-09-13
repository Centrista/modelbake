from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"
SPEC = importlib.util.spec_from_file_location("release_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_check
previous_bytecode_setting = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(release_check)
finally:
    sys.dont_write_bytecode = previous_bytecode_setting


def _valid_site(root: Path) -> Path:
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html lang='en'><head><meta name='viewport' "
        "content='width=device-width'><title>Test</title><link rel='stylesheet' "
        "href='./styles.css'></head><body><main>Publisher acceptance release record</main>"
        "<script src='./app.js'></script></body></html>",
        encoding="utf-8",
    )
    (root / "styles.css").write_text("body { color: black; }", encoding="utf-8")
    (root / "app.js").write_text("const ready = true;\n", encoding="utf-8")
    evidence = {
        "schema": "modelbake.site-evidence.v1",
        "run_id": "cold-run",
        "project": "fixture-demo",
        "recipe_digest": "sha256:" + "1" * 64,
        "source_digest": "sha256:" + "2" * 64,
        "runner": "modelbake.fixture/v0",
        "repeat_run": {
            "run_id": "warm-run",
            "same_inputs": True,
            "digest_verified_cache_hits": 7,
        },
        "nodes": [
            {
                "id": "source",
                "kind": "source",
                "state": "produced",
                "bytes": 245,
                "digest": "sha256:" + "3" * 64,
            },
            {
                "id": "f16",
                "kind": "convert",
                "state": "produced",
                "bytes": 444,
                "digest": "sha256:" + "4" * 64,
            },
            *[
                {
                    "id": node_id,
                    "kind": "quantize",
                    "state": "produced",
                    "bytes": 547,
                    "digest": f"sha256:{index:064x}",
                }
                for index, node_id in enumerate(("q4", "q8"), start=5)
            ],
            *[
                {
                    "id": node_id,
                    "kind": "smoke",
                    "state": "fixture_observed",
                    "tokens_observed": 4,
                    "digest": f"sha256:{index:064x}",
                }
                for index, node_id in enumerate(
                    ("run-f16", "run-q4", "run-q8"), start=7
                )
            ],
        ],
        "exclusions": ["one", "two", "three"],
    }
    (root / "demo-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return root


def _valid_comparison_evidence() -> dict[str, object]:
    return {
        "schema": "modelbake.public-comparison.v1",
        "fixture_only": True,
        "lineage": {"source_digest_same": True},
        "counts": {"reused_from_cache": 4, "added": 2},
        "nodes": [
            {
                "id": "source",
                "change": "reused_from_cache",
                "recorded_output_digests": "same",
            }
        ],
        "truth_boundary": "Fixture evidence, not GGUF runtime behavior.",
    }


def _write_wheel(path: Path, *, include_review_modules: bool = True) -> None:
    names = [
        "modelbake/cli.py",
        "modelbake_ai-0.1.0.dist-info/METADATA",
        "modelbake_ai-0.1.0.dist-info/entry_points.txt",
        "modelbake_ai-0.1.0.dist-info/RECORD",
    ]
    if include_review_modules:
        names.extend(("modelbake/baseline.py", "modelbake/compare.py"))
    with zipfile.ZipFile(path, mode="w") as archive:
        for name in names:
            archive.writestr(name, b"release member")


def _write_sdist(path: Path, *, extra: tuple[str, ...] = ()) -> None:
    root = "modelbake_ai-0.1.0"
    names = (
        "action.yml",
        "docs/BASELINES.md",
        "docs/CI_INTEGRATION.md",
        "docs/EVIDENCE_MODEL.md",
        "docs/REAL_ACCEPTANCE.md",
        "examples/github/modelbake-release.yml",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "scripts/generate_public_evidence.py",
        "scripts/release_check.py",
        "src/modelbake/cli.py",
        *extra,
    )
    with tarfile.open(path, mode="w:gz") as archive:
        for name in names:
            data = b"release member"
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _write_parity_archives(
    root: Path, *, wheel_source: bytes, sdist_source: bytes
) -> tuple[Path, Path]:
    wheel = root / "modelbake_ai-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("modelbake/__init__.py", wheel_source)
        archive.writestr(
            "modelbake_ai-0.1.0.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: modelbake-ai\nVersion: 0.1.0\n",
        )
    sdist = root / "modelbake_ai-0.1.0.tar.gz"
    members = {
        "modelbake_ai-0.1.0/src/modelbake/__init__.py": sdist_source,
        "modelbake_ai-0.1.0/PKG-INFO": (
            b"Metadata-Version: 2.4\nName: modelbake-ai\nVersion: 0.1.0\n"
        ),
    }
    with tarfile.open(sdist, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return wheel, sdist


def test_validate_site_checks_assets_json_and_javascript(tmp_path: Path) -> None:
    checked: list[str] = []
    assets = release_check.validate_site(
        _valid_site(tmp_path / "site"),
        javascript_checker=lambda path: checked.append(path.name),
    )
    assert checked == ["app.js"]
    assert {path.name for path in assets} == {
        "app.js",
        "demo-evidence.json",
        "index.html",
        "styles.css",
    }


def test_validate_site_fails_closed_for_missing_asset(tmp_path: Path) -> None:
    site = _valid_site(tmp_path / "site")
    (site / "styles.css").unlink()
    with pytest.raises(release_check.ReleaseCheckError, match="missing referenced"):
        release_check.validate_site(site, javascript_checker=lambda _path: None)


def test_validate_site_fails_closed_without_javascript_checker(tmp_path: Path) -> None:
    with pytest.raises(release_check.ReleaseCheckError, match="unavailable"):
        release_check.validate_site(_valid_site(tmp_path / "site"))


@pytest.mark.parametrize("claim", ("CERTIFIED SAFE", "WORKS EVERYWHERE"))
def test_site_rejects_broad_claims_in_html_or_javascript(
    tmp_path: Path, claim: str
) -> None:
    site = _valid_site(tmp_path / "site")
    if claim == "CERTIFIED SAFE":
        (site / "index.html").write_text(
            (site / "index.html").read_text(encoding="utf-8").replace(
                "Publisher acceptance release record", claim
            ),
            encoding="utf-8",
        )
    else:
        (site / "app.js").write_text(f"document.body.textContent = '{claim}';\n")
    with pytest.raises(release_check.ReleaseCheckError, match="broad public claim"):
        release_check.validate_site(site, javascript_checker=lambda _path: None)


def test_evidence_page_requires_publisher_boundary_script_and_dynamic_ids(tmp_path: Path) -> None:
    page = tmp_path / "real-evidence.html"
    valid = (
        "<!doctype html><html><body><main>Generated by the publisher, not an "
        "independent attestation. The model was not certified. Publisher-generated "
        "CAS acceptance. Receipt-backed baseline decision. Records no actor identity "
        "or authorization.</main>"
        "<script src='./evidence.js'></script></body></html>"
    )
    release_check._validate_public_claims(page, valid)
    release_check._validate_evidence_page_claims(page, valid)
    with pytest.raises(release_check.ReleaseCheckError, match="hardcoded long"):
        release_check._validate_public_claims(page, valid + ("a" * 40))
    with pytest.raises(release_check.ReleaseCheckError, match="load evidence.js"):
        release_check._validate_evidence_page_claims(
            page, valid.replace("<script src='./evidence.js'></script>", "")
        )


def test_site_real_evidence_requires_cas_decision_review_and_candidate_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real-evidence.json"
    candidate = "sha256:" + "3" * 64
    payload = {
        "schema": "modelbake.public-acceptance.v2",
        "acceptance": {
            "kind": "publisher-generated-cas-acceptance",
            "channel": "production",
            "decision_digest": "sha256:" + "1" * 64,
            "review_digest": "sha256:" + "2" * 64,
            "candidate_manifest_digest": candidate,
            "predecessor_decision_digest": "sha256:" + "4" * 64,
            "comparison_digest": "sha256:" + "5" * 64,
        },
        "warm_run": {"manifest": {"modelbake_digest": candidate}},
    }
    release_check._validate_real_evidence(payload, path)

    missing_review = json.loads(json.dumps(payload))
    del missing_review["acceptance"]["review_digest"]
    with pytest.raises(release_check.ReleaseCheckError, match="CAS acceptance binding"):
        release_check._validate_real_evidence(missing_review, path)

    wrong_candidate = json.loads(json.dumps(payload))
    wrong_candidate["warm_run"]["manifest"]["modelbake_digest"] = (
        "sha256:" + "9" * 64
    )
    with pytest.raises(release_check.ReleaseCheckError, match="warm manifest"):
        release_check._validate_real_evidence(wrong_candidate, path)


def test_site_rejects_legacy_real_evidence_outside_bootstrap(tmp_path: Path) -> None:
    site = _valid_site(tmp_path / "site")
    evidence = site / "real-evidence.json"
    evidence.write_text(
        json.dumps({"schema": "modelbake.public-acceptance.v1"}), encoding="utf-8"
    )

    with pytest.raises(release_check.ReleaseCheckError, match="unexpected real"):
        release_check.validate_site(site, javascript_checker=lambda _path: None)

    release_check.validate_site(
        site,
        javascript_checker=lambda _path: None,
        validate_real_evidence=False,
    )


def test_prepare_stage_refuses_unmarked_contents(tmp_path: Path) -> None:
    stage = tmp_path / "candidate"
    stage.mkdir()
    evidence = stage / "keep.txt"
    evidence.write_text("user data", encoding="utf-8")
    with pytest.raises(release_check.ReleaseCheckError, match="unmarked"):
        release_check.prepare_stage(stage)
    assert evidence.read_text(encoding="utf-8") == "user data"


def test_prepare_stage_clears_only_a_valid_marked_directory(tmp_path: Path) -> None:
    stage = release_check.prepare_stage(tmp_path / "candidate")
    (stage / "old.txt").write_text("old", encoding="utf-8")
    nested = stage / "nested"
    nested.mkdir()
    (nested / "artifact").write_bytes(b"old")
    assert release_check.prepare_stage(stage) == stage
    assert {path.name for path in stage.iterdir()} == {release_check.STAGE_MARKER}


def test_run_command_fails_closed_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["tool"], 9, stdout="", stderr="bad output")

    monkeypatch.setattr(release_check.subprocess, "run", failed)
    with pytest.raises(release_check.ReleaseCheckError, match="exit 9"):
        release_check.run_command(["tool", "arg"])


def test_archive_inventory_rejects_path_traversal(tmp_path: Path) -> None:
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("../escape", b"bad")
    with pytest.raises(release_check.ReleaseCheckError, match="unsafe archive member"):
        release_check.inspect_archives([wheel])


def test_checksums_are_stable_and_scoped_to_stage(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    artifact = stage / "artifact.whl"
    artifact.write_bytes(b"release bytes")
    destination = release_check.write_checksums(stage, [artifact])
    checksum, relative = destination.read_text(encoding="utf-8").strip().split("  ")
    assert checksum == release_check.sha256_file(artifact)
    assert relative == "artifact.whl"
    outside = tmp_path / "outside"
    outside.write_bytes(b"no")
    with pytest.raises(release_check.ReleaseCheckError, match="outside staging"):
        release_check.write_checksums(stage, [outside])


def test_publish_bundle_contains_only_public_files_with_exhaustive_checksums(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    packages = stage / "packages"
    packages.mkdir(parents=True)
    wheel = packages / "modelbake_ai-0.1.0-py3-none-any.whl"
    sdist = packages / "modelbake_ai-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    inventory = stage / "archive-inventory.json"
    inventory.write_text('{"archives":[]}\n', encoding="utf-8")
    evidence = tmp_path / "real-evidence.json"
    evidence.write_text('{"schema":"modelbake.public-acceptance.v2"}\n', encoding="utf-8")
    (stage / "private-manifest.json").write_text("private", encoding="utf-8")

    publish = release_check.create_publish_bundle(
        stage, [wheel, sdist], inventory, evidence
    )
    names = {path.name for path in publish.iterdir()}
    assert names == {
        wheel.name,
        sdist.name,
        "archive-inventory.json",
        "public-real-evidence.json",
        "SHA256SUMS",
    }
    listed = {
        line.split("  ", 1)[1]
        for line in (publish / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    }
    assert listed == names - {"SHA256SUMS"}
    assert "private-manifest.json" not in names


def test_composite_action_stages_exact_nonhidden_evidence_set() -> None:
    action = (release_check.ROOT / "action.yml").read_text(encoding="utf-8")
    assert "modelbake-release-upload-${{ github.run_id }}-${{ github.run_attempt }}" in action
    assert 'names = ("manifest.json", "report.html", "comparison.md", "review.json")' in action
    assert "staged evidence set is not exact" in action
    assert "if-no-files-found: error" in action
    upload_block = action.split("- name: Upload the release evidence", 1)[1]
    assert "${{ inputs.run-directory }}/manifest.json" not in upload_block
    assert "include-hidden-files: true" not in upload_block


def test_shipped_run_blocks_never_interpolate_expressions_directly() -> None:
    paths = [
        release_check.ROOT / "action.yml",
        release_check.ROOT / "examples/github/modelbake-release.yml",
        *sorted((release_check.ROOT / ".github/workflows").glob("*.yml")),
    ]
    for path in paths:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if path.name == "action.yml":
            steps = payload["runs"]["steps"]
        else:
            steps = [
                step
                for job in payload["jobs"].values()
                for step in job.get("steps", [])
            ]
        for step in steps:
            assert "${{" not in step.get("run", ""), (
                f"route expressions through env before shell execution: {path}: "
                f"{step.get('name', '<unnamed>')}"
            )


def test_comparison_evidence_requires_current_digest_fields(tmp_path: Path) -> None:
    path = tmp_path / "comparison-evidence.json"
    payload = _valid_comparison_evidence()
    release_check._validate_comparison_evidence(payload, path)

    obsolete = json.loads(json.dumps(payload))
    obsolete["lineage"]["source_bytes_same"] = True
    with pytest.raises(release_check.ReleaseCheckError, match="obsolete fields"):
        release_check._validate_comparison_evidence(obsolete, path)

    missing_lineage = json.loads(json.dumps(payload))
    del missing_lineage["lineage"]["source_digest_same"]
    with pytest.raises(release_check.ReleaseCheckError, match="source_digest_same"):
        release_check._validate_comparison_evidence(missing_lineage, path)

    obsolete_node = json.loads(json.dumps(payload))
    del obsolete_node["nodes"][0]["recorded_output_digests"]
    obsolete_node["nodes"][0]["artifact_bytes"] = "same"
    with pytest.raises(release_check.ReleaseCheckError, match="obsolete fields"):
        release_check._validate_comparison_evidence(obsolete_node, path)


def test_archive_inventory_requires_release_review_surface(tmp_path: Path) -> None:
    wheel = tmp_path / "modelbake_ai-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "modelbake_ai-0.1.0.tar.gz"
    _write_wheel(wheel)
    _write_sdist(sdist)

    inventory = release_check.inspect_archives([wheel, sdist])

    assert len(inventory["archives"]) == 2


def test_archive_inventory_requires_compare_and_baseline_modules(tmp_path: Path) -> None:
    wheel = tmp_path / "modelbake_ai-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, include_review_modules=False)

    with pytest.raises(release_check.ReleaseCheckError, match="baseline.py"):
        release_check.inspect_archives([wheel])


def test_distribution_parity_accepts_identical_python_source(tmp_path: Path) -> None:
    source = b'__version__ = "0.1.0"\n'
    wheel, sdist = _write_parity_archives(
        tmp_path, wheel_source=source, sdist_source=source
    )
    release_check.validate_distribution_parity(wheel, sdist)


def test_distribution_parity_rejects_divergent_or_broken_sdist(tmp_path: Path) -> None:
    source = b'__version__ = "0.1.0"\n'
    wheel, divergent = _write_parity_archives(
        tmp_path, wheel_source=source, sdist_source=b'__version__ = "other"\n'
    )
    with pytest.raises(release_check.ReleaseCheckError, match="sources differ"):
        release_check.validate_distribution_parity(wheel, divergent)

    wheel.unlink()
    divergent.unlink()
    wheel, broken = _write_parity_archives(
        tmp_path, wheel_source=source, sdist_source=b"def broken(:\n"
    )
    with pytest.raises(release_check.ReleaseCheckError, match="sdist Python source is invalid"):
        release_check.validate_distribution_parity(wheel, broken)


def test_installed_story_validator_matches_actual_graph_node_ids(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema": "modelbake.run.v1",
                "status": "succeeded",
                "nodes": [
                    {"id": "source", "state": "produced"},
                    {"id": "convert-f16-base", "state": "produced"},
                    {"id": "quantize-q4", "state": "produced"},
                    {"id": "smoke-q4", "state": "fixture_observed"},
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "schema": "modelbake.run.v1",
                "status": "succeeded",
                "nodes": [
                    {"id": "source", "state": "cache_hit"},
                    {"id": "convert-f16-base", "state": "cache_hit"},
                    {"id": "quantize-q4", "state": "cache_hit"},
                    {"id": "smoke-q4", "state": "cache_hit"},
                    {"id": "quantize-q8", "state": "produced"},
                    {"id": "smoke-q8", "state": "fixture_observed"},
                ],
            }
        ),
        encoding="utf-8",
    )

    release_check._load_story_manifest(baseline, candidate=False)
    release_check._load_story_manifest(candidate, candidate=True)


@pytest.mark.parametrize(
    "forbidden",
    (
        "site/.openai/hosting.json",
        "site/dist/downloads/modelbake_ai-0.1.0.tar.gz",
        "site/dist/index.html",
        ".integration/real-llama/run/manifest.json",
    ),
)
def test_archive_inventory_rejects_private_or_recursive_site_files(
    tmp_path: Path, forbidden: str
) -> None:
    sdist = tmp_path / "modelbake_ai-0.1.0.tar.gz"
    _write_sdist(sdist, extra=(forbidden,))

    with pytest.raises(release_check.ReleaseCheckError, match="excluded site"):
        release_check.inspect_archives([sdist])


def test_public_evidence_gate_requires_record_unless_explicitly_bootstrapping(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "modelbake_ai-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "modelbake_ai-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    with pytest.raises(release_check.ReleaseCheckError, match="missing public"):
        release_check.validate_public_evidence(
            tmp_path / "missing.json", [wheel, sdist], {}
        )


def test_public_evidence_gate_uses_exact_staged_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "real-evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    wheel = tmp_path / "modelbake_ai-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "modelbake_ai-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    observed: list[tuple[list[object], dict[str, str]]] = []

    def fake_run(
        argv: object, *, env: dict[str, str], label: str
    ) -> release_check.CommandResult:
        assert label == "validate public real acceptance evidence against staged packages"
        observed.append((list(argv), env))
        return release_check.CommandResult("", "")

    monkeypatch.setattr(release_check, "run_command", fake_run)
    release_check.validate_public_evidence(evidence, [wheel, sdist], {"SAFE": "1"})

    argv, env = observed[0]
    assert argv[2:4] == ["--validate-public", evidence]
    assert argv[4:] == ["--wheel", wheel, "--sdist", sdist]
    assert env == {"SAFE": "1"}


def test_real_acceptance_bootstrap_is_explicit_and_off_by_default() -> None:
    assert release_check._parser().parse_args([]).bootstrap_real_acceptance is False
    assert (
        release_check._parser()
        .parse_args(["--bootstrap-real-acceptance"])
        .bootstrap_real_acceptance
        is True
    )


def test_distribution_build_pins_reproducible_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    observed_env: dict[str, str] = {}

    def fake_run(
        argv: object, *, env: dict[str, str], label: str
    ) -> release_check.CommandResult:
        assert label == "build wheel and source distribution"
        observed_env.update(env)
        packages = stage / "packages"
        (packages / "modelbake_ai-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        (packages / "modelbake_ai-0.1.0.tar.gz").write_bytes(b"sdist")
        return release_check.CommandResult("", "")

    monkeypatch.setattr(release_check, "run_command", fake_run)
    archives = release_check._build_distributions(stage)

    assert len(archives) == 2
    assert (
        observed_env["SOURCE_DATE_EPOCH"]
        == release_check.REPRODUCIBLE_BUILD_EPOCH
    )


@pytest.mark.parametrize(
    ("bootstrap", "expected_calls"),
    [(False, 1), (True, 0)],
)
def test_release_gate_skips_real_evidence_only_in_explicit_bootstrap_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap: bool,
    expected_calls: int,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    wheel = stage / "candidate.whl"
    sdist = stage / "candidate.tar.gz"
    evidence_calls: list[Path] = []

    monkeypatch.setattr(release_check, "prepare_stage", lambda _path: stage)
    monkeypatch.setattr(release_check, "_python_env", dict)
    monkeypatch.setattr(
        release_check,
        "run_command",
        lambda *args, **kwargs: release_check.CommandResult("", ""),
    )
    monkeypatch.setattr(release_check.shutil, "which", lambda _name: None)
    monkeypatch.setattr(release_check, "_compile_python", lambda: None)
    monkeypatch.setattr(release_check, "_run_fixture_acceptance", lambda *a, **k: [])
    monkeypatch.setattr(release_check, "_build_distributions", lambda _stage: [wheel, sdist])
    monkeypatch.setattr(release_check, "inspect_archives", lambda _archives: {})
    monkeypatch.setattr(release_check, "validate_distribution_parity", lambda *a: None)
    monkeypatch.setattr(release_check, "sync_release_distributions", lambda *a: [])
    monkeypatch.setattr(release_check, "sync_site_downloads", lambda *a: [])
    monkeypatch.setattr(
        release_check,
        "validate_public_evidence",
        lambda path, _archives, _env: evidence_calls.append(path),
    )
    monkeypatch.setattr(release_check, "_require_tool", lambda *a: "node")
    monkeypatch.setattr(release_check, "validate_site", lambda *a, **k: [])
    monkeypatch.setattr(release_check, "_smoke_install", lambda _wheel: None)
    monkeypatch.setattr(release_check, "write_checksums", lambda *a: stage / "sums")
    publish_calls: list[Path] = []
    monkeypatch.setattr(
        release_check,
        "create_publish_bundle",
        lambda *a: publish_calls.append(stage / "publish") or stage / "publish",
    )

    release_check.release_check(stage, bootstrap_real_acceptance=bootstrap)

    assert len(evidence_calls) == expected_calls
    assert len(publish_calls) == (0 if bootstrap else 1)


def test_sync_site_downloads_replaces_stale_packages_with_exact_bytes(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = packages / "modelbake_ai-0.1.0-py3-none-any.whl"
    sdist = packages / "modelbake_ai-0.1.0.tar.gz"
    wheel.write_bytes(b"exact wheel")
    sdist.write_bytes(b"exact source")
    site_dist = tmp_path / "site-dist"
    downloads = site_dist / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "modelbake_ai-0.0.9-py3-none-any.whl").write_bytes(b"stale")
    (downloads / "modelbake_ai-0.0.9.tar.gz").write_bytes(b"stale")

    synced = release_check.sync_site_downloads([wheel, sdist], site_dist)

    assert {path.name for path in synced} == {wheel.name, sdist.name}
    assert {path.name for path in downloads.iterdir()} == {
        wheel.name,
        sdist.name,
        "SHA256SUMS",
    }
    assert (downloads / wheel.name).read_bytes() == wheel.read_bytes()
    assert (downloads / sdist.name).read_bytes() == sdist.read_bytes()
    assert (downloads / "SHA256SUMS").read_text(encoding="utf-8") == (
        f"{release_check.sha256_file(wheel)}  {wheel.name}\n"
        f"{release_check.sha256_file(sdist)}  {sdist.name}\n"
    )


def test_sync_site_downloads_refuses_unexpected_content(tmp_path: Path) -> None:
    wheel = tmp_path / "package.whl"
    sdist = tmp_path / "package.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"source")
    site_dist = tmp_path / "site-dist"
    downloads = site_dist / "downloads"
    downloads.mkdir(parents=True)
    unexpected = downloads / "keep.txt"
    unexpected.write_text("user data", encoding="utf-8")

    with pytest.raises(release_check.ReleaseCheckError, match="non-package"):
        release_check.sync_site_downloads([wheel, sdist], site_dist)
    assert unexpected.read_text(encoding="utf-8") == "user data"


def test_sync_release_distributions_replaces_stale_pair(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = packages / "modelbake_ai-0.1.0-py3-none-any.whl"
    sdist = packages / "modelbake_ai-0.1.0.tar.gz"
    wheel.write_bytes(b"exact wheel")
    sdist.write_bytes(b"exact source")
    release_dist = tmp_path / "dist"
    release_dist.mkdir()
    (release_dist / "modelbake_ai-0.0.9-py3-none-any.whl").write_bytes(b"stale")
    (release_dist / "modelbake_ai-0.0.9.tar.gz").write_bytes(b"stale")

    synced = release_check.sync_release_distributions([wheel, sdist], release_dist)

    assert {path.name for path in synced} == {wheel.name, sdist.name}
    assert {path.name for path in release_dist.iterdir()} == {wheel.name, sdist.name}
    assert (release_dist / wheel.name).read_bytes() == wheel.read_bytes()
    assert (release_dist / sdist.name).read_bytes() == sdist.read_bytes()


def test_sync_release_distributions_refuses_unexpected_content(tmp_path: Path) -> None:
    wheel = tmp_path / "package.whl"
    sdist = tmp_path / "package.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"source")
    release_dist = tmp_path / "dist"
    release_dist.mkdir()
    unexpected = release_dist / "keep.txt"
    unexpected.write_text("user data", encoding="utf-8")

    with pytest.raises(release_check.ReleaseCheckError, match="non-package"):
        release_check.sync_release_distributions([wheel, sdist], release_dist)
    assert unexpected.read_text(encoding="utf-8") == "user data"


def test_fixture_acceptance_verifies_cache_and_writes_comparison(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    demo_evidence = tmp_path / "site" / "demo-evidence.json"
    demo_evidence.parent.mkdir()
    demo_evidence.write_text('{"fabricated":true}\n', encoding="utf-8")

    generated = release_check._run_fixture_acceptance(
        stage,
        release_check._python_env(),
        demo_evidence_path=demo_evidence,
    )

    comparison = stage / "reports" / "fixture-comparison.json"
    assert comparison in generated
    payload = json.loads(comparison.read_text(encoding="utf-8"))
    assert payload["schema"] == "modelbake.compare.v1"
    assert payload["counts"]["reused_from_cache"] == 7
    assert payload["lineage"]["source_digest_same"] is True
    public_payload = json.loads(demo_evidence.read_text(encoding="utf-8"))
    cold = json.loads(
        (stage / "acceptance" / "cold" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    warm = json.loads(
        (stage / "acceptance" / "warm" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert "fabricated" not in public_payload
    assert public_payload["run_id"] == cold["run_id"]
    assert public_payload["recipe_digest"] == cold["recipe_digest"]
    assert public_payload["source_digest"] == cold["source_digest"]
    assert public_payload["repeat_run"]["run_id"] == warm["run_id"]
    assert public_payload["repeat_run"]["digest_verified_cache_hits"] == 7
    assert [node["id"] for node in public_payload["nodes"]] == [
        "source",
        "f16",
        "q4",
        "q8",
        "run-f16",
        "run-q4",
        "run-q8",
    ]
    assert all(
        node["digest"] in manifest_node["output_digests"].values()
        for node, manifest_node in zip(public_payload["nodes"], cold["nodes"], strict=True)
    )

    warm["nodes"][0]["output_digests"]["source"] = "sha256:" + "f" * 64
    tampered_warm = tmp_path / "tampered-warm.json"
    tampered_warm.write_text(json.dumps(warm), encoding="utf-8")
    with pytest.raises(release_check.ReleaseCheckError, match="output digests changed"):
        release_check.generate_demo_evidence(
            stage / "acceptance" / "cold" / "manifest.json",
            tampered_warm,
            demo_evidence,
        )
