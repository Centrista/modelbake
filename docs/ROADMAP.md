# Roadmap

The roadmap expands only when each new observation can be made precise. Dates and versions are directional, not commitments.

## Now: prove the local wedge

- stabilize the strict `modelbake/v0` recipe and `modelbake.run.v1` manifest;
- test the real adapter against pinned public Llama checkpoints and `llama.cpp` revisions;
- improve failure diagnostics without leaking unlimited tool output;
- document cache storage, cleanup, and migration behavior;
- make manifests portable enough to attach to CI artifacts;
- validate that teams repeat this on actual releases, not just demos.

## Next: make release evidence reusable

- verify the declared `llama.cpp` revision against observed source or build metadata;
- add signed or attestable manifest export without claiming source trust by default;
- separate artifact storage from run records so evidence remains valid after cache cleanup;
- add explicit hardware/runtime observations needed for scoped performance evidence;
- test the optional self-hosted GitHub Actions example with explicit artifact roots and opt-in cache/evidence transfer;
- evaluate one additional runner route only after the Llama/`llama.cpp` wedge has real use.

## Later: earn networked value

- a remote, content-addressed cache with access control and retention policy;
- an execution fleet that can run the same artifact on named hardware/runtime profiles;
- organization policies for required targets and evidence before promotion;
- a queryable corpus of anonymized failure and compatibility observations;
- registry and deployment integrations that make the manifest part of artifact promotion.

## Explicit non-goals for the current release

- universal model or runtime support;
- training orchestration;
- automated quality or safety certification;
- license compliance decisions;
- arbitrary user-defined shell steps;
- production deployment or autoscaling;
- claiming a moat before repeated use and accumulated evidence exist.

The decision to proceed from one phase to the next is governed by the falsifiable tests in [MOAT_EXPERIMENT.md](MOAT_EXPERIMENT.md), not feature volume.
