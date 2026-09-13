# Evidence model

ModelBake records observations without promoting them into broader claims. This is a product constraint, not a disclaimer added after the fact.

## Three layers of evidence

### 1. Provenance evidence

The plan and run manifest identify the canonical recipe digest, observed source-tree digest, node inputs, dependency output digests, cache keys, declared runner, and observed host fingerprint. The adapter fingerprint also enters each cache key; the `llamacpp` node observations expose its revision, tool byte digests, and available version output. Together these records answer “what did this run observe and use?” without treating a hash as an authenticity proof.

It does not establish publisher identity, source authenticity, legal rights, or a signed chain of custody.

### 2. Artifact evidence

`produced` means an adapter returned declared outputs and ModelBake copied and digested them into a cache entry. `cache_hit` means an existing cache entry's bytes matched its cache manifest when read. A manifest otherwise contains recorded output digests. A live `modelbake verify <manifest> --allow-artifact-root <trusted-root>` means current output bytes at the run manifest's paths matched those recorded digests within the caller-selected root.

None of these states means that the bytes are a valid GGUF, load in another runtime, preserve source behavior, or are free of malicious content.

### 3. Execution evidence

`fixture_observed` means the in-process test adapter decoded a private fixture artifact and wrote deterministic synthetic observations. It launches no model runtime or subprocess and provides no `llama.cpp` evidence.

`executed_on_runner` means the real adapter launched its internally constructed runner argv and the process returned exit code 0 within the configured timeout. The manifest records the command and observations. For `llama.cpp`, smoke execution uses the recipe prompt, requested token cap, and seed 0.

External `llama.cpp` runner lifecycle handling is POSIX-only in v0. A manifest
recorded on POSIX is not evidence of Windows support.

This is an observation about one artifact, one invocation, one toolchain, and one host. It is not a universal compatibility result, a performance benchmark, a quality evaluation, or a production-readiness certificate.

## State semantics

| State | Positive observation | Must not be inferred |
| --- | --- | --- |
| `pending` | Node is declared but no terminal observation exists yet. | Execution has started. |
| `running` | Reserved execution state. | Progress, success, or output validity. |
| `produced` | Declared output bytes were returned, copied, and digested. | Validity, compatibility, equivalence, quality. |
| `fixture_observed` | The in-process fixture adapter decoded its private test format and emitted synthetic observations. | Any model-runtime execution or compatibility. |
| `executed_on_runner` | Recorded runner process returned exit code 0. | Correct generation, cross-runner support, production fitness. |
| `cache_hit` | Cached bytes matched the cache entry manifest. | The original tool was re-run in this invocation. |
| `failed` | Execution or cache verification did not complete successfully. | Root cause beyond the recorded error. |
| `blocked` | A dependency failed or was blocked; this node did not run. | This node itself is incompatible or defective. |

## Digest scope

Filesystem tree digests cover names, object kinds, permission bits, symlink targets, and regular-file bytes. Special objects are rejected. Source configuration rejects source-tree symlinks, while the generic hashing layer can represent a symlink target without following it.

SHA-256 detects accidental or adversarial byte changes relative to a recorded value. A digest alone provides no signer identity, timestamp authority, confidentiality, or proof that the original bytes were trustworthy.

## Toolchain scope

The real adapter fingerprint records tool paths and byte digests, tool version output where available, the adapter identity, the named runner, and the recipe's revision string. The revision is caller-supplied; v0 does not compare it with a Git checkout or signature. Python, converter, quantizer, and runner remain trusted executables.

## Explicit exclusions

No ModelBake v0 state, badge, report, or manifest asserts:

- output quality or task accuracy;
- numerical or behavioral equivalence to the source checkpoint;
- compatibility outside the exact recorded execution;
- safety, absence of malicious code, or model alignment;
- license, export-control, privacy, or policy compliance;
- optimal quantization or performance;
- reproducibility across hardware, operating systems, or tool versions;
- production readiness.

Future evidence types must name their observation, scope, and nearest non-claim before they can enter the public vocabulary.
