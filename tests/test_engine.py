from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from modelbake.adapters.base import Adapter, AdapterContext, AdapterExecution
from modelbake.cache import CacheCorruptionError, LocalCache
from modelbake.engine import BuildEngine
from modelbake.graph import CycleError, GraphError, topological_sort
from modelbake.hashing import tree_digest
from modelbake.types import BuildPlan, NodeSpec, NodeState


class RecordingAdapter(Adapter):
    def __init__(self, *, interrupt_node: str | None = None) -> None:
        self.calls: list[str] = []
        self.interrupt_node = interrupt_node
        self.did_interrupt = False

    def fingerprint(self) -> Mapping[str, Any]:
        return {"adapter": "recording", "version": 1}

    def run(self, context: AdapterContext) -> AdapterExecution:
        self.calls.append(context.node.id)
        if context.node.id == self.interrupt_node and not self.did_interrupt:
            self.did_interrupt = True
            raise KeyboardInterrupt
        dependency_text = []
        for dependency_id, dependency in sorted(context.dependencies.items()):
            for name, path in sorted(dependency.outputs.items()):
                dependency_text.append(f"{dependency_id}:{name}={path.read_text(encoding='utf-8')}")
        output = context.output_dir / "artifact.txt"
        source_bytes = None
        configured_path = context.node.config.get("path")
        if isinstance(configured_path, str):
            source_bytes = Path(configured_path).read_text(encoding="utf-8")
        output.write_text(
            json.dumps(
                {
                    "node": context.node.id,
                    "config": context.node.config,
                    "dependencies": dependency_text,
                    "source_bytes": source_bytes,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return AdapterExecution(
            outputs={"artifact": output},
            command=("recording-adapter", context.node.id),
            observations={"wrote": output.name},
        )


def make_plan(nodes: tuple[NodeSpec, ...]) -> BuildPlan:
    return BuildPlan(
        schema="modelbake.plan.v1",
        project="engine-test",
        recipe_path=Path("modelbake.yaml"),
        recipe_digest="sha256:" + "1" * 64,
        runner={"requested": "test"},
        nodes=nodes,
        source={"kind": "fixture", "digest": "sha256:" + "2" * 64},
        toolchain={"recording": {"version": 1}},
    )


def branch_plan(q4_value: int = 4) -> BuildPlan:
    return make_plan(
        (
            NodeSpec("source", "fake", config={"value": "source"}),
            NodeSpec("convert", "fake", ("source",), {"format": "f16"}),
            NodeSpec("q4", "fake", ("convert",), {"bits": q4_value}),
            NodeSpec("q8", "fake", ("convert",), {"bits": 8}),
        )
    )


def test_cold_run_then_verified_warm_cache(tmp_path) -> None:
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})

    cold = engine.run(branch_plan(), run_dir=tmp_path / "cold", run_id="cold")
    assert cold.status == "succeeded"
    assert [result.state for result in cold.nodes] == [NodeState.PRODUCED] * 4
    assert adapter.calls == ["source", "convert", "q4", "q8"]
    assert json.loads((tmp_path / "cold/manifest.json").read_text())["status"] == "succeeded"

    warm = engine.run(branch_plan(), run_dir=tmp_path / "warm", run_id="warm")
    assert warm.status == "succeeded"
    assert [result.state for result in warm.nodes] == [NodeState.CACHE_HIT] * 4
    assert adapter.calls == ["source", "convert", "q4", "q8"]
    for result in warm.nodes:
        assert result.output_digests["artifact"].startswith("sha256:")


def test_config_change_invalidates_only_changed_node_and_descendants(tmp_path) -> None:
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})
    engine.run(branch_plan(4), run_dir=tmp_path / "first")

    changed = engine.run(branch_plan(3), run_dir=tmp_path / "changed")
    states = {result.id: result.state for result in changed.nodes}
    assert states == {
        "source": NodeState.CACHE_HIT,
        "convert": NodeState.CACHE_HIT,
        "q4": NodeState.PRODUCED,
        "q8": NodeState.CACHE_HIT,
    }
    assert adapter.calls == ["source", "convert", "q4", "q8", "q4"]


def test_dependency_change_invalidates_descendant_but_not_sibling(tmp_path) -> None:
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})
    original = branch_plan()
    engine.run(original, run_dir=tmp_path / "first")
    changed_nodes = tuple(
        NodeSpec(node.id, node.kind, node.dependencies, {"format": "bf16"})
        if node.id == "convert"
        else node
        for node in original.nodes
    )
    changed = engine.run(make_plan(changed_nodes), run_dir=tmp_path / "changed")

    states = {result.id: result.state for result in changed.nodes}
    assert states["source"] == NodeState.CACHE_HIT
    assert states["convert"] == NodeState.PRODUCED
    assert states["q4"] == NodeState.PRODUCED
    assert states["q8"] == NodeState.PRODUCED


