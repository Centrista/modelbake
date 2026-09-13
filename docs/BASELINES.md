# Accepted baselines

ModelBake's local baseline ledger records one narrow fact: an owner accepted a
succeeded run manifest after ModelBake recomputed its artifact digests inside
explicit, owner-selected trusted roots.

## CLI

```bash
modelbake baseline review CANDIDATE --store STORE --name NAME --review REVIEW.json --allow-artifact-root ROOT [--initial]
modelbake baseline accept CANDIDATE --store STORE --name NAME --review REVIEW.json --allow-artifact-root ROOT [--note NOTE]
modelbake baseline check CANDIDATE --store STORE --name NAME --allow-artifact-root ROOT
modelbake baseline current --store STORE --name NAME
modelbake baseline verify --store STORE --name NAME
modelbake baseline compare CANDIDATE --store STORE --name NAME [--markdown | --json]
```

`review`, `accept`, and `check` live-verify artifact bytes and require at least one
explicit `--allow-artifact-root` (repeat it for multiple roots).
`review` binds the exact candidate manifest bytes, current decision digest, project,
and deterministic recorded comparison into a canonical receipt. `accept --review`
rechecks all of them under the ledger lock, recomputes the comparison, and rejects
a stale predecessor, changed candidate, or changed live artifact. This is a
compare-and-swap release decision, not a human approval or quality result.

Use `--initial` only when deliberately reviewing the first release of an empty
channel. `check` succeeds only when the exact candidate is the current accepted
manifest and its live artifact bytes still match.

The CLI requires `--review`. Python `accept_baseline(..., review=None)` remains
only for legacy v1-ledger compatibility; decisions created that way cannot pass
`baseline check`. Release workflows must consume the action-created receipt.

`current` and `verify` check the stored ledger and exact stored manifest bytes.
`compare` reads the candidate manifest and compares its recorded data with the
current baseline; it does not inspect candidate artifact bytes or decide promotion.

Example recurring loop:

```bash
modelbake demo --story baseline --run-dir .modelbake/demo-first
modelbake verify .modelbake/demo-first/manifest.json --allow-artifact-root .modelbake/cache
modelbake baseline review .modelbake/demo-first/manifest.json --store .modelbake/baselines --name demo --review .modelbake/first-review.json --allow-artifact-root .modelbake/cache --initial
modelbake baseline accept .modelbake/demo-first/manifest.json --store .modelbake/baselines --name demo --allow-artifact-root .modelbake/cache --review .modelbake/first-review.json --note "first local fixture run"
modelbake baseline check .modelbake/demo-first/manifest.json --store .modelbake/baselines --name demo --allow-artifact-root .modelbake/cache
modelbake demo --story candidate --run-dir .modelbake/demo-second
modelbake verify .modelbake/demo-second/manifest.json --allow-artifact-root .modelbake/cache
modelbake baseline compare .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --markdown
modelbake baseline review .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --review .modelbake/second-review.json --allow-artifact-root .modelbake/cache
modelbake baseline accept .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --review .modelbake/second-review.json --allow-artifact-root .modelbake/cache
modelbake baseline check .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --allow-artifact-root .modelbake/cache
```

The baseline story builds Q4. The candidate story keeps Q4 and adds Q8, so the
comparison contains four digest-verified cache reuses and two added nodes.

## Python API

```python
from modelbake.baseline import accept_baseline, check_baseline, review_baseline

reviewed = review_baseline(
    ".modelbake/runs/candidate/manifest.json",
    ".modelbake/baselines",
    "production",
    allowed_roots=(".modelbake/runs/candidate/artifacts",),
    review=".modelbake/runs/candidate/review.json",
)

accepted = accept_baseline(
    ".modelbake/runs/candidate/manifest.json",
    ".modelbake/baselines",
    "production",
    allowed_roots=(".modelbake/runs/candidate/artifacts",),
    note="approved after eval suite 2026-09-12",
    review=reviewed.path,
)
checked = check_baseline(
    ".modelbake/runs/candidate/manifest.json",
    ".modelbake/baselines",
    "production",
    allowed_roots=(".modelbake/runs/candidate/artifacts",),
)
```

Each exact manifest and consumed review byte sequence is stored once under its
content digest. Each new reviewed decision references the exact receipt and
previous decision digest, creating an append-only, hash-chained channel history.
The `current.json` pointer is atomically replaced and always identifies the
terminal verified decision. Re-accepting the exact current manifest with the same
receipt is idempotent and does not rewrite its note or add a decision.

The store is local and owner-private: directories created by ModelBake use mode
`0700` and files use `0600`. ModelBake does not change the mode of a store root
that the caller created. Channel names are filename-safe, and stored symlinks,
special files, malformed records, chain gaps, digest mismatches, or a mismatched
current pointer fail closed.

`verify_ledger` checks stored manifests, receipts, and both v1 and v2 decision
records; it does not
re-verify live artifacts because no artifact trust roots are implicit. Artifact
bytes are recomputed only at `accept_baseline`, where roots are mandatory.

This ledger is portable JSON and preserves the exact accepted manifest bytes.
It is not a signature, identity system, authorization layer, transparency log,
remote consensus protocol, compatibility result, quality result, or proof that
the person making the decision was entitled to do so. A note is manual,
single-line input only; never put secrets in it.
