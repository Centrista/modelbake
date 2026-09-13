# Go-to-market

## Positioning

**Headline:** Know what changed before you ship the next model.

**Product sentence:** Turn a local checkpoint into pinned GGUF targets, keep the graph and evidence with the release, and review the next release against the last.

**Architecture thesis:** A model release is a build graph, not a file.

**Proof sentence:** ModelBake records observed bytes, exact tools and argv, cache lineage, and runner exit—not quality, equivalence, universal compatibility, or production readiness.

The useful analogy is “a build system for open-model releases.” Avoid “Docker for models,” “model certification,” and “works everywhere”; those phrases borrow guarantees the product does not provide.

## First market

Focus on inference-platform teams that ran at least four Llama-family GGUF builds in the prior eight weeks and own the release path for offline, local, or edge deployments:

- inference-platform teams supporting several local or edge products;
- applied-AI teams pinning open models for regulated or offline environments;
- model publishers producing recurring GGUF variants across several repositories.

The champion is a release/inference engineer; the budget owner is the platform engineering lead accountable for build compute and release incidents. Individual open-source maintainers are a distribution channel, not assumed paid demand. The trigger is a failed conversion, an unexplained artifact difference, a toolchain upgrade, or a request to reconstruct what shipped.

## The demo that earns attention

Use one small, public, redistributable checkpoint and show four moments in under three minutes. Until that real-toolchain acceptance run exists, show the fixture as engine evidence only and do not call it a GGUF demo:

1. `modelbake plan` reveals the graph before execution.
2. One build creates F16, Q4_0, and Q8_0 branches with scoped smoke results.
3. A second build reuses cache entries after their bytes match the cache records.
4. `modelbake compare` shows recorded output-digest, evidence, and cache-lineage changes; it does not read live artifacts.

End on the manifest's truth boundary. The credibility of the non-claims is part of the sale.

## Distribution loops

- **Open-source utility:** make the credential-free fixture demo instant, then provide one scoped public real-toolchain record after live verification on the release revision.
- **Release badge:** link to a human-readable manifest summary that names runner and scope; do not reduce it to an unexplained green check.
- **Optional CI example:** a team may explicitly add a self-hosted workflow that creates a graph diff and GitHub run artifact; remote cache/evidence transfer is opt-in.
- **Failure corpus:** publish anonymized, reproducible conversion failure classes and their exact toolchain boundaries.
- **Partner recipes:** collaborate with model maintainers and inference runtimes on pinned, reviewed routes rather than claiming generic support.

Each loop should bring another real build or evidence record, not only page views.

## Launch sequence

### Weeks 1–2: design partners

Recruit 10 teams that produced GGUF artifacts in the previous month. Observe their existing scripts and rebuild one real release per team. Record setup time, failed steps, graph reuse, and evidence they actually attach downstream.

### Weeks 3–4: public technical launch

Publish the repository, the real end-to-end example, a two-minute terminal/video walkthrough, and a technical post: “The checkpoint is an input, not a release.” Distribute where model publishers and inference engineers already work: GitHub, Hugging Face discussions, `llama.cpp` communities, MLOps practitioner groups, and direct maintainer outreach.

### Weeks 5–8: workflow insertion

Ship the smallest CI and artifact-promotion integration requested by repeated users. Measure automatic executions per repository and manifest consumption, not sign-ups.

## Metrics and kill signals

Primary metrics:

- time from clone to successful fixture and real build;
- qualifying model releases that invoke ModelBake automatically;
- digest-checked cache-hit rate and measured compute/time avoided;
- manifests consumed by a downstream review, registry, or deployment step;
- teams returning for a second and third release within 60 days.

Warning signals:

- users copy the generated command and remove ModelBake;
- builds happen only once per model with no workflow integration;
- setup cost exceeds the debugging time saved;
- users interpret smoke success as quality or universal compatibility;
- most demand is for an unsupported universal converter rather than release evidence.

## Business model hypothesis

Keep the recipe, local engine, manifest schema, and core adapters open. Controlled execution, remote cache, policy, or evidence retention are unbuilt hypotheses, not a hosted-service roadmap claim. Any future remote cache or evidence transfer must be separately opt-in and exportable. There is no paid product or pricing claim in v0.