def test_source_byte_change_invalidates_root_and_descendants(tmp_path) -> None:
    source_path = tmp_path / "source.bin"
    source_path.write_text("version one", encoding="utf-8")
    nodes = (
        NodeSpec("source", "fake", config={"path": str(source_path)}),
        NodeSpec("convert", "fake", ("source",)),
    )
    plan = BuildPlan(
        schema="modelbake.plan.v1",
        project="source-bytes-test",
        recipe_path=Path("modelbake.yaml"),
        recipe_digest="sha256:" + "1" * 64,
        runner={"requested": "test"},
        nodes=nodes,
        source={"kind": "local", "path": str(source_path)},
        toolchain={"recording": {"version": 1}},
    )
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})

    first = engine.run(plan, run_dir=tmp_path / "first")
    source_path.write_text("version two", encoding="utf-8")
    second = engine.run(plan, run_dir=tmp_path / "second")

    assert first.source_digest != second.source_digest
    assert [node.state for node in second.nodes] == [NodeState.PRODUCED, NodeState.PRODUCED]
    assert adapter.calls == ["source", "convert", "source", "convert"]


def test_cycle_is_rejected_before_any_adapter_runs(tmp_path) -> None:
    nodes = (
        NodeSpec("a", "fake", ("b",)),
        NodeSpec("b", "fake", ("a",)),
    )
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})

    with pytest.raises(CycleError, match="a, b"):
        engine.run(make_plan(nodes), run_dir=tmp_path / "run")
    assert adapter.calls == []
    with pytest.raises(CycleError):
        topological_sort(nodes)


def test_unsafe_node_id_is_rejected_before_work_directory_creation(tmp_path) -> None:
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})
    with pytest.raises(GraphError, match="path-safe"):
        engine.run(make_plan((NodeSpec("../escape", "fake"),)), run_dir=tmp_path / "run")
    assert adapter.calls == []


def test_failure_blocks_descendants_but_independent_branch_runs(tmp_path) -> None:
    class FailingAdapter(RecordingAdapter):
        def run(self, context: AdapterContext) -> AdapterExecution:
            if context.node.id == "convert":
                self.calls.append(context.node.id)
                raise RuntimeError("observed conversion failure")
            return super().run(context)

    adapter = FailingAdapter()
    result = BuildEngine(
        tmp_path / "cache", {"fake": adapter}, runner={"host": "test"}
    ).run(branch_plan(), run_dir=tmp_path / "run")

    states = {node.id: node.state for node in result.nodes}
    assert result.status == "failed"
    assert states == {
        "source": NodeState.PRODUCED,
        "convert": NodeState.FAILED,
        "q4": NodeState.BLOCKED,
        "q8": NodeState.BLOCKED,
    }


def test_interruption_writes_manifest_and_resume_reuses_completed_nodes(tmp_path) -> None:
    nodes = (
        NodeSpec("source", "fake"),
        NodeSpec("convert", "fake", ("source",)),
    )
    adapter = RecordingAdapter(interrupt_node="convert")
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})
    run_dir = tmp_path / "run"

    with pytest.raises(KeyboardInterrupt):
        engine.run(make_plan(nodes), run_dir=run_dir, run_id="interrupted")
    partial = json.loads((run_dir / "manifest.json").read_text())
    assert partial["status"] == "interrupted"
    assert [node["id"] for node in partial["nodes"]] == ["source"]

    resumed = engine.run(make_plan(nodes), run_dir=run_dir, run_id="resumed", resume=True)
    assert resumed.status == "succeeded"
    assert [node.state for node in resumed.nodes] == [NodeState.CACHE_HIT, NodeState.PRODUCED]
    assert adapter.calls == ["source", "convert", "convert"]


def test_tampered_cache_is_detected_and_not_silently_rebuilt(tmp_path) -> None:
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})
    first = engine.run(branch_plan(), run_dir=tmp_path / "first")
    source_result = next(result for result in first.nodes if result.id == "source")
    Path(source_result.outputs["artifact"]).write_text("tampered", encoding="utf-8")

    with pytest.raises(CacheCorruptionError):
        LocalCache(tmp_path / "cache").load(source_result.cache_key)

    second = engine.run(branch_plan(), run_dir=tmp_path / "second")
    states = {node.id: node.state for node in second.nodes}
    assert second.status == "failed"
    assert states["source"] == NodeState.FAILED
    assert states["convert"] == NodeState.BLOCKED
    assert states["q4"] == NodeState.BLOCKED
    assert states["q8"] == NodeState.BLOCKED
    assert adapter.calls == ["source", "convert", "q4", "q8"]


