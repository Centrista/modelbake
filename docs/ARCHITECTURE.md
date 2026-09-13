# Architecture

ModelBake is a small build system with a strict planner, a deterministic DAG engine, a content-addressed local cache, typed execution adapters, and an append-as-it-runs manifest. The governing idea is: **a model release is a build graph, not a file.**

## Data flow

```text
strict YAML recipe
       │ validate and normalize
       ▼
    BuildPlan ── deterministic topological order
       │
       ├── source ──► convert F16 ──► smoke F16
       │                    ├───────► quantize Q4_0 ──► smoke Q4_0
       │                    └───────► quantize Q8_0 ──► smoke Q8_0
       │
       ▼
 typed adapter ──► work directory ──► digest-checked atomic cache commit
       │                                      │
       └──────── observations + argv ─────────┤
                                              ▼
                                      run manifest.json
```

## Components

### Configuration and planning

`src/modelbake/config.py` loads `modelbake/v0` YAML with a safe loader that rejects duplicate fields. Every mapping has an explicit allowlist. The planner currently accepts a local Llama source in fixture or safetensors format, F16/Q4_0/Q8_0 targets, one smoke prompt, and either the fixture or `llamacpp` adapter.

The planner hashes canonical recipe data and the source filesystem tree, then emits immutable `NodeSpec` values. It creates an F16 intermediate automatically when the recipe only requests quantized outputs.

### Graph validation

`src/modelbake/graph.py` validates path-safe unique node IDs, known and non-duplicated dependencies, and acyclicity. A heap-backed topological sort gives deterministic dependency-first ordering. Invalid graphs fail before adapter execution.

### Engine

`src/modelbake/engine.py` executes nodes in graph order. A node cache key covers:

- its full node specification;
- the root source digest where applicable;
- dependency output digests;
- the plan toolchain declaration;
- the adapter fingerprint;
- declared and observed runner fingerprints.

A changed branch therefore invalidates that node and its descendants while preserving unaffected siblings. Failed nodes block descendants, but do not silently invent outputs. The engine writes `manifest.json` atomically after every state transition. Interruption preserves completed cache entries; a later invocation recomputes the plan and can reuse entries after their bytes match the cache record.

### Cache

`src/modelbake/cache.py` stores immutable entries under a SHA-256-derived directory. Adapter outputs are copied from a constrained output root into a temporary entry, hashed, described by a cache manifest, and atomically renamed into place. Every cache load rehashes outputs. A corrupt entry fails its node and is not silently treated as a miss.

The cache is local and single-host in v0. It has no garbage collector, quota policy, remote transport, signature, access control, or multi-writer protocol beyond same-filesystem atomic rename behavior.

### Adapters

`src/modelbake/adapters/base.py` separates graph execution from tool execution. The engine never interprets command strings. An adapter receives a typed node, paths, and dependency outputs, and may return only observed outputs, argv, observations, and a narrow terminal state.

The fixture adapter exercises engine behavior without pretending to create model artifacts. The `llamacpp` adapter copies a local source, runs conversion, runs supported quantization commands, and smoke-executes each artifact. See [ADAPTERS.md](ADAPTERS.md).

### Manifest and verification

`src/modelbake/manifest.py` loads only `modelbake.run.v1`, renders a scoped text report, and can recompute digests for output paths when the caller supplies explicit trusted roots. Live verification checks bytes, not re-execution or semantics. See [EVIDENCE_MODEL.md](EVIDENCE_MODEL.md).

The v0 external `llama.cpp` runner lifecycle is POSIX-only. In particular, timeout
cleanup relies on POSIX process groups; Windows external-runner operation is not supported.

## Filesystem layout

```text
.modelbake/
├── cache/entries/<node-key>/
│   ├── manifest.json
│   └── outputs/<declared-output>
└── runs/<run-id>/
    ├── manifest.json
    └── work/<node-id>/
```

Custom `--cache-dir` and `--run-dir` values replace these defaults. Successful manifest output paths currently reference cached artifacts, so moving or deleting the cache can make later `verify` calls report missing outputs.

## Trust model

Configuration parsing is restrictive, but the system is not a sandbox. Explicitly configured tools are trusted code. The engine observes their process status and output bytes; it does not prove their source, intent, isolation, or correctness. Tool executable digests, adapter implementation digest, configured Python interpreter identity, bounded direct converter dependency-version metadata, observed Git checkout identity when available, and the supplied revision enter the fingerprint. The engine checks that fingerprint again after execution before committing outputs. Installed dependency files are not exhaustively hashed, so this record makes version drift visible without claiming exact dependency-code identity or authenticity.
