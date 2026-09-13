# Decision log

This log records the product and architecture choices behind the v0. It is not a promise that every hypothesis will survive contact with users.

## 2026-09-12 — Choose ModelBake

The team selected ModelBake after debating broader agent protocols, effect systems, trace-to-test tools, training-objective infrastructure, and disposable SaaS sandboxes. Those directions either collapsed into narrow utilities, required external authorization as an existential dependency, or lacked a believable path from a useful first build to repeated workflow ownership.

ModelBake won because the smallest honest implementation tests the graph, cache,
manifest, and evidence vocabulary locally. CI adoption, remote cache, and controlled
runners remain unproven hypotheses; remote transfer must be explicit and opt-in.

## 2026-09-12 — Narrow the first route

Decision: support local Hugging Face-style safetensors for the Llama architecture, pinned local `llama.cpp` tools, GGUF F16, Q4_0/Q8_0, and a smoke execution per target.

Reason: a hard-edged route can make precise statements and fail clearly. A nominally universal converter would create a shallow compatibility matrix and ambiguous evidence before any route was dependable.

Consequence: Mistral and other architectures, remote Hub inputs, other GGUF quantizations, other runners, arbitrary steps, and deployment are rejected rather than best-effort.

## 2026-09-12 — Separate fixture success from model success

Decision: provide a deterministic dependency-free fixture adapter using a private artifact format.

Reason: contributors need to exercise planning, branch invalidation, caching, interruption, tamper detection, and CLI reporting without downloading model weights or building `llama.cpp`.

Consequence: fixture UI and reports must never imply GGUF or runtime compatibility.

## 2026-09-12 — Use observed-state language

Decision: use `produced`, `fixture_observed`, `executed_on_runner`, `cache_hit`, `failed`, and `blocked` rather than “valid,” “compatible,” or “certified.”

Reason: process success and matching bytes are valuable observations, but they do not establish quality, equivalence, security, license compliance, universal compatibility, or production readiness.

## 2026-09-12 — Reject arbitrary recipe execution

Decision: recipes choose from a closed schema and cannot supply commands or extra argv.

Reason: a generic shell DAG would dilute the evidence contract and turn model inputs into a command-execution surface. Concrete adapters own argv construction and fingerprinting.

## 2026-09-12 — Treat the moat as an experiment

Decision: claim no day-one moat. Publish a 60-day behavioral test for repeated release use, evidence consumption, and measured cache value.

Reason: the engine can be copied and the market already includes Docker Model Runner, KitOps, Olive, Hugging Face Optimum, NVIDIA tooling, and mature CI systems. Defensibility must be earned through accumulated outcome evidence, controlled execution coverage, cache economics, and deep workflow integration.

Consequence: if teams do not return or consume manifests downstream, the platform expansion stops rather than being justified by more features.
