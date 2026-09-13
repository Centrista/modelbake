# Two-minute ModelBake demo script

Use this as a live terminal script. Select the ending that matches the launch's locked evidence mode. Commands assume the repository root and a prepared virtual environment.

## Preflight

Before going live:

```bash
source .venv/bin/activate
modelbake --help
pytest
```

Clear prior demo outputs only through the team's approved, explicit cleanup procedure; do not improvise a broad delete command during the demo. Confirm that no terminal, manifest, report, or browser view exposes private paths, secrets, prompts, or model data.

## Spoken script and commands

### 0:00 — thesis

Say:

> A model release is a build graph, not a file. The checkpoint is an input. Conversion, quantization, the runner, and the evidence of what happened are the release.

### 0:12 — plan before execution

Run:

```bash
modelbake plan examples/modelbake.fixture.yaml
```

Say:

> ModelBake compiles a strict recipe into a deterministic graph before executing it. This recipe cannot add arbitrary shell commands or arguments.

Point out the source, conversion, Q4_0/Q8_0 branches, and separate smoke nodes.

### 0:32 — run the fixture

Run:

```bash
modelbake demo --story baseline --run-dir .modelbake/demo-first
modelbake baseline review .modelbake/demo-first/manifest.json --store .modelbake/baselines --name demo --review .modelbake/demo-first/review.json --allow-artifact-root .modelbake/cache --initial
modelbake baseline accept .modelbake/demo-first/manifest.json --store .modelbake/baselines --name demo --review .modelbake/demo-first/review.json --allow-artifact-root .modelbake/cache
```

Say:

> This quickstart is a private deterministic fixture format. It is not GGUF, and it launches no model runtime. It exists to exercise the graph, cache, manifest, report, and verification path without credentials or downloads.

Point to `fixture_observed`. The baseline story contains F16 and Q4 nodes.

Say:

> The state is deliberately named fixture-observed. Synthetic success cannot borrow a compatibility claim.

### 0:58 — inspect and verify

Run:

```bash
modelbake status .modelbake/demo-first/manifest.json
modelbake verify .modelbake/demo-first/manifest.json --allow-artifact-root .modelbake/cache
modelbake report .modelbake/demo-first/manifest.json --html .modelbake/demo-first/report.html
```

Say:

> The manifest records what ran, the resulting digests, and the claim boundary. Verify checks that current output bytes match the manifest. It does not re-run the tools or evaluate model behavior.

### 1:20 — demonstrate the next release

Run:

```bash
modelbake demo --story candidate --run-dir .modelbake/demo-second
modelbake verify .modelbake/demo-second/manifest.json --allow-artifact-root .modelbake/cache
modelbake baseline compare .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo
modelbake baseline review .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --review .modelbake/demo-second/review.json --allow-artifact-root .modelbake/cache
modelbake baseline accept .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --review .modelbake/demo-second/review.json --allow-artifact-root .modelbake/cache
modelbake baseline check .modelbake/demo-second/manifest.json --store .modelbake/baselines --name demo --allow-artifact-root .modelbake/cache
```

Say:

> The candidate keeps Q4 and adds Q8. Five nodes come from digest-verified cache entries and two are new. The receipt binds this exact comparison to the accepted predecessor; acceptance fails if either side changes after review. Check then re-reads the live output bytes. None of this evaluates model quality.

## Ending A — `REAL_ACCEPTANCE`

Open the sanitized public evidence at `<EVIDENCE_URL>`; do not display the private local manifest.

Say:

> The real adapter is intentionally narrow, POSIX-only in v0, and still experimental. Use this ending only after a fresh release-revision run has cleared Gate R. In that scoped run, describe only the displayed recorded outputs and runner observations.

Point to the runner name, revision, node states, and evidence exclusions.

Say:

> That is one observation about one source, invocation, toolchain, and host. It is not a model-quality result, an equivalence test, a broad compatibility statement, a security or license check, or production readiness.

Close:

> Claim only what ran. Try the fixture, inspect the manifest, and if you repeatedly ship GGUF variants, bring us one real workflow or reproducible failure.

## Ending B — `FIXTURE_ONLY`

Open `examples/modelbake.llamacpp.yaml` and keep its placeholder tool paths visible.

Say:

> There is an experimental POSIX-only adapter for a narrow local Llama safetensors-to-GGUF route using explicit local llama.cpp tools. This launch does not present that route as accepted because a fresh sanitized release-revision artifact has not cleared the release gate.

Close:

> Claim only what ran. Today, that is the fixture-backed graph, cache, manifest, report, and verification machinery. Try it, inspect the evidence model, and help us validate a real route.

## Questions to invite

Ask only one:

> If you repeatedly build GGUF variants, which release, rollback, or debugging decision should consume this manifest—and what evidence is missing?

## Demo failure handling

- If `plan` fails, show the configuration error and stop; do not switch to screenshots without saying so.
- If the first fixture run fails, end the launch demo and investigate. The fixture baseline is a release gate.
- If verification fails, say the bytes did not match and stop. Never narrate a red state as success.
- If the real evidence page is unavailable, switch verbally and visually to `FIXTURE_ONLY` before continuing.
- If asked whether generated text is “correct,” answer that ModelBake did not evaluate correctness and the smoke node only recorded process exit.
