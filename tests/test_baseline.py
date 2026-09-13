from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from modelbake.baseline import (
    BaselineCorruptionError,
    BaselineError,
    accept_baseline,
    check_baseline,
    load_current_baseline,
    review_baseline,
    verify_ledger,
)
from modelbake.hashing import canonical_json_bytes, digest_json, tree_digest


def _write_manifest(root: Path, run_id: str, artifact: Path, *, status: str = "succeeded") -> Path:
    node_state = "produced" if status == "succeeded" else "failed"
    payload = {
        "schema": "modelbake.run.v1",
        "run_id": run_id,
        "project": "ledger-test",
        "status": status,
        "recipe_digest": "sha256:" + "1" * 64,
        "source_digest": "sha256:" + "2" * 64,
        "runner": {},
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "nodes": [
            {
                "id": "artifact",
                "state": node_state,
                "outputs": {"model": str(artifact)},
                "output_digests": {"model": tree_digest(artifact)},
                "command": [],
                "observations": {},
            }
        ],
    }
    path = root / f"{run_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_first_and_second_accept_form_chain_and_load_current(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    first_artifact = artifacts / "first.gguf"
    first_artifact.write_bytes(b"first")
    first = _write_manifest(tmp_path, "run-one", first_artifact)
    store = tmp_path / "store"

    accepted_first = accept_baseline(first, store, "production", (artifacts,), "ship one")
    assert accepted_first.created is True
    assert accepted_first.verified_artifacts == 1
    assert accepted_first.decision["predecessor_decision_digest"] is None

    second_artifact = artifacts / "second.gguf"
    second_artifact.write_bytes(b"second")
    second = _write_manifest(tmp_path, "run-two", second_artifact)
    accepted_second = accept_baseline(second, store, "production", (artifacts,))
    assert accepted_second.created is True
    assert accepted_second.decision["predecessor_decision_digest"] == accepted_first.decision_digest

    verified = verify_ledger(store, "production")
    assert verified.decisions == 2
    assert verified.current_decision_digest == accepted_second.decision_digest
    current = load_current_baseline(store, "production")
    assert current.manifest["run_id"] == "run-two"
    assert current.manifest_bytes == second.read_bytes()
    assert current.decision_digest == accepted_second.decision_digest


def test_identical_reaccept_is_idempotent_even_with_a_new_note(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"same")
    manifest = _write_manifest(tmp_path, "same-run", artifact)
    store = tmp_path / "store"
    first = accept_baseline(manifest, store, "production", (tmp_path,), "original")
    repeated = accept_baseline(manifest, store, "production", (tmp_path,), "ignored")
    assert repeated.created is False
    assert repeated.decision_digest == first.decision_digest
    assert repeated.decision["note"] == "original"
    assert verify_ledger(store, "production").decisions == 1


def test_stored_manifest_byte_tamper_is_detected(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "tamper-run", artifact)
    accepted = accept_baseline(manifest, tmp_path / "store", "production", (tmp_path,))
    accepted.manifest_path.write_bytes(accepted.manifest_path.read_bytes() + b" ")
    with pytest.raises(BaselineCorruptionError, match="stored manifest .*bytes|stored manifest digest mismatch"):
        verify_ledger(tmp_path / "store", "production")


