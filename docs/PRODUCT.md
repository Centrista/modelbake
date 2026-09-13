# Product

## The one-sentence product

ModelBake turns a local checkpoint release into a pinned, cached, inspectable build graph and records whether each output ran on the named runner.

## The aha moment

**A model release is a build graph, not a file.** Teams already know that application binaries are products of builds; open-model artifacts deserve the same treatment. The checkpoint is an input. Conversion, quantization, runner execution, and evidence are the release.

Git is still the source history. ModelBake is the release check around the bytes
Git normally does not contain: model input, converter, runner, outputs, and the
accepted predecessor. A review receipt makes that distinction executable by
rejecting a candidate when its bytes or predecessor changed after review.

## Beachhead user

The first user is the engineer who publishes or deploys open-weight Llama-family models through `llama.cpp` and currently maintains bespoke scripts. Their recurring pain is not “how do I convert once?” It is “which source, tool build, flags, artifact, and runner produced the thing we shipped—and can I reuse the work safely?”

ModelBake is not initially for hosted API-only teams, arbitrary training pipelines, model evaluation researchers, or users seeking a universal format converter.

## Core workflow

1. Point a strict recipe at a local safetensors checkpoint and explicit local tools.
2. Inspect the compiled graph before anything executes.
3. Build F16 and selected Q4_0/Q8_0 artifacts through one command.
4. See a separate smoke-execution result for every target.
5. Reuse cache entries only after their stored bytes match their cache records.
6. attach the manifest to a release, CI run, or incident review.

The capitalized value is not the command count. It is that the output, lineage, cache reuse, and claim boundary arrive together.

## Interface principles

- **Graph first.** Show source → conversion → quantization → execution as the primary object.
- **One human question per view.** What will run? What ran? Which recorded output digests changed? What is not proven?
- **Plain labels.** Prefer “Ran on this runner” over ambiguous “Validated.”
- **Evidence beside scope.** A green result must display the named runner and an adjacent non-claim.
- **Details on demand.** The default view shows status and lineage; generated argv with
  sensitive prompt values digest-redacted, digests, versions, and bounded logs are one
  level deeper.
- **No synthetic certainty.** Unknown and unavailable values remain visible instead of being replaced with confident prose.

## Retention hypothesis

Model conversion alone is episodic. Retention must come from occupying the release path: pull-request planning, reusable cache entries, artifact promotion, policy checks, and incident lookup. This is a hypothesis to validate, not a current property of the local v0.

The healthiest repeated behavior is “every qualifying model release invokes ModelBake automatically,” not “a user remembers to open a dashboard.” See [MOAT_EXPERIMENT.md](MOAT_EXPERIMENT.md).

## Product boundaries

ModelBake v0 is a local open-source alpha. It has no accounts, remote workers, registry, scheduler, access control, billing, fleet evidence, hosted cache, deployment integration, model evaluation suite, or production SLA. The fixture demo exercises the build machinery only and is visibly distinct from real `llama.cpp` evidence. External `llama.cpp` runner lifecycle support is POSIX-only in v0.
