"""Constrained llama.cpp adapter.

Every process is assembled by ModelBake from validated fields and invoked as an
argv array with ``shell=False``. Recipes cannot supply arguments or commands.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..hashing import digest_json, tree_digest
from ..types import NodeState
from .base import Adapter, AdapterContext, AdapterError, AdapterExecution


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _one_dependency_output(context: AdapterContext) -> Path:
    outputs = [
        Path(path)
        for dependency in context.dependencies.values()
        for path in dependency.outputs.values()
        if Path(path).name != "execution.log"
    ]
    if len(outputs) != 1:
        raise AdapterError(f"{context.node.id} requires exactly one dependency output")
    return outputs[0]


# Direct runtime distributions declared by llama.cpp's supported HF converter route.
_CONVERTER_DISTRIBUTIONS = (
    "gguf",
    "numpy",
    "protobuf",
    "sentencepiece",
    "torch",
    "transformers",
)
_PYTHON_ENVIRONMENT_SCHEMA = "modelbake.llamacpp-python-environment.v2"


class LlamaCppAdapter(Adapter):
    """Run an explicitly pinned local llama.cpp toolchain."""

    def __init__(self, runner: Mapping[str, Any]):
        if os.name != "posix":  # pragma: no cover - v0 is intentionally POSIX-only
            raise AdapterError(
                "the v0 llamacpp adapter requires POSIX process-group semantics"
            )
        tools = runner.get("tools")
        if not isinstance(tools, Mapping):
            raise AdapterError("llamacpp adapter requires configured tool paths")
        expected = {"python", "convert", "quantize", "runner", "revision"}
        if set(tools) != expected:
            raise AdapterError("llamacpp tools must contain only python, convert, quantize, runner, revision")
        self.tools = {key: str(tools[key]) for key in expected}
        for key in ("python", "convert", "quantize", "runner"):
            path = Path(self.tools[key])
            if not path.is_absolute() or not path.resolve().is_file():
                raise AdapterError(f"configured {key} tool must be an existing absolute file")
            self.tools[key] = os.path.abspath(path)
        self.timeout_seconds = int(runner.get("timeout_seconds", 600))
        self.max_log_bytes = int(runner.get("max_log_bytes", 65_536))
        self.runner_name = str(runner.get("name", "llama.cpp"))

    def _version(self, executable: str) -> str:
        try:
            return_code, stdout, stderr, _, timed_out = self._run_capped(
                (executable, "--version"),
                Path.cwd(),
                timeout=min(self.timeout_seconds, 10),
                cap=512,
            )
            if timed_out:
                return "unavailable:TimeoutExpired"
            rendered = (stdout + stderr).decode("utf-8", errors="replace").strip()
            return rendered if return_code == 0 else f"reported-with-exit-{return_code}:{rendered}"
        except OSError as exc:
            return f"unavailable:{type(exc).__name__}"

    @staticmethod
    def _controlled_environment() -> dict[str, str]:
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "TMPDIR": tempfile.gettempdir(),
        }
        if os.name == "nt" and "SYSTEMROOT" in os.environ:  # pragma: no cover - Windows
            environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        tracking_id = os.environ.get("RUNNER_TRACKING_ID")
        if tracking_id:
            environment["RUNNER_TRACKING_ID"] = tracking_id
        return environment

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> int:
        """Terminate and reap the complete configured-tool process group."""

        if process.poll() is not None:
            return process.returncode
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows
                    process.kill()
            except ProcessLookupError:
                pass
            return process.wait()

    @staticmethod
    def _run_capped(
        argv: Sequence[str], cwd: Path, *, timeout: int, cap: int
    ) -> tuple[int, bytes, bytes, bool, bool]:
        """Drain both pipes while retaining at most ``cap`` bytes in total."""

        process = subprocess.Popen(
            list(argv),
            shell=False,
            cwd=cwd,
            env=LlamaCppAdapter._controlled_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        stdout = bytearray()
        stderr = bytearray()
        truncated = [False]
        lock = threading.Lock()

        def drain(stream: Any, destination: bytearray) -> None:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                with lock:
                    remaining = max(0, cap - len(stdout) - len(stderr))
                    destination.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated[0] = True

        assert process.stdout is not None and process.stderr is not None
        readers = (
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        )
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = LlamaCppAdapter._terminate_process_tree(process)
        except BaseException:
            LlamaCppAdapter._terminate_process_tree(process)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=5)
            process.stdout.close()
            process.stderr.close()
        return return_code, bytes(stdout), bytes(stderr), truncated[0], timed_out

    def fingerprint(self) -> dict[str, Any]:
        distribution_names = repr(_CONVERTER_DISTRIBUTIONS)
        package_probe = (
            "import importlib.metadata as metadata,json,sys\n"
            f"names={distribution_names}\n"
            "versions={}\n"
            "for name in names:\n"
            "  try: versions[name]=metadata.version(name)\n"
            "  except metadata.PackageNotFoundError: versions[name]=None\n"
            "payload={"
            f"'schema':'{_PYTHON_ENVIRONMENT_SCHEMA}',"
            "'interpreter':{"
            "'implementation':sys.implementation.name,"
            "'version':list(sys.version_info[:3]),"
            "'cache_tag':sys.implementation.cache_tag,"
            "'executable':sys.executable,"
            "'prefix':sys.prefix,"
            "'base_prefix':sys.base_prefix},"
            "'dependency_versions':versions}\n"
            "print(json.dumps(payload,sort_keys=True,separators=(',',':')))"
        )
        package_code, package_out, package_err, package_truncated, package_timeout = (
            self._run_capped(
                (self.tools["python"], "-c", package_probe),
                Path(self.tools["convert"]).parent,
                timeout=min(self.timeout_seconds, 15),
                cap=65_536,
            )
        )
        if package_timeout:
            reason = f"probe timed out after {min(self.timeout_seconds, 15)} seconds"
        elif package_truncated:
            reason = "probe output exceeded the retained-output limit"
        elif package_code != 0:
            reason = f"probe exited with status {package_code}"
        else:
            reason = "probe returned an invalid payload"
        package_payload: Any = None
        if package_code == 0 and not package_timeout and not package_truncated:
            try:
                package_payload = json.loads(package_out)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        expected_payload_keys = {"schema", "interpreter", "dependency_versions"}
        expected_interpreter_keys = {
            "implementation",
            "version",
            "cache_tag",
            "executable",
            "prefix",
            "base_prefix",
        }
        valid_payload = (
            package_code == 0
            and not package_timeout
            and not package_truncated
            and isinstance(package_payload, dict)
            and set(package_payload) == expected_payload_keys
            and package_payload.get("schema") == _PYTHON_ENVIRONMENT_SCHEMA
            and isinstance(package_payload.get("interpreter"), dict)
            and set(package_payload["interpreter"]) == expected_interpreter_keys
            and isinstance(package_payload["interpreter"].get("implementation"), str)
            and isinstance(package_payload["interpreter"].get("version"), list)
            and len(package_payload["interpreter"]["version"]) == 3
            and all(
                type(part) is int
                for part in package_payload["interpreter"]["version"]
            )
            and (
                package_payload["interpreter"].get("cache_tag") is None
                or isinstance(package_payload["interpreter"].get("cache_tag"), str)
            )
            and all(
                isinstance(package_payload["interpreter"].get(key), str)
                for key in ("executable", "prefix", "base_prefix")
            )
            and isinstance(package_payload.get("dependency_versions"), dict)
            and set(package_payload["dependency_versions"])
            == set(_CONVERTER_DISTRIBUTIONS)
            and all(
                version is None or isinstance(version, str)
                for version in package_payload["dependency_versions"].values()
            )
        )
        if not valid_payload:
            detail = package_err.decode(errors="replace").strip()[:512]
            suffix = f": {detail}" if detail else ""
            raise AdapterError(
                f"cannot fingerprint configured Python environment: {reason}{suffix}"
            )
        assert isinstance(package_payload, dict)
        repository: dict[str, Any] = {"available": False}
        current = Path(self.tools["convert"]).parent
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                git = shutil.which("git", path=os.defpath)
                if git is not None:
                    head_code, head_out, _, _, head_timeout = self._run_capped(
                        (git, "-C", str(candidate), "rev-parse", "HEAD"),
                        candidate,
                        timeout=10,
                        cap=512,
                    )
                    status_code, status_out, _, status_truncated, status_timeout = (
                        self._run_capped(
                            (
                                git,
                                "-C",
                                str(candidate),
                                "status",
                                "--porcelain=v1",
                                "--untracked-files=normal",
                            ),
                            candidate,
                            timeout=10,
                            cap=65_536,
                        )
                    )
                    observed_head = head_out.decode(errors="replace").strip()
                    if head_code != 0 or head_timeout:
                        raise AdapterError("cannot observe configured tool source revision")
                    if observed_head != self.tools["revision"]:
                        raise AdapterError(
                            "configured llama.cpp revision does not match the observed Git checkout"
                        )
                    worktree_digest = tree_digest(candidate)
                    repository = {
                        "available": head_code == 0 and not head_timeout,
                        "root": str(candidate.resolve()),
                        "observed_head": observed_head,
                        "declared_revision_matches_head": observed_head == self.tools["revision"],
                        "tracked_or_untracked_changes": bool(status_out.strip()),
                        "status_probe_exit_code": status_code,
                        "status_probe_timed_out": status_timeout,
                        "status_probe_truncated": status_truncated,
                        "status_digest": "sha256:" + hashlib.sha256(status_out).hexdigest(),
                        "worktree_digest": worktree_digest,
                    }
                break
        return {
            "adapter": "modelbake.llamacpp/v0",
            "implementation_digest": tree_digest(__file__),
            "runner_name": self.runner_name,
            "revision": self.tools["revision"],
            "tools": {
                key: {
                    "path": self.tools[key],
                    "resolved_path": str(Path(self.tools[key]).resolve()),
                    "configured_path_digest": tree_digest(self.tools[key]),
                    "resolved_file_digest": _digest(Path(self.tools[key]).resolve()),
                }
                for key in ("python", "convert", "quantize", "runner")
            },
            "python_environment": {
                "schema": package_payload["schema"],
                "probe_exit_code": package_code,
                "probe_timed_out": package_timeout,
                "probe_truncated": package_truncated,
                "interpreter": package_payload["interpreter"],
                "dependency_versions": package_payload["dependency_versions"],
                "dependency_versions_digest": digest_json(
                    package_payload["dependency_versions"]
                ),
            },
            "tool_source_repository": repository,
            "versions": {
                "python": self._version(self.tools["python"]),
                "quantize": self._version(self.tools["quantize"]),
                "runner": self._version(self.tools["runner"]),
            },
        }

    def _execute(
        self,
        argv: Sequence[str],
        cwd: Path,
        log_path: Path,
        *,
        sensitive_indices: frozenset[int] = frozenset(),
    ) -> tuple[tuple[str, ...], dict[str, Any]]:
        exact_argv = tuple(str(part) for part in argv)
        recorded_argv = tuple(
            (
                "<redacted:sha256:"
                + hashlib.sha256(part.encode("utf-8")).hexdigest()
                + ">"
                if index in sensitive_indices
                else part
            )
            for index, part in enumerate(exact_argv)
        )
        try:
            return_code, stdout, stderr, truncated, timed_out = self._run_capped(
                exact_argv,
                cwd,
                timeout=self.timeout_seconds,
                cap=self.max_log_bytes,
            )
        except OSError as exc:
            log_path.write_text(
                json.dumps(
                    {
                        "argv": list(recorded_argv),
                        "error": f"{type(exc).__name__}: {exc}",
                        "timeout_seconds": self.timeout_seconds,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            raise AdapterError(f"cannot execute configured tool {exact_argv[0]}: {exc}") from exc
        log_path.write_text(
            json.dumps(
                {
                    "argv": list(recorded_argv),
                    "exit_code": return_code,
                    "log_truncated": truncated,
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "timed_out": timed_out,
                    "timeout_seconds": self.timeout_seconds,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if timed_out:
            raise AdapterError(
                f"command timed out after {self.timeout_seconds}s; "
                f"stdout={stdout.decode(errors='replace')!r}; stderr={stderr.decode(errors='replace')!r}"
            )
        observations = {
            "exit_code": return_code,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "log_bytes_recorded": len(stdout) + len(stderr),
            "log_truncated": truncated,
            "timeout_seconds": self.timeout_seconds,
        }
        if return_code != 0:
            raise AdapterError(
                f"configured tool exited {return_code}; "
                f"stdout={observations['stdout']!r}; stderr={observations['stderr']!r}"
            )
        return recorded_argv, observations

    def run(self, context: AdapterContext) -> AdapterExecution:
        context.output_dir.mkdir(parents=True, exist_ok=True)
        handlers = {
            "source": self._source,
            "convert": self._convert,
            "quantize": self._quantize,
            "smoke": self._smoke,
        }
        try:
            return handlers[context.node.kind](context)
        except KeyError as exc:
            raise AdapterError(f"llamacpp adapter does not implement node kind {context.node.kind!r}") from exc

    def _base_observations(self) -> dict[str, Any]:
        fingerprint = self.fingerprint()
        return {
            "runner": self.runner_name,
            "adapter": fingerprint["adapter"],
            "revision": fingerprint["revision"],
            "tool_versions": fingerprint["versions"],
            "tool_digests": {
                key: value["resolved_file_digest"] for key, value in fingerprint["tools"].items()
            },
            "python_environment": fingerprint["python_environment"],
            "tool_source_repository": fingerprint["tool_source_repository"],
        }

    def _source(self, context: AdapterContext) -> AdapterExecution:
        source = Path(str(context.node.config["path"]))
        expected_digest = str(context.node.config["digest"])
        if tree_digest(source) != expected_digest:
            raise AdapterError("source bytes changed after planning")
        destination = context.output_dir / "source"
        if source.is_dir():
            shutil.copytree(source, destination)
        elif source.is_file():
            destination.mkdir()
            shutil.copy2(source, destination / source.name)
        else:
            raise AdapterError(f"source does not exist: {source}")
        if tree_digest(source) != expected_digest or tree_digest(destination) != expected_digest:
            raise AdapterError("source bytes changed while being copied")
        observations = self._base_observations()
        observations["operation"] = "local_source_copied"
        return AdapterExecution(
            outputs={"source": destination}, command=(), observations=observations, state=NodeState.PRODUCED
        )

    def _convert(self, context: AdapterContext) -> AdapterExecution:
        source = _one_dependency_output(context)
        output = context.output_dir / "model-F16.gguf"
        argv = (
            self.tools["python"],
            self.tools["convert"],
            str(source),
            "--outfile",
            str(output),
            "--outtype",
            "f16",
        )
        command, process = self._execute(argv, context.work_dir, context.log_path)
        if not output.is_file():
            raise AdapterError("converter exited successfully but did not produce its declared output")
        observations = self._base_observations() | process | {
            "operation": "converter_executed",
            "artifact_digest": _digest(output),
        }
        return AdapterExecution(
            outputs={"artifact": output}, command=command, observations=observations, state=NodeState.PRODUCED
        )

    def _quantize(self, context: AdapterContext) -> AdapterExecution:
        source = _one_dependency_output(context)
        quantization = str(context.node.config["quantization"])
        if quantization not in {"Q4_0", "Q8_0"}:
            raise AdapterError("llamacpp quantize node only accepts Q4_0 or Q8_0")
        output = context.output_dir / f"model-{quantization}.gguf"
        command, process = self._execute(
            (self.tools["quantize"], str(source), str(output), quantization),
            context.work_dir,
            context.log_path,
        )
        if not output.is_file():
            raise AdapterError("quantizer exited successfully but did not produce its declared output")
        observations = self._base_observations() | process | {
            "operation": "quantizer_executed",
            "quantization": quantization,
            "artifact_digest": _digest(output),
        }
        return AdapterExecution(
            outputs={"artifact": output}, command=command, observations=observations, state=NodeState.PRODUCED
        )

    def _smoke(self, context: AdapterContext) -> AdapterExecution:
        artifact = _one_dependency_output(context)
        prompt = str(context.node.config["prompt"])
        max_tokens = int(context.node.config["max_tokens"])
        command, process = self._execute(
            (
                self.tools["runner"],
                "-m",
                str(artifact),
                "-p",
                prompt,
                "-n",
                str(max_tokens),
                "--seed",
                "0",
                "--single-turn",
            ),
            context.work_dir,
            context.log_path,
            sensitive_indices=frozenset({4}),
        )
        output = context.output_dir / "execution.log"
        output.write_text(process["stdout"], encoding="utf-8")
        observations = self._base_observations() | process | {
            "operation": "artifact_executed_on_named_runner",
            "artifact_digest": _digest(artifact),
            "prompt_digest": "sha256:" + hashlib.sha256(prompt.encode()).hexdigest(),
        }
        return AdapterExecution(
            outputs={"observation": output}, command=command, observations=observations, state=NodeState.EXECUTED
        )
