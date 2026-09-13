"""Deterministic, dependency-free adapter used to exercise the complete build graph.

Fixture artifacts are intentionally *not* GGUF files.  Their private magic and the
``fixture_format`` observations prevent a successful demo from being presented as
evidence of llama.cpp or model compatibility.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..hashing import tree_digest
from ..types import NodeState
from .base import Adapter, AdapterContext, AdapterError, AdapterExecution

FIXTURE_MAGIC = b"MODELBAKE_FIXTURE_V0\x00"
FIXTURE_RUNNER = "modelbake.fixture/v0"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    return _sha256(path.read_bytes())


def _source_files(path: Path) -> Iterable[tuple[str, bytes]]:
    if path.is_file():
        yield path.name, path.read_bytes()
        return
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        yield item.relative_to(path).as_posix(), item.read_bytes()


def _bundle_source(path: Path) -> bytes:
    payload = bytearray()
    for relative, content in _source_files(path):
        name = relative.encode("utf-8")
        payload.extend(struct.pack(">I", len(name)))
        payload.extend(name)
        payload.extend(struct.pack(">Q", len(content)))
        payload.extend(content)
    return bytes(payload)


def _encode_artifact(metadata: dict[str, Any], payload: bytes) -> bytes:
    header = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return FIXTURE_MAGIC + struct.pack(">I", len(header)) + header + payload


def _decode_artifact(data: bytes) -> tuple[dict[str, Any], bytes]:
    if not data.startswith(FIXTURE_MAGIC) or len(data) < len(FIXTURE_MAGIC) + 4:
        raise AdapterError("input is not a ModelBake fixture artifact")
    offset = len(FIXTURE_MAGIC)
    header_size = struct.unpack(">I", data[offset : offset + 4])[0]
    offset += 4
    end = offset + header_size
    if end > len(data):
        raise AdapterError("fixture artifact has a truncated header")
    try:
        metadata = json.loads(data[offset:end])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("fixture artifact has an invalid header") from exc
    if not isinstance(metadata, dict):
        raise AdapterError("fixture artifact header must be an object")
    return metadata, data[end:]


def _one_dependency_output(context: AdapterContext) -> Path:
    outputs = [
        path
        for dependency in context.dependencies.values()
        for path in dependency.outputs.values()
    ]
    if len(outputs) != 1:
        raise AdapterError(f"{context.node.id} requires exactly one dependency output")
    return Path(outputs[0])


class FixtureAdapter(Adapter):
    """Run source, conversion, quantization, and smoke nodes deterministically."""

    def fingerprint(self) -> dict[str, Any]:
        return {
            "adapter": FIXTURE_RUNNER,
            "revision": "v0",
            "implementation_digest": tree_digest(__file__),
            "artifact_format": "modelbake-fixture-v0",
        }

    def run(self, context: AdapterContext) -> AdapterExecution:
        context.output_dir.mkdir(parents=True, exist_ok=True)
        handlers = {
            "source": self._source,
            "convert": self._convert,
            "quantize": self._quantize,
            "smoke": self._smoke,
        }
        try:
            handler = handlers[context.node.kind]
        except KeyError as exc:
            raise AdapterError(f"fixture adapter does not implement node kind {context.node.kind!r}") from exc
        return handler(context)

    def _source(self, context: AdapterContext) -> AdapterExecution:
        source = Path(str(context.node.config["path"]))
        if not source.exists():
            raise AdapterError(f"fixture source does not exist: {source}")
        expected_digest = str(context.node.config["digest"])
        if tree_digest(source) != expected_digest:
            raise AdapterError("source bytes changed after planning")
        payload = _bundle_source(source)
        if tree_digest(source) != expected_digest:
            raise AdapterError("source bytes changed while being copied")
        output = context.output_dir / "source.fixture-bundle"
        output.write_bytes(payload)
        return AdapterExecution(
            outputs={"source": output},
            command=(),
            observations={
                "runner": FIXTURE_RUNNER,
                "operation": "source_bundled",
                "bytes": len(payload),
                "digest": _file_digest(output),
            },
            state=NodeState.PRODUCED,
        )

    def _convert(self, context: AdapterContext) -> AdapterExecution:
        source = _one_dependency_output(context)
        source_bytes = source.read_bytes()
        quantization = str(context.node.config.get("quantization", "F16"))
        metadata = {
            "fixture_format": "modelbake-fixture-v0",
            "operation": "convert",
            "quantization": quantization,
            "source_digest": _sha256(source_bytes),
        }
        output = context.output_dir / f"model-{quantization}.fixture-artifact"
        output.write_bytes(_encode_artifact(metadata, source_bytes))
        return AdapterExecution(
            outputs={"artifact": output},
            command=(),
            observations={
                "runner": FIXTURE_RUNNER,
                "operation": "fixture_bytes_produced",
                "fixture_format": "modelbake-fixture-v0",
                "quantization": quantization,
                "digest": _file_digest(output),
            },
            state=NodeState.PRODUCED,
        )

    def _quantize(self, context: AdapterContext) -> AdapterExecution:
        source = _one_dependency_output(context)
        metadata, payload = _decode_artifact(source.read_bytes())
        quantization = str(context.node.config["quantization"])
        # A deterministic byte transformation makes this a genuine build artifact
        # while the private format prevents any inference about model quality.
        transformed = hashlib.sha256(quantization.encode("ascii") + payload).digest() + payload[::2]
        output_metadata = {
            "fixture_format": "modelbake-fixture-v0",
            "operation": "quantize",
            "quantization": quantization,
            "input_digest": _file_digest(source),
            "input_metadata": metadata,
        }
        output = context.output_dir / f"model-{quantization}.fixture-artifact"
        output.write_bytes(_encode_artifact(output_metadata, transformed))
        return AdapterExecution(
            outputs={"artifact": output},
            command=(),
            observations={
                "runner": FIXTURE_RUNNER,
                "operation": "fixture_bytes_produced",
                "fixture_format": "modelbake-fixture-v0",
                "quantization": quantization,
                "digest": _file_digest(output),
            },
            state=NodeState.PRODUCED,
        )

    def _smoke(self, context: AdapterContext) -> AdapterExecution:
        artifact = _one_dependency_output(context)
        metadata, payload = _decode_artifact(artifact.read_bytes())
        prompt = str(context.node.config["prompt"])
        max_tokens = int(context.node.config["max_tokens"])
        seed = payload + prompt.encode("utf-8")
        observed_tokens = [
            hashlib.sha256(seed + index.to_bytes(8, "big")).hexdigest()[:8]
            for index in range(max_tokens)
        ]
        record = {
            "schema": "modelbake/fixture-observation/v0",
            "runner": FIXTURE_RUNNER,
            "artifact_digest": _file_digest(artifact),
            "artifact_metadata": metadata,
            "prompt_digest": _sha256(prompt.encode("utf-8")),
            "max_tokens": max_tokens,
            "observed_tokens": observed_tokens,
        }
        output = context.output_dir / "execution-observation.json"
        output.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        return AdapterExecution(
            outputs={"observation": output},
            command=(),
            observations={
                "runner": FIXTURE_RUNNER,
                "operation": "fixture_artifact_executed",
                "artifact_digest": record["artifact_digest"],
                "observation_digest": _file_digest(output),
                "tokens_observed": len(observed_tokens),
            },
            state=NodeState.FIXTURE_OBSERVED,
        )
