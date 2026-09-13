from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from modelbake.adapters.base import AdapterError
from modelbake.adapters.llamacpp import LlamaCppAdapter


def _write_executable(path: Path, source: str = "#!/bin/sh\necho test-v1\n") -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _adapter(tmp_path: Path, python: Path, *, timeout: int = 5) -> LlamaCppAdapter:
    return LlamaCppAdapter(
        {
            "name": "fingerprint-test",
            "timeout_seconds": timeout,
            "max_log_bytes": 256,
            "tools": {
                "python": str(python),
                "convert": str(_write_executable(tmp_path / "convert.py")),
                "quantize": str(_write_executable(tmp_path / "llama-quantize")),
                "runner": str(_write_executable(tmp_path / "llama-cli")),
                "revision": "test-revision",
            },
        }
    )


@pytest.mark.skipif(os.name != "posix", reason="llamacpp adapter is POSIX-only")
def test_python_fingerprint_is_bounded_to_converter_dependency_versions(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python = environment / "bin" / "python"
    purelib = Path(
        subprocess.check_output(
            [str(python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip()
    )
    for index in range(1_000):
        (purelib / f"unrelated-{index}.data").write_bytes(b"not a converter dependency")
    os.mkfifo(purelib / "unrelated.pipe")
    torch_metadata = purelib / "torch-1.2.3.dist-info"
    torch_metadata.mkdir()
    metadata = torch_metadata / "METADATA"
    metadata.write_text("Metadata-Version: 2.1\nName: torch\nVersion: 1.2.3\n", encoding="utf-8")

    adapter = _adapter(tmp_path, python)
    first = adapter.fingerprint()["python_environment"]

    assert first["schema"] == "modelbake.llamacpp-python-environment.v2"
    assert first["dependency_versions"] == {
        "gguf": None,
        "numpy": None,
        "protobuf": None,
        "sentencepiece": None,
        "torch": "1.2.3",
        "transformers": None,
    }
    assert "files_hashed" not in first
    assert "packages_digest" not in first

    (purelib / "unrelated-0.data").write_bytes(b"changed but still unrelated")
    unchanged = adapter.fingerprint()["python_environment"]
    assert unchanged == first

    metadata.write_text("Metadata-Version: 2.1\nName: torch\nVersion: 1.2.4\n", encoding="utf-8")
    second = adapter.fingerprint()["python_environment"]
    assert second["dependency_versions"]["torch"] == "1.2.4"
    assert second["dependency_versions_digest"] != first["dependency_versions_digest"]


@pytest.mark.skipif(os.name != "posix", reason="llamacpp adapter is POSIX-only")
def test_python_fingerprint_times_out_a_slow_configured_interpreter(tmp_path: Path) -> None:
    slow_python = _write_executable(tmp_path / "slow-python", "#!/bin/sh\nsleep 30\n")
    adapter = _adapter(tmp_path, slow_python, timeout=1)

    with pytest.raises(AdapterError, match="probe timed out after 1 seconds"):
        adapter.fingerprint()


def test_python_fingerprint_rejects_malformed_success_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path, Path(sys.executable))

    def malformed_probe(*args: object, **kwargs: object) -> tuple[int, bytes, bytes, bool, bool]:
        return 0, json.dumps({"schema": "wrong"}).encode(), b"", False, False

    monkeypatch.setattr(adapter, "_run_capped", malformed_probe)

    with pytest.raises(AdapterError, match="probe returned an invalid payload"):
        adapter.fingerprint()


def test_python_fingerprint_surfaces_probe_exit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path, Path(sys.executable))

    def failed_probe(*args: object, **kwargs: object) -> tuple[int, bytes, bytes, bool, bool]:
        return 7, b"", b"metadata lookup failed", False, False

    monkeypatch.setattr(adapter, "_run_capped", failed_probe)

    with pytest.raises(
        AdapterError, match="probe exited with status 7: metadata lookup failed"
    ):
        adapter.fingerprint()
