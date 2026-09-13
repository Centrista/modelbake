# Adapters

Adapters are the only layer allowed to turn a validated node into external tool execution. The build engine handles graph order, cache keys, output containment, digests, and manifests; it does not parse or run command strings.

## Contract

An adapter implements two methods:

- `fingerprint()` returns JSON-compatible identity used in node cache keys;
- `run(context)` writes outputs below `context.output_dir` and returns an `AdapterExecution`.

A successful execution declares at least one output and uses only `produced` or `executed_on_runner`. The cache independently copies and hashes returned paths. Exceptions become a failed node, and descendants become blocked.

## Fixture adapter

`modelbake.fixture/v0` exercises source, conversion, quantization, smoke execution, caching, and verification without third-party binaries. It emits a private `modelbake-fixture-v0` byte format and deterministic JSON observation.

Fixture outputs are intentionally not GGUF. A successful fixture run is evidence about ModelBake's graph machinery, not evidence about a model or `llama.cpp`.

## `llama.cpp` adapter

The v0 real adapter is deliberately constrained. The recipe supplies absolute paths to Python, `convert_hf_to_gguf.py`, `llama-quantize`, and `llama-cli`, plus a revision string. It cannot supply commands or extra arguments.

ModelBake constructs these operations internally:

```text
<python> <convert> <local-source> --outfile <output> --outtype f16
<quantize> <f16-input> <output> Q4_0|Q8_0
<runner> -m <artifact> -p <prompt> -n <max-tokens> --seed 0
```

All subprocesses use argv arrays with `shell=False`, no stdin, a controlled environment,
a configured timeout, and a shared configured cap for retained stdout/stderr. Timeouts
terminate the process group on POSIX so child processes do not outlive the failed node.
The adapter records generated argv, exit status, retained output, truncation status,
tool byte digests, available version output, Python interpreter and converter-dependency
version metadata, and the supplied revision. The smoke prompt value in recorded argv is
replaced by its SHA-256 digest. Smoke stdout is still stored as the node's execution
observation output and may contain prompt text, so manifests and logs must be handled as
potentially sensitive.

The Python environment record is intentionally bounded. It records interpreter identity
and installed-version metadata (or explicit absence) for `gguf`, `numpy`, `protobuf`,
`sentencepiece`, `torch`, and `transformers`, the direct dependencies of the supported
converter route. It does not recursively hash `site-packages`, and therefore does not
claim the exact bytes of installed dependency code. The configured interpreter and
converter files remain byte-digested independently.

The adapter trusts all configured executables. The recorded revision string is not validated against a Git checkout. Successful process exit is not a semantic model evaluation.

ModelBake v0 supports the external `llama.cpp` process lifecycle on POSIX only.
The fixture adapter remains usable for engine testing elsewhere, but Windows external
runner execution is outside the v0 support boundary.

## Adding an adapter

Propose the evidence contract before code. The design must answer:

1. What exact input formats and architectures are accepted?
2. Which process or library produces each output?
3. What observation justifies each terminal state?
4. Which tools, code, settings, environment, and hardware must affect the fingerprint?
5. Which outputs are cacheable, and how are they contained?
6. What is the closest misleading claim, and how will the UI and docs prevent it?
7. How do timeout, partial output, corruption, interruption, and tool replacement fail?

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the required implementation and test checklist.