def test_source_changed_after_plan_is_rejected_before_cache_lookup(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    weights = source / "weights.fixture"
    weights.write_bytes(b"planned bytes")
    digest = tree_digest(source)
    plan = BuildPlan(
        schema="modelbake.plan.v1",
        project="source-race",
        recipe_path=tmp_path / "modelbake.yaml",
        recipe_digest="sha256:" + "1" * 64,
        runner={"requested": "test"},
        nodes=(NodeSpec("source", "fake", config={"path": str(source), "digest": digest}),),
        source={"kind": "local", "path": str(source), "digest": digest},
        toolchain={"recording": {"version": 1}},
    )
    adapter = RecordingAdapter()
    engine = BuildEngine(tmp_path / "cache", {"fake": adapter}, runner={"host": "test"})
    weights.write_bytes(b"mutated bytes")

    with pytest.raises(ValueError, match="changed after planning"):
        engine.run(plan, run_dir=tmp_path / "run")
    assert adapter.calls == []
    assert not any((tmp_path / "cache/entries").iterdir())


def test_adapter_fingerprint_drift_prevents_cache_commit(tmp_path) -> None:
    class DriftingAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.epoch = 0

        def fingerprint(self) -> Mapping[str, Any]:
            self.epoch += 1
            return {"adapter": "drifting", "epoch": self.epoch}

    adapter = DriftingAdapter()
    result = BuildEngine(
        tmp_path / "cache", {"fake": adapter}, runner={"host": "test"}
    ).run(make_plan((NodeSpec("source", "fake"),)), run_dir=tmp_path / "run")

    assert result.status == "failed"
    assert result.nodes[0].state == NodeState.FAILED
    assert "fingerprint changed" in str(result.nodes[0].error)
    assert not any((tmp_path / "cache/entries").iterdir())


def test_concurrent_cache_commit_uses_one_authoritative_metadata_record(tmp_path) -> None:
    cache = LocalCache(tmp_path / "cache")
    key = "sha256:" + "a" * 64
    barrier = Barrier(2)

    def commit(label: str):
        output_dir = tmp_path / label
        output_dir.mkdir()
        output = output_dir / "artifact.txt"
        output.write_text(label, encoding="utf-8")
        barrier.wait()
        return cache.commit(
            key,
            {"artifact": output},
            {"state": "produced", "command": [label], "observations": {"writer": label}},
            allowed_root=output_dir,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(commit, ("first", "second"))

    assert sorted((first.created, second.created)) == [False, True]
    assert first.entry.output_digests == second.entry.output_digests
    assert first.entry.metadata == second.entry.metadata


def test_run_directory_reuse_requires_explicit_interrupted_resume(tmp_path) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    run_dir = tmp_path / "run"
    engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)
    lock_info = (run_dir / ".modelbake-run.lock").lstat()
    assert stat.S_ISREG(lock_info.st_mode)
    if os.name == "posix":
        assert stat.S_IMODE(lock_info.st_mode) == 0o600

    with pytest.raises(FileExistsError, match="run directory is not empty"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_preexisting_nonempty_run_directory_is_rejected_without_chmod(tmp_path) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    run_dir = tmp_path / "shared-run"
    run_dir.mkdir(mode=0o755)
    (run_dir / "unrelated.txt").write_text("caller data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="run directory is not empty"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)

    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o755
    assert (run_dir / "unrelated.txt").read_text(encoding="utf-8") == "caller data"
    assert not (run_dir / ".modelbake-run.lock").exists()
    assert not (run_dir / ".modelbake-run.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
@pytest.mark.parametrize("mode", [0o755, 0o700])
def test_preexisting_empty_run_directory_is_unowned_and_unchanged(
    tmp_path: Path, mode: int
) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    run_dir = tmp_path / f"shared-empty-{mode:o}"
    run_dir.mkdir(mode=mode)

    with pytest.raises(ValueError, match="pre-existing unowned run directory"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)

    assert stat.S_IMODE(run_dir.stat().st_mode) == mode
    assert list(run_dir.iterdir()) == []


@pytest.mark.parametrize(
    "broad_path",
    [Path(Path.cwd().anchor), Path.home(), Path.cwd(), Path.cwd().parent],
)
def test_broad_run_directories_are_rejected_before_inspection_or_locking(
    tmp_path: Path, broad_path: Path
) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )

    with pytest.raises(ValueError, match="unsafe broad path"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=broad_path)


def test_run_directory_and_parent_symlinks_are_rejected_without_touching_targets(
    tmp_path: Path,
) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    target = tmp_path / "shared-target"
    target.mkdir()
    target.chmod(0o755)
    direct_link = tmp_path / "linked-run"
    parent_link = tmp_path / "linked-parent"
    try:
        direct_link.symlink_to(target, target_is_directory=True)
        parent_link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks unavailable: {exc}")

    for run_dir in (direct_link, parent_link / "child"):
        with pytest.raises(ValueError, match="not a real directory"):
            engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == []


def test_fresh_run_accepts_platform_temporary_directory_aliases(tmp_path: Path) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    with tempfile.TemporaryDirectory(prefix="modelbake-run-parent-") as temporary:
        run_dir = Path(temporary) / "run"
        result = engine.run(
            make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir
        )

        assert result.status == "succeeded"
        assert result.run_dir == run_dir.resolve()
        assert (run_dir / "manifest.json").is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX special-file semantics required")
def test_special_run_path_is_rejected_without_replacement(tmp_path: Path) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    fifo = tmp_path / "run-fifo"
    os.mkfifo(fifo, 0o644)

    with pytest.raises(ValueError, match="not a real directory"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=fifo)

    assert stat.S_ISFIFO(fifo.lstat().st_mode)


def test_run_lock_rejects_symlink_without_touching_target(tmp_path) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    victim.chmod(0o644)
    lock_path = run_dir / ".modelbake-run.lock"
    try:
        lock_path.symlink_to(victim)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(FileExistsError, match="not a regular file"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)

    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link semantics required")
def test_run_lock_rejects_hard_link_without_touching_target(tmp_path) -> None:
    engine = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    victim.chmod(0o644)
    os.link(victim, run_dir / ".modelbake-run.lock")

    with pytest.raises(FileExistsError, match="multiple hard links"):
        engine.run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)

    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_cache_rejects_noncausal_metadata_before_engine_consumes_it(tmp_path) -> None:
    adapter = RecordingAdapter()
    cache_dir = tmp_path / "cache"
    first = BuildEngine(cache_dir, {"fake": adapter}, runner={"host": "test"}).run(
        make_plan((NodeSpec("source", "fake"),)), run_dir=tmp_path / "run"
    )
    cache_manifest = LocalCache(cache_dir).entry_path(first.nodes[0].cache_key) / "manifest.json"
    payload = json.loads(cache_manifest.read_text(encoding="utf-8"))
    payload["metadata"]["state"] = "failed"
    cache_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="metadata state"):
        LocalCache(cache_dir).load(first.nodes[0].cache_key)


def test_dependency_is_snapshotted_before_adapter_execution(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"

    class CacheMutatingAdapter(RecordingAdapter):
        dependency_path: Path | None = None

        def run(self, context: AdapterContext) -> AdapterExecution:
            if context.node.id != "convert":
                return super().run(context)
            dependency = context.dependencies["source"]
            self.dependency_path = Path(dependency.outputs["artifact"])
            cached_source = next(cache_root.glob("entries/*/outputs/artifact"))
            original = cached_source.read_bytes()
            cached_source.write_bytes(b"poisoned cache bytes")
            try:
                result = super().run(context)
            finally:
                cached_source.write_bytes(original)
            return result

    adapter = CacheMutatingAdapter()
    result = BuildEngine(cache_root, {"fake": adapter}, runner={"host": "test"}).run(
        make_plan(
            (
                NodeSpec("source", "fake", config={"value": "source"}),
                NodeSpec("convert", "fake", ("source",)),
            )
        ),
        run_dir=tmp_path / "run",
    )

    assert result.status == "succeeded"
    assert adapter.dependency_path is not None
    assert "inputs" in adapter.dependency_path.parts
    child_output = Path(result.nodes[1].outputs["artifact"]).read_text(encoding="utf-8")
    assert "poisoned cache bytes" not in child_output


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_run_work_and_generated_files_are_owner_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result = BuildEngine(
        tmp_path / "cache", {"fake": RecordingAdapter()}, runner={"host": "test"}
    ).run(make_plan((NodeSpec("source", "fake"),)), run_dir=run_dir)

    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / "work").stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / ".modelbake-run.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(result.nodes[0].outputs["artifact"]).stat().st_mode) == 0o600
