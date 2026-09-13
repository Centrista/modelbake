from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from modelbake.config import ConfigError, build_plan, load_config


def recipe_data() -> dict:
    return {
        "schema": "modelbake/v0",
        "project": "test-model",
        "source": {
            "kind": "local",
            "path": "source",
            "architecture": "llama",
            "format": "fixture",
        },
        "targets": [
            {"name": "f16", "quantization": "F16"},
            {"name": "q4", "quantization": "Q4_0"},
            {"name": "q8", "quantization": "Q8_0"},
        ],
        "smoke": {"prompt": "hello fixture", "max_tokens": 4},
        "runner": {
            "adapter": "fixture",
            "name": "test-fixture",
            "timeout_seconds": 10,
            "max_log_bytes": 4096,
        },
    }


def write_recipe(tmp_path: Path, data: dict | None = None) -> Path:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "weights.fixture").write_bytes(b"deterministic-test-weights\n")
    path = tmp_path / "modelbake.yaml"
    path.write_text(yaml.safe_dump(data or recipe_data(), sort_keys=False), encoding="utf-8")
    return path


def test_strict_v0_recipe_compiles_expected_graph(tmp_path: Path) -> None:
    config = load_config(write_recipe(tmp_path))
    plan = build_plan(config)

    assert config.schema == "modelbake/v0"
    assert plan.source["digest"].startswith("sha256:")
    assert [(node.id, node.kind, node.dependencies) for node in plan.nodes] == [
        ("source", "source", ()),
        ("convert-f16", "convert", ("source",)),
        ("quantize-q4", "quantize", ("convert-f16",)),
        ("quantize-q8", "quantize", ("convert-f16",)),
        ("smoke-f16", "smoke", ("convert-f16",)),
        ("smoke-q4", "smoke", ("quantize-q4",)),
        ("smoke-q8", "smoke", ("quantize-q8",)),
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema="modelbake/v1"), "schema must be exactly"),
        (
            lambda value: value["source"].update(architecture="mistral"),
            "unsupported architecture",
        ),
        (
            lambda value: value["targets"][0].update(quantization="Q2_K"),
            "unsupported quantization",
        ),
        (lambda value: value["runner"].update(command="rm -rf /"), "unknown field"),
        (lambda value: value["source"].update(trust_remote_code=True), "trust_remote_code"),
        (lambda value: value["source"].update(kind="huggingface"), "source.kind"),
    ],
)
def test_rejects_unsupported_or_executable_configuration(
    tmp_path: Path, mutation, message: str
) -> None:
    data = recipe_data()
    mutation(data)
    with pytest.raises(ConfigError, match=message):
        load_config(write_recipe(tmp_path, data))


def test_rejects_parent_path_traversal(tmp_path: Path) -> None:
    data = recipe_data()
    data["source"]["path"] = "../outside"
    with pytest.raises(ConfigError, match="cannot contain"):
        load_config(write_recipe(tmp_path, data))


def test_absolute_source_requires_and_obeys_explicit_trusted_root(tmp_path: Path) -> None:
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    mounted = tmp_path / "mounted-models"
    source = mounted / "fixture"
    source.mkdir(parents=True)
    (source / "weights.fixture").write_bytes(b"mounted fixture")
    data = recipe_data()
    data["source"]["path"] = str(source)
    recipe = write_recipe(recipe_dir, data)

    with pytest.raises(ConfigError, match="explicit trusted --allow-source-root"):
        load_config(recipe)
    config = load_config(recipe, allowed_source_roots=(mounted,))
    assert config.source.path == source.resolve()
    with pytest.raises(ConfigError, match="outside the allowed"):
        load_config(recipe, allowed_source_roots=(tmp_path / "other",))


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "weights.fixture").write_bytes(b"outside")
    link = tmp_path / "linked-source"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    data = recipe_data()
    data["source"]["path"] = "linked-source"
    with pytest.raises(ConfigError, match="outside the allowed"):
        load_config(write_recipe(tmp_path, data))


def test_rejects_pickle_backed_source(tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    (tmp_path / "source/model.bin").write_bytes(b"pickle-like")
    with pytest.raises(ConfigError, match="pickle-backed"):
        load_config(recipe)


def test_rejects_nested_source_symlink(tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    outside = tmp_path / "outside.fixture"
    outside.write_bytes(b"outside")
    link = tmp_path / "source/linked.fixture"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ConfigError, match="cannot contain symlinks"):
        load_config(recipe)


def test_rejects_duplicate_yaml_fields(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "weights.fixture").write_bytes(b"fixture")
    recipe = tmp_path / "modelbake.yaml"
    recipe.write_text(
        "schema: modelbake/v0\n"
        "schema: modelbake/v0\n"
        "project: duplicate\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate YAML field"):
        load_config(recipe)


def test_source_bytes_participate_in_plan_digest(tmp_path: Path) -> None:
    recipe = write_recipe(tmp_path)
    first = build_plan(load_config(recipe)).source["digest"]
    (tmp_path / "source/weights.fixture").write_bytes(b"changed")
    second = build_plan(load_config(recipe)).source["digest"]
    assert first != second


def test_llamacpp_requires_only_explicit_existing_absolute_tools(tmp_path: Path) -> None:
    data = recipe_data()
    data["source"]["format"] = "safetensors"
    data["runner"]["adapter"] = "llamacpp"
    tools = {}
    for name in ("python", "convert", "quantize", "runner"):
        tool = tmp_path / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
        tools[name] = str(tool)
    tools["revision"] = "llama.cpp-test-revision"
    data["runner"]["tools"] = tools
    recipe = write_recipe(tmp_path, data)
    (tmp_path / "source/model.safetensors").write_bytes(b"safe-test-source")
    config = load_config(recipe)
    assert config.runner.tools is not None
    assert config.runner.tools.revision == "llama.cpp-test-revision"

    data["runner"]["tools"]["runner"] = "llama-cli --unsafe-arg"
    unsafe_recipe = write_recipe(tmp_path, data)
    with pytest.raises(ConfigError, match="explicit absolute path"):
        load_config(unsafe_recipe)
