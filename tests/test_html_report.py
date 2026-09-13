from __future__ import annotations

from pathlib import Path

from modelbake.cli import EXIT_OK, main
from modelbake.html_report import render_html_report


def sample_manifest() -> dict:
    return {
        "schema": "modelbake.run.v1",
        "run_id": "run-1",
        "project": "demo <script>alert(1)</script>",
        "status": "succeeded",
        "recipe_digest": "sha256:" + "1" * 64,
        "source_digest": "sha256:" + "2" * 64,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "runner": {"observed": {"os": "Darwin", "machine": "arm64", "python_version": "3.14"}},
        "nodes": [
            {
                "id": "source",
                "kind": "source",
                "state": "produced",
                "outputs": {"source": "/tmp/source"},
                "output_digests": {"source": "sha256:" + "3" * 64},
                "command": [],
                "observations": {},
            },
            {
                "id": "smoke-q4",
                "kind": "smoke",
                "state": "fixture_observed",
                "outputs": {"observation": "/tmp/observation"},
                "output_digests": {"observation": "sha256:" + "4" * 64},
                "command": [],
                "observations": {},
            },
        ],
        "exclusions": ["No quality <claim> is inferred."],
    }


def test_html_report_is_self_contained_scoped_and_escaped() -> None:
    report = render_html_report(sample_manifest())
    assert "<!doctype html>" in report
    assert "fixture_observed" in report
    assert "A green process exit is not a green model." in report
    assert "demo &lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "No quality &lt;claim&gt; is inferred." in report
    assert "<script>alert(1)</script>" not in report
    assert "https://" not in report


def test_cli_writes_html_report(tmp_path: Path, capsys) -> None:
    import json

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(sample_manifest()), encoding="utf-8")
    output = tmp_path / "nested/report.html"
    assert main(["report", str(manifest), "--html", str(output)]) == EXIT_OK
    assert output.is_file()
    assert str(output.resolve()) in capsys.readouterr().out