def test_decision_chain_tamper_is_detected(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "chain-run", artifact)
    store = tmp_path / "store"
    accept_baseline(manifest, store, "production", (tmp_path,))
    decision_path = next((store / "ledgers" / "production" / "decisions").iterdir())
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["predecessor_decision_digest"] = "sha256:" + "0" * 64
    decision_path.write_bytes(json.dumps(decision, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    with pytest.raises(BaselineCorruptionError, match="hash chain is discontinuous"):
        verify_ledger(store, "production")


def test_accept_requires_explicit_root_and_verified_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "roots-run", artifact)
    with pytest.raises(BaselineError, match="explicit trusted artifact roots"):
        accept_baseline(manifest, tmp_path / "store-empty", "production", ())
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(BaselineError, match="outside allowed artifact roots"):
        accept_baseline(manifest, tmp_path / "store-outside", "production", (outside,))


def test_failed_manifest_and_bad_artifact_digest_are_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    failed = _write_manifest(tmp_path, "failed-run", artifact, status="failed")
    with pytest.raises(BaselineError, match="only a succeeded manifest"):
        accept_baseline(failed, tmp_path / "store-failed", "production", (tmp_path,))

    candidate = _write_manifest(tmp_path, "changed-run", artifact)
    artifact.write_bytes(b"changed")
    with pytest.raises(BaselineError, match="digest mismatch"):
        accept_baseline(candidate, tmp_path / "store-changed", "production", (tmp_path,))


def test_created_content_is_private_but_existing_root_mode_is_preserved(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "mode-run", artifact)
    store = tmp_path / "caller-store"
    store.mkdir(mode=0o755)
    os.chmod(store, 0o755)
    accepted = accept_baseline(manifest, store, "production", (tmp_path,))

    assert stat.S_IMODE(store.stat().st_mode) == 0o755
    for directory in (
        store / "manifests",
        store / "ledgers",
        store / "locks",
        store / "ledgers" / "production",
        store / "ledgers" / "production" / "decisions",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    decision = next((store / "ledgers" / "production" / "decisions").iterdir())
    for file in (
        accepted.manifest_path,
        decision,
        store / "ledgers" / "production" / "current.json",
        store / "locks" / "production.lock",
    ):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", ["", ".", "..", "../prod", "prod/live", "prod live", "x" * 65])
def test_baseline_name_must_be_filename_safe(tmp_path: Path, name: str) -> None:
    with pytest.raises(BaselineError, match="filename-safe"):
        verify_ledger(tmp_path / "store", name)


def test_symlink_manifest_and_store_are_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "safe-run", artifact)
    linked_manifest = tmp_path / "linked.json"
    linked_manifest.symlink_to(manifest)
    with pytest.raises(BaselineError, match="non-symlink"):
        accept_baseline(linked_manifest, tmp_path / "store", "production", (tmp_path,))

    real_store = tmp_path / "real-store"
    real_store.mkdir()
    linked_store = tmp_path / "linked-store"
    linked_store.symlink_to(real_store, target_is_directory=True)
    with pytest.raises(BaselineError, match="not a real directory"):
        verify_ledger(linked_store, "production")


def test_note_is_bounded_single_line_and_not_auto_populated(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "note-run", artifact)
    with pytest.raises(BaselineError, match="single line"):
        accept_baseline(manifest, tmp_path / "newline", "production", (tmp_path,), "a\nb")
    with pytest.raises(BaselineError, match="too long"):
        accept_baseline(manifest, tmp_path / "long", "production", (tmp_path,), "a" * 241)
    accepted = accept_baseline(manifest, tmp_path / "none", "production", (tmp_path,))
    assert "note" not in accepted.decision


def _rewrite_receipt(path: Path, **changes: object) -> None:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt.update(changes)
    payload = dict(receipt)
    payload.pop("record_digest", None)
    receipt["record_digest"] = digest_json(payload)
    path.write_bytes(canonical_json_bytes(receipt) + b"\n")


def test_initial_review_accept_check_and_idempotent_retry(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "initial-run", artifact)
    store = tmp_path / "store"
    receipt = tmp_path / "review.json"

    reviewed = review_baseline(
        manifest, store, "production", (tmp_path,), receipt, initial=True
    )
    assert reviewed.receipt["initial"] is True
    assert reviewed.predecessor_decision_digest is None
    accepted = accept_baseline(
        manifest, store, "production", (tmp_path,), review=receipt
    )
    assert accepted.created is True
    assert accepted.decision["schema"] == "modelbake.baseline.decision.v2"
    assert accepted.decision["review_digest"] == reviewed.receipt_digest

    checked = check_baseline(manifest, store, "production", (tmp_path,))
    assert checked.decision_digest == accepted.decision_digest
    repeated = accept_baseline(
        manifest, store, "production", (tmp_path,), review=receipt
    )
    assert repeated.created is False
    assert repeated.decision_digest == accepted.decision_digest
    assert verify_ledger(store, "production").decisions == 1


def test_review_preserves_existing_output_directory_mode(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "initial-run", artifact)
    output = tmp_path / "caller-owned"
    output.mkdir(mode=0o755)
    os.chmod(output, 0o755)
    review_baseline(
        manifest,
        tmp_path / "store",
        "production",
        (tmp_path,),
        output / "review.json",
        initial=True,
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o755
    assert stat.S_IMODE((output / "review.json").stat().st_mode) == 0o600


def test_review_requires_initial_only_for_empty_channel(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "initial-run", artifact)
    store = tmp_path / "store"
    with pytest.raises(BaselineError, match="use --initial"):
        review_baseline(manifest, store, "production", (tmp_path,), tmp_path / "r.json")
    accept_baseline(manifest, store, "production", (tmp_path,))
    with pytest.raises(BaselineError, match="only for an empty"):
        review_baseline(
            manifest,
            store,
            "production",
            (tmp_path,),
            tmp_path / "r.json",
            initial=True,
        )


def test_review_rejects_stale_predecessor_and_changed_candidate(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    first_artifact = artifacts / "first.gguf"
    first_artifact.write_bytes(b"first")
    first = _write_manifest(tmp_path, "first", first_artifact)
    store = tmp_path / "store"
    accept_baseline(first, store, "production", (artifacts,))

    candidate_artifact = artifacts / "candidate.gguf"
    candidate_artifact.write_bytes(b"candidate")
    candidate = _write_manifest(tmp_path, "candidate", candidate_artifact)
    receipt = tmp_path / "candidate-review.json"
    review_baseline(candidate, store, "production", (artifacts,), receipt)

    other_artifact = artifacts / "other.gguf"
    other_artifact.write_bytes(b"other")
    other = _write_manifest(tmp_path, "other", other_artifact)
    accept_baseline(other, store, "production", (artifacts,))
    with pytest.raises(BaselineError, match="release changed since this review"):
        accept_baseline(candidate, store, "production", (artifacts,), review=receipt)

    fresh_receipt = tmp_path / "fresh.json"
    review_baseline(candidate, store, "production", (artifacts,), fresh_receipt)
    candidate.write_bytes(candidate.read_bytes() + b" ")
    with pytest.raises(BaselineError, match="release changed since this review"):
        accept_baseline(candidate, store, "production", (artifacts,), review=fresh_receipt)


def test_review_rejects_changed_artifact_and_corrupt_comparison(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "candidate", artifact)
    store = tmp_path / "store"
    receipt = tmp_path / "review.json"
    review_baseline(manifest, store, "production", (tmp_path,), receipt, initial=True)
    artifact.write_bytes(b"changed")
    with pytest.raises(BaselineError, match="digest mismatch"):
        accept_baseline(manifest, store, "production", (tmp_path,), review=receipt)

    artifact.write_bytes(b"model")
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["comparison"]["promotion_decision"] = "changed"
    receipt.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(BaselineCorruptionError, match="comparison digest mismatch"):
        accept_baseline(manifest, store, "production", (tmp_path,), review=receipt)


def test_accept_recomputes_and_rejects_a_resigned_changed_comparison(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "candidate", artifact)
    store = tmp_path / "store"
    receipt = tmp_path / "review.json"
    review_baseline(manifest, store, "production", (tmp_path,), receipt, initial=True)
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["comparison"]["promotion_decision"] = "forged"
    value["comparison_digest"] = digest_json(value["comparison"])
    payload = dict(value)
    del payload["record_digest"]
    value["record_digest"] = digest_json(payload)
    receipt.write_bytes(canonical_json_bytes(value) + b"\n")
    with pytest.raises(BaselineError, match="recorded comparison"):
        accept_baseline(manifest, store, "production", (tmp_path,), review=receipt)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("name", "other", "review channel"),
        ("project", "other-project", "review project"),
    ),
)
def test_review_rejects_wrong_channel_or_project(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "candidate", artifact)
    store = tmp_path / "store"
    receipt = tmp_path / "review.json"
    review_baseline(manifest, store, "production", (tmp_path,), receipt, initial=True)
    _rewrite_receipt(receipt, **{field: value})
    with pytest.raises(BaselineError, match=message):
        accept_baseline(manifest, store, "production", (tmp_path,), review=receipt)


def test_review_rejects_resigned_verified_artifact_count(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "candidate", artifact)
    store = tmp_path / "store"
    receipt = tmp_path / "review.json"
    review_baseline(manifest, store, "production", (tmp_path,), receipt, initial=True)
    _rewrite_receipt(receipt, verified_artifacts=99)
    with pytest.raises(BaselineError, match="live artifact set"):
        accept_baseline(manifest, store, "production", (tmp_path,), review=receipt)


def test_two_receipts_for_one_predecessor_have_one_cas_winner(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    base_artifact = artifacts / "base.gguf"
    base_artifact.write_bytes(b"base")
    store = tmp_path / "store"
    base = _write_manifest(tmp_path, "base", base_artifact)
    accept_baseline(base, store, "production", (artifacts,))

    candidates: list[tuple[Path, Path]] = []
    for suffix in ("one", "two"):
        artifact = artifacts / f"{suffix}.gguf"
        artifact.write_bytes(suffix.encode())
        manifest = _write_manifest(tmp_path, suffix, artifact)
        receipt = tmp_path / f"{suffix}-review.json"
        review_baseline(manifest, store, "production", (artifacts,), receipt)
        candidates.append((manifest, receipt))

    def attempt(pair: tuple[Path, Path]) -> str:
        try:
            accept_baseline(pair[0], store, "production", (artifacts,), review=pair[1])
        except BaselineError:
            return "stale"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, candidates))
    assert sorted(results) == ["accepted", "stale"]
    assert verify_ledger(store, "production").decisions == 2


