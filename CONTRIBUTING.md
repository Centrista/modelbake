# Contributing to ModelBake

ModelBake welcomes focused contributions that make open-model release evidence more reproducible and more honest. The project is intentionally narrow; a larger feature is not automatically a better feature.

## Development setup

```bash
git clone <YOUR_FORK_URL> modelbake
cd modelbake
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[dev]'
pytest
```

Before opening a pull request, also run:

```bash
python -m compileall -q src tests
modelbake plan examples/modelbake.fixture.yaml
modelbake demo --run-dir .modelbake/contributor-demo
modelbake verify .modelbake/contributor-demo/manifest.json --allow-artifact-root .modelbake/cache
```

## What belongs here

Good changes strengthen at least one of these properties:

- deterministic planning and cache keys;
- byte-level provenance and verification;
- explicit runner/toolchain identity;
- safe, typed adapter boundaries;
- legible evidence and equally legible exclusions;
- failure behavior that never upgrades uncertainty into a claim.

Start with an issue before implementing a new model architecture, artifact format, execution backend, remote source, or recipe field. Each expands the product's truth boundary and needs an evidence design, not just an implementation.

## Evidence-language rule

Use observational language in code, tests, docs, and UI. `executed_on_runner` means one recorded process returned success. It does not mean “compatible,” “validated,” “safe,” “equivalent,” “optimized,” or “production ready.” A contribution that adds a new positive claim must include:

1. the exact observation supporting it;
2. the scope in which it is true;
3. a negative statement preventing the nearest likely misinterpretation;
4. tests for failure and tampering paths.

See [docs/EVIDENCE_MODEL.md](docs/EVIDENCE_MODEL.md).

## Adapter checklist

New adapters must:

- implement the typed `Adapter` interface;
- provide a JSON-compatible fingerprint that changes when behaviorally relevant tools or settings change;
- construct argv internally and invoke no recipe-supplied command;
- use `shell=False` for child processes;
- cap captured output and enforce timeouts;
- write declared outputs only beneath `context.output_dir`;
- return `produced` or `executed_on_runner` only after observing the corresponding event;
- include cold, warm-cache, failure, timeout, tamper, and command-injection tests;
- document trusted dependencies and unsupported cases.

## Pull requests

Keep changes small enough to review as one evidence claim. Include the motivation, the old and new truth boundary, test output, and any compatibility or migration concern. Update documentation and examples in the same pull request when behavior changes.

Do not commit model weights, `llama.cpp` binaries, cache entries, run artifacts, secrets, or credentials. Never include a private checkpoint or prompt in a test fixture.

By contributing, you agree that your contribution is licensed under Apache-2.0.
