"""Command-line interface for planning, building, and checking ModelBake runs."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .compare import compare_manifests, render_comparison
from .config import ConfigError, build_plan, load_config
from .html_report import write_html_report
from .manifest import ManifestError, load_manifest, render_report, verify_manifest
from .update_check import maybe_print_update_notice

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUILD_FAILED = 3
EXIT_VERIFY_FAILED = 4
EXIT_IO = 5

_BUILTIN_DEMO_FILES = {
    "source/config.json": b'{"architecture":"llama","fixture":true,"hidden_size":4}\n',
    "source/tokenizer.json": (
        b'{"model":{"type":"ModelBakeFixture","vocab":{"fixture":0,"token":1}}}\n'
    ),
    "source/weights.fixture": b"MODELBAKE-DETERMINISTIC-FIXTURE-WEIGHTS-V0\n",
    "modelbake.fixture.baseline.yaml": b"""schema: modelbake/v0
project: fixture-demo
source:
  kind: local
  path: source
  architecture: llama
  format: fixture
targets:
  - name: q4
    quantization: Q4_0
smoke:
  prompt: \"ModelBake fixture prompt\"
  max_tokens: 4
runner:
  adapter: fixture
  name: deterministic-local-fixture
  timeout_seconds: 30
  max_log_bytes: 16384
""",
    "modelbake.fixture.candidate.yaml": b"""schema: modelbake/v0
project: fixture-demo
source:
  kind: local
  path: source
  architecture: llama
  format: fixture
targets:
  - name: q4
    quantization: Q4_0
  - name: q8
    quantization: Q8_0
smoke:
  prompt: \"ModelBake fixture prompt\"
  max_tokens: 4
runner:
  adapter: fixture
  name: deterministic-local-fixture
  timeout_seconds: 30
  max_log_bytes: 16384
""",
    "modelbake.fixture.yaml": b"""schema: modelbake/v0
project: fixture-demo
source:
  kind: local
  path: source
  architecture: llama
  format: fixture
targets:
  - name: f16
    quantization: F16
  - name: q4
    quantization: Q4_0
  - name: q8
    quantization: Q8_0
smoke:
  prompt: \"ModelBake fixture prompt\"
  max_tokens: 4
runner:
  adapter: fixture
  name: deterministic-local-fixture
  timeout_seconds: 30
  max_log_bytes: 16384