def test_check_rejects_unaccepted_candidate_and_changed_live_bytes(tmp_path: Path) -> None:
    first_artifact = tmp_path / "first.gguf"
    first_artifact.write_bytes(b"first")
    first = _write_manifest(tmp_path, "first", first_artifact)
    second_artifact = tmp_path / "second.gguf"
    second_artifact.write_bytes(b"second")
    second = _write_manifest(tmp_path, "second", second_artifact)
    store = tmp_path / "store"
    review = tmp_path / "review.json"
    review_baseline(first, store, "production", (tmp_path,), review, initial=True)
    accept_baseline(first, store, "production", (tmp_path,), review=review)
    with pytest.raises(BaselineError, match="not the exact current"):
        check_baseline(second, store, "production", (tmp_path,))
    first_artifact.write_bytes(b"changed")
    with pytest.raises(BaselineError, match="digest mismatch"):
        check_baseline(first, store, "production", (tmp_path,))


def test_check_rejects_legacy_python_api_acceptance(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.gguf"
    artifact.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, "legacy", artifact)
    store = tmp_path / "store"
    accept_baseline(manifest, store, "production", (tmp_path,))
    with pytest.raises(BaselineError, match="legacy unreviewed decision"):
        check_baseline(manifest, store, "production", (tmp_path,))