""",
}


def _interrupt_on_sigterm(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _add_source_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-source-root",
        action="append",
        type=Path,
        dest="allowed_source_roots",
        help=(
            "trusted root for source.path; required for absolute mounted source paths "
            "and repeatable"
        ),
    )


def _add_baseline_location_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--store",
        type=Path,
        default=Path(".modelbake/baselines"),
        help="local tamper-evident baseline store",
    )
    parser.add_argument(
        "--name",
        default="production",
        help="baseline channel name (default: production)",
    )


def _prepare_builtin_demo_recipe(story: str | None = None) -> Path:
    """Materialize the deterministic fixture bundled in the CLI implementation."""

    root = Path(".modelbake/demo-input-v1").resolve()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ConfigError(f"built-in demo input path is unsafe: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        root.chmod(0o700)
    for relative, content in _BUILTIN_DEMO_FILES.items():
        path = root / relative
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigError(f"built-in demo file path is unsafe: {path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            path.parent.chmod(0o700)
        if path.exists():
            if path.read_bytes() != content:
                raise ConfigError(
                    f"built-in demo input differs from the expected bytes: {path}"
                )
        else:
            path.write_bytes(content)
        if os.name == "posix":
            path.chmod(0o600)
    recipe = {
        "baseline": "modelbake.fixture.baseline.yaml",
        "candidate": "modelbake.fixture.candidate.yaml",
    }.get(story, "modelbake.fixture.yaml")
    return root / recipe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modelbake", description="Build observed open-model artifacts")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="validate and print the build graph")
    plan.add_argument("recipe", type=Path)
    _add_source_root_argument(plan)
    plan.add_argument("--json", action="store_true", dest="as_json")

    build = subparsers.add_parser("build", help="execute a validated recipe")
    build.add_argument("recipe", type=Path)
    _add_source_root_argument(build)
    build.add_argument("--run-dir", type=Path)
    build.add_argument("--cache-dir", type=Path, default=Path(".modelbake/cache"))
    build.add_argument(
        "--resume",
        action="store_true",
        help="reuse cached completed nodes from an interrupted run directory",
    )
    build.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser("status", help="show a run manifest status")
    status.add_argument("manifest", type=Path)
    status.add_argument("--json", action="store_true", dest="as_json")

    verify = subparsers.add_parser("verify", help="recompute recorded artifact digests")
    verify.add_argument("manifest", type=Path)
    verify.add_argument(
        "--allow-artifact-root",
        action="append",
        type=Path,
        dest="allowed_roots",
        required=True,
        help="allowed root for recorded output paths (repeatable)",
    )
    verify.add_argument("--json", action="store_true", dest="as_json")

    demo = subparsers.add_parser("demo", help="run the credential-free fixture example")
    demo_source = demo.add_mutually_exclusive_group()
    demo_source.add_argument("--recipe", type=Path)
    demo_source.add_argument(
        "--story",
        choices=("baseline", "candidate"),
        help="built-in Q4 baseline or Q4+Q8 candidate release",
    )
    _add_source_root_argument(demo)
    demo.add_argument("--run-dir", type=Path, default=Path(".modelbake/demo"))
    demo.add_argument("--cache-dir", type=Path, default=Path(".modelbake/cache"))
    demo.add_argument("--resume", action="store_true")

    tour = subparsers.add_parser(
        "tour", help="run a baseline, candidate, and the release diff between them"
    )
    tour.add_argument(
        "--run-root",
        type=Path,
        help="directory for both fixture runs (default: a new .modelbake/tours entry)",
    )
    tour.add_argument("--cache-dir", type=Path, default=Path(".modelbake/cache"))
    tour.add_argument("--markdown", action="store_true")

    report = subparsers.add_parser("report", help="render scoped run observations")
    report.add_argument("manifest", type=Path)
    report_output = report.add_mutually_exclusive_group()
    report_output.add_argument("--json", action="store_true", dest="as_json")
    report_output.add_argument("--html", type=Path, metavar="PATH")

    compare = subparsers.add_parser(
        "compare", help="review recorded changes between two run manifests"
    )
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare_output = compare.add_mutually_exclusive_group()
    compare_output.add_argument("--json", action="store_true", dest="as_json")
    compare_output.add_argument("--markdown", action="store_true")

    baseline = subparsers.add_parser(
        "baseline", help="accept and inspect a local release baseline"
    )
    baseline_commands = baseline.add_subparsers(
        dest="baseline_command", required=True
    )
    baseline_accept = baseline_commands.add_parser(
        "accept", help="verify and append an accepted manifest"
    )
    baseline_accept.add_argument("manifest", type=Path)
    _add_baseline_location_arguments(baseline_accept)
    baseline_accept.add_argument(
        "--allow-artifact-root",
        action="append",
        type=Path,
        dest="allowed_roots",
        required=True,
        help="trusted root for recorded output paths (repeatable)",
    )
    baseline_accept.add_argument("--note", help="optional single-line decision note")
    baseline_accept.add_argument(
        "--review",
        type=Path,
        required=True,
        help="required exact review receipt for compare-and-swap acceptance",
    )
    baseline_accept.add_argument("--json", action="store_true", dest="as_json")

    baseline_review = baseline_commands.add_parser(
        "review", help="live-verify and bind a candidate to the current predecessor"
    )
    baseline_review.add_argument("candidate", type=Path)
    _add_baseline_location_arguments(baseline_review)
    baseline_review.add_argument("--review", type=Path, required=True, dest="review_path")
    baseline_review.add_argument(
        "--allow-artifact-root",
        action="append",
        type=Path,
        dest="allowed_roots",
        required=True,
        help="trusted root for recorded output paths (repeatable)",
    )
    baseline_review.add_argument(
        "--initial",
        action="store_true",
        help="review the first release of an explicitly empty channel",
    )
    baseline_review.add_argument("--json", action="store_true", dest="as_json")

    baseline_check = baseline_commands.add_parser(
        "check", help="require this exact candidate to be current and live-verified"
    )
    baseline_check.add_argument("candidate", type=Path)
    _add_baseline_location_arguments(baseline_check)
    baseline_check.add_argument(
        "--allow-artifact-root",
        action="append",
        type=Path,
        dest="allowed_roots",
        required=True,
        help="trusted root for recorded output paths (repeatable)",
    )
    baseline_check.add_argument("--json", action="store_true", dest="as_json")

    baseline_current = baseline_commands.add_parser(
        "current", help="show the current accepted baseline"
    )
    _add_baseline_location_arguments(baseline_current)
    baseline_current.add_argument("--json", action="store_true", dest="as_json")

    baseline_verify = baseline_commands.add_parser(
        "verify", help="verify the stored hash chain and exact manifest bytes"
    )
    _add_baseline_location_arguments(baseline_verify)
    baseline_verify.add_argument("--json", action="store_true", dest="as_json")

    baseline_compare = baseline_commands.add_parser(
        "compare", help="compare the accepted baseline with a candidate manifest"
    )
    baseline_compare.add_argument("candidate", type=Path)
    _add_baseline_location_arguments(baseline_compare)
    baseline_compare_output = baseline_compare.add_mutually_exclusive_group()
    baseline_compare_output.add_argument("--json", action="store_true", dest="as_json")
    baseline_compare_output.add_argument("--markdown", action="store_true")
    return parser


def _print_plan(
    recipe: Path, as_json: bool, allowed_source_roots: list[Path] | None = None
) -> int:
    plan = build_plan(load_config(recipe, allowed_source_roots=allowed_source_roots))
    if as_json:
        print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"project: {plan.project}")
        print(f"schema: {plan.schema}")
        for node in plan.nodes:
            dependencies = ", ".join(node.dependencies) if node.dependencies else "-"
            print(f"{node.id}: {node.kind} <- {dependencies}")
    return EXIT_OK


def _build(
    recipe: Path,
    run_dir: Path | None,
    cache_dir: Path,
    as_json: bool = False,
    resume: bool = False,
    allowed_source_roots: list[Path] | None = None,
) -> int:
    # Core execution is imported only for commands that need it; config/status use
    # remains available even when an optional runner environment is incomplete.
    from .adapters.fixture import FixtureAdapter
    from .adapters.llamacpp import LlamaCppAdapter
    from .engine import BuildEngine

    config = load_config(recipe, allowed_source_roots=allowed_source_roots)
    plan = build_plan(config)
    adapter = (
        FixtureAdapter()
        if config.runner.adapter == "fixture"
        else LlamaCppAdapter(config.runner.to_dict())
    )
    adapters = {kind: adapter for kind in ("source", "convert", "quantize", "smoke")}
    result = BuildEngine(cache_dir, adapters).run(plan, run_dir=run_dir, resume=resume)
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_report(result.to_dict()))
        print(f"manifest: {result.run_dir / 'manifest.json'}")
    return EXIT_OK if result.status == "succeeded" else EXIT_BUILD_FAILED


def _status(manifest: Path, as_json: bool) -> int:
    data = load_manifest(manifest)
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"{data.get('status', 'unknown')} {data.get('run_id', '<unknown>')}")
        for node in data["nodes"]:
            if isinstance(node, dict):
                print(f"{node.get('id', '<unknown>')}: {node.get('state', 'unknown')}")
    return EXIT_OK if data.get("status") == "succeeded" else EXIT_BUILD_FAILED


def _verify(manifest: Path, as_json: bool, allowed_roots: list[Path]) -> int:
    result = verify_manifest(
        manifest,
        allowed_roots=tuple(allowed_roots),
    )
    payload = {"ok": result.ok, "checked": result.checked, "failures": list(result.failures)}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif result.ok:
        print(f"digest verified: {result.checked} artifact(s)")
    else:
        print("digest verification failed", file=sys.stderr)
        for failure in result.failures:
            print(f"- {failure}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_VERIFY_FAILED


def _baseline_command(args: argparse.Namespace) -> int:
    try:
        from .baseline import (
            BaselineError,
            accept_baseline,
            check_baseline,
            load_current_baseline,
            review_baseline,
            verify_ledger,
        )
    except ImportError:
        print(
            "baseline error: accepted-baseline ledgers require a POSIX host",
            file=sys.stderr,
        )
        return EXIT_IO

    try:
        if args.baseline_command == "accept":
            accepted = accept_baseline(
                args.manifest,
                args.store,
                args.name,
                tuple(args.allowed_roots),
                args.note,
                args.review,
            )
            payload = {
                "created": accepted.created,
                "decision_digest": accepted.decision_digest,
                "manifest_digest": accepted.manifest_digest,
                "manifest_path": str(accepted.manifest_path),
                "name": accepted.name,
                "run_id": accepted.decision["run_id"],
                "verified_artifacts": accepted.verified_artifacts,
            }
            if args.as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                verb = "accepted" if accepted.created else "already current"
                print(
                    f"baseline {accepted.name!r} {verb}: "
                    f"{accepted.decision['run_id']} "
                    f"({accepted.verified_artifacts} artifact(s) digest verified)"
                )
            return EXIT_OK
        if args.baseline_command == "review":
            reviewed = review_baseline(
                args.candidate,
                args.store,
                args.name,
                tuple(args.allowed_roots),
                args.review_path,
                initial=args.initial,
            )
            payload = {
                "candidate_manifest_digest": reviewed.candidate_manifest_digest,
                "comparison": reviewed.receipt["comparison"],
                "comparison_digest": reviewed.comparison_digest,
                "initial": reviewed.receipt["initial"],
                "name": reviewed.name,
                "predecessor_decision_digest": reviewed.predecessor_decision_digest,
                "project": reviewed.project,
                "receipt_digest": reviewed.receipt_digest,
                "review": str(reviewed.path),
                "verified_artifacts": reviewed.verified_artifacts,
            }
            if args.as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"review receipt written: {reviewed.path}\n"
                    f"candidate: {reviewed.candidate_manifest_digest}\n"
                    f"predecessor: {reviewed.predecessor_decision_digest or 'empty channel'}\n"
                    f"artifact digests verified: {reviewed.verified_artifacts}"
                )
            return EXIT_OK
        if args.baseline_command == "check":
            checked = check_baseline(
                args.candidate,
                args.store,
                args.name,
                tuple(args.allowed_roots),
            )
            payload = {
                "current": True,
                "decision_digest": checked.decision_digest,
                "manifest_digest": checked.manifest_digest,
                "name": checked.name,
                "run_id": checked.run_id,
                "verified_artifacts": checked.verified_artifacts,
            }
            if args.as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"exact current baseline verified: {checked.name!r}, "
                    f"{checked.run_id} ({checked.verified_artifacts} artifact(s))"
                )
            return EXIT_OK
        if args.baseline_command == "current":
            current = load_current_baseline(args.store, args.name)
            payload = {
                "decision": dict(current.decision),
                "decision_digest": current.decision_digest,
                "manifest_digest": current.manifest_digest,
                "manifest_path": str(current.manifest_path),
                "name": current.name,
                "run_id": current.manifest["run_id"],
            }
            if args.as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"baseline {current.name!r}: {current.manifest['run_id']}\n"
                    f"manifest: {current.manifest_path}\n"
                    f"decision: {current.decision_digest}"
                )
            return EXIT_OK
        if args.baseline_command == "verify":
            verified = verify_ledger(args.store, args.name)
            payload = {
                "current_decision_digest": verified.current_decision_digest,
                "current_manifest_digest": verified.current_manifest_digest,
                "decisions": verified.decisions,
                "name": verified.name,
            }
            if args.as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"baseline ledger verified: {verified.name!r}, "
                    f"{verified.decisions} decision(s); live artifacts were not re-read"
                )
            return EXIT_OK
        if args.baseline_command == "compare":
            current = load_current_baseline(args.store, args.name)
            comparison = compare_manifests(current.manifest_path, args.candidate)
            if args.as_json:
                print(json.dumps(comparison, indent=2, sort_keys=True))
            else:
                print(render_comparison(comparison, markdown=args.markdown))
            return EXIT_OK
    except BaselineError as exc:
        print(f"baseline error: {exc}", file=sys.stderr)
        return EXIT_IO
    raise AssertionError(f"unknown baseline command: {args.baseline_command}")


def _tour(run_root: Path | None, cache_dir: Path, markdown: bool) -> int:
    """Show ModelBake's recurring value with one credential-free command."""

    root = run_root or Path(".modelbake/tours") / uuid.uuid4().hex[:12]
    baseline_dir = root / "accepted"
    candidate_dir = root / "candidate"
    print("ModelBake tour: same source, one added release target\n")
    first = _build(
        _prepare_builtin_demo_recipe("baseline"),
        baseline_dir,
        cache_dir,
    )
    if first != EXIT_OK:
        return first
    second = _build(
        _prepare_builtin_demo_recipe("candidate"),
        candidate_dir,
        cache_dir,
    )
    if second != EXIT_OK:
        return second
    comparison = compare_manifests(
        baseline_dir / "manifest.json", candidate_dir / "manifest.json"
    )
    print("\nRELEASE DIFF\n------------")
    print(render_comparison(comparison, markdown=markdown))
    print(f"\nTour files: {root.resolve()}")
    print("Fixture only: no GGUF or model-runtime compatibility is claimed.")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    previous_sigterm = None
    if os.name == "posix":
        try:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _interrupt_on_sigterm)
        except ValueError:
            previous_sigterm = None
    try:
        args = parser.parse_args(argv)
        if not getattr(args, "as_json", False):
            maybe_print_update_notice(__version__)
        if args.command == "plan":
            return _print_plan(args.recipe, args.as_json, args.allowed_source_roots)
        if args.command == "build":
            return _build(
                args.recipe,
                args.run_dir,
                args.cache_dir,
                args.as_json,
                args.resume,
                args.allowed_source_roots,
            )
        if args.command == "demo":
            recipe = args.recipe or _prepare_builtin_demo_recipe(args.story)
            return _build(
                recipe,
                args.run_dir,
                args.cache_dir,
                resume=args.resume,
                allowed_source_roots=args.allowed_source_roots,
            )
        if args.command == "tour":
            return _tour(args.run_root, args.cache_dir, args.markdown)
        if args.command == "status":
            return _status(args.manifest, args.as_json)
        if args.command == "verify":
            return _verify(args.manifest, args.as_json, args.allowed_roots)
        if args.command == "report":
            data = load_manifest(args.manifest)
            if args.html:
                print(f"report: {write_html_report(data, args.html)}")
            else:
                print(json.dumps(data, indent=2, sort_keys=True) if args.as_json else render_report(data))
            return EXIT_OK
        if args.command == "compare":
            comparison = compare_manifests(args.before, args.after)
            if args.as_json:
                print(json.dumps(comparison, indent=2, sort_keys=True))
            else:
                print(render_comparison(comparison, markdown=args.markdown))
            return EXIT_OK
        if args.command == "baseline":
            return _baseline_command(args)
        parser.error("unknown command")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return EXIT_IO
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"I/O error: {exc}", file=sys.stderr)
        return EXIT_IO
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
