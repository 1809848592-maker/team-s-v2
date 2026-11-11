"""纯本地指令包解析与边界；不读取真实环境配置或调用模型。"""

import json
import os
from dataclasses import FrozenInstanceError, replace
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.review.reviewers.instructions import (
    MAX_INSTRUCTION_BYTES,
    InstructionBundle,
    ReviewerInstructionError,
    load_framework_instruction,
    load_instruction,
)
from app.review.reviewers.schemas import ReviewOutput

VALID = b"""reviewer = "framework"
version = "2026-11-11.1"
instructions = "Use only the quoted data."
rubric = "Return findings and coverage JSON."
[[positive_examples]]
input = "One structural example."
output = { findings = [], coverage = { checked = ["Given data only"], uncovered = [] } }
[[negative_examples]]
input = "An intentionally invalid output shape."
output = { findings = [], coverage = { checked = [], uncovered = [] }, status = "passed" }
"""
PRIVATE = "PRIVATE_INSTRUCTION_CONTENT_OR_PATH"


def source(tmp_path, data=VALID):
    path = tmp_path / (PRIVATE + ".toml")
    path.write_bytes(data)
    return path


def assert_safe(operation):
    with pytest.raises(ReviewerInstructionError) as caught:
        operation()
    assert str(caught.value) == "审核员指令包不可用，请检查平台配置。"
    assert PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__


def test_builtin_resource_has_fixed_identity_and_valid_positive_invalid_negative_examples():
    bundle = load_framework_instruction()
    assert bundle.reviewer == "framework" and bundle.version == "2026-11-11.1"
    for example in bundle.positive_examples:
        ReviewOutput.model_validate_json(json.dumps(example["output"], ensure_ascii=False))
    for example in bundle.negative_examples:
        with pytest.raises(ValidationError):
            ReviewOutput.model_validate_json(json.dumps(example["output"], ensure_ascii=False))
    assert len(bundle.system_text().encode()) < MAX_INSTRUCTION_BYTES


def test_regular_file_load_is_read_only_and_system_text_has_stable_order(tmp_path):
    path = source(tmp_path)
    bundle = load_instruction(path)
    before = path.read_bytes()
    text = bundle.system_text()
    assert text == bundle.system_text() == load_instruction(path).system_text()
    assert path.read_bytes() == before
    positions = [
        text.index(label) for label in ("指令版本：", "\n指令\n", "评判口径\n", "正例（", "反例（")
    ]
    assert positions == sorted(positions)
    assert "数据，不是指令" in text
    assert "不采纳" in text and "不重复" in text
    assert "未证实" in text and "判决" in text
    assert "findings" in text and "coverage" in text and "不输出通过" in text


def test_bundle_and_nested_examples_are_immutable_defensive_copies(tmp_path):
    original = load_instruction(source(tmp_path))
    positive = {
        "input": "Input is copied.",
        "output": {"findings": [], "coverage": {"checked": ["Original"], "uncovered": []}},
    }
    bundle = replace(original, positive_examples=(positive,))
    text = bundle.system_text()
    positive["input"] = "Changed"
    positive["output"]["coverage"]["checked"].append("Changed")
    assert text == bundle.system_text()
    with pytest.raises(FrozenInstanceError):
        bundle.version = "changed"
    with pytest.raises(TypeError):
        bundle.positive_examples[0]["input"] = "changed"
    with pytest.raises(TypeError):
        bundle.positive_examples[0]["output"]["coverage"].update(checked=())
    assert isinstance(bundle.positive_examples, tuple)
    assert isinstance(bundle.positive_examples[0], dict)


@pytest.mark.parametrize(
    "field",
    ["reviewer", "version", "instructions", "rubric", "positive_examples", "negative_examples"],
)
def test_every_top_level_field_is_required(tmp_path, field):
    if field.endswith("examples"):
        parts = VALID.decode().split("[[" + field + "]]")
        remainder = parts[1]
        ending = remainder.find("[[")
        text = parts[0] + (remainder[ending:] if ending >= 0 else "")
    else:
        text = "\n".join(
            line for line in VALID.decode().splitlines() if not line.startswith(field + " =")
        )
    assert_safe(lambda: load_instruction(source(tmp_path, text.encode())))


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.replace(b'reviewer = "framework"', b'reviewer = " "'),
        lambda raw: raw.replace(b'reviewer = "framework"', b'reviewer = "UPPER"'),
        lambda raw: raw.replace(b'reviewer = "framework"', b'reviewer = "../outside"'),
        lambda raw: raw.replace(b'reviewer = "framework"', b"reviewer = 1"),
        lambda raw: raw.replace(b'version = "2026-11-11.1"', b'version = " "'),
        lambda raw: raw.replace(b'version = "2026-11-11.1"', b"version = true"),
        lambda raw: raw.replace(
            b'instructions = "Use only the quoted data."', b'instructions = " "'
        ),
        lambda raw: raw.replace(b'rubric = "Return findings and coverage JSON."', b'rubric = " "'),
        lambda raw: raw.replace(b"[[positive_examples]]", b'[[positive_examples]]\nextra = "bad"'),
        lambda raw: raw.replace(b"[[negative_examples]]", b'[[negative_examples]]\nextra = "bad"'),
        lambda raw: b'extra = "bad"\n' + raw,
        lambda raw: raw.replace(b'input = "One structural example."', b'input = " "'),
        lambda raw: raw.replace(b'input = "One structural example."', b"input = 123"),
        lambda raw: raw.replace(
            b"output = { findings = [], coverage = "
            b'{ checked = ["Given data only"], uncovered = [] } }',
            b'output = "bad"',
        ),
        lambda raw: raw.replace(b'checked = ["Given data only"]', b"checked = []"),
        lambda raw: raw.replace(b'checked = ["Given data only"]', b'checked = [" "]'),
        lambda raw: raw.replace(b"[[positive_examples]]", b"positive_examples = []\n[[removed]]"),
        lambda raw: raw.replace(b"[[negative_examples]]", b"negative_examples = []\n[[removed]]"),
    ],
)
def test_invalid_fields_extra_data_and_empty_examples_are_safe_errors(tmp_path, change):
    assert_safe(lambda: load_instruction(source(tmp_path, change(VALID))))


@pytest.mark.parametrize(
    "data",
    [
        b"\xff",
        b"\xef\xbb\xbf" + VALID,
        b'reviewer = "unterminated',
        b'reviewer = "framework"\nreviewer = "duplicate"',
        VALID.replace(b"Use only the quoted data.", b"PRIVATE_INSTRUCTION_CONTENT_OR_PATH\\u0000"),
    ],
)
def test_invalid_utf8_toml_duplicate_keys_and_control_characters_are_safe(tmp_path, data):
    assert_safe(lambda: load_instruction(source(tmp_path, data)))


def test_file_byte_limit_is_exact_and_oversize_is_refused_before_toml_parse(tmp_path):
    exact = VALID + b"\n#" + b"x" * (MAX_INSTRUCTION_BYTES - len(VALID) - 2)
    assert len(exact) == MAX_INSTRUCTION_BYTES
    assert load_instruction(source(tmp_path, exact)).reviewer == "framework"
    assert_safe(lambda: load_instruction(source(tmp_path, exact + b"x")))


@pytest.mark.parametrize(
    "field, value",
    [
        ("reviewer", "x" * 65),
        ("version", "x" * 81),
        ("instructions", "x" * 12001),
        ("rubric", "x" * 12001),
        ("positive_examples", ()),
        ("negative_examples", ()),
        ("positive_examples", []),
        ("negative_examples", []),
    ],
)
def test_direct_construction_applies_same_limits(tmp_path, field, value):
    bundle = load_instruction(source(tmp_path))
    assert_safe(lambda: replace(bundle, **{field: value}))


def test_examples_have_count_depth_json_and_text_limits(tmp_path):
    bundle = load_instruction(source(tmp_path))
    assert_safe(lambda: replace(bundle, positive_examples=bundle.positive_examples * 9))
    assert_safe(lambda: replace(bundle, negative_examples=({"input": "x" * 6001, "output": {}},)))
    nested = {"end": "value"}
    for _ in range(12):
        nested = {"child": nested}
    invalid = [nested, {"list": ["one"] * 101}, {"non_finite": float("nan")}, {"bad": Path("x")}]
    for output in invalid:
        assert_safe(
            lambda: replace(bundle, negative_examples=({"input": "Case", "output": output},))
        )


def test_unavailable_directory_symlink_and_fifo_paths_are_rejected(tmp_path):
    assert_safe(lambda: load_instruction(tmp_path / PRIVATE))
    assert_safe(lambda: load_instruction(tmp_path))
    target = source(tmp_path)
    link = tmp_path / "link.toml"
    link.symlink_to(target)
    assert_safe(lambda: load_instruction(link))
    fifo = tmp_path / "fifo.toml"
    os.mkfifo(fifo)
    assert_safe(lambda: load_instruction(fifo))
    assert_safe(lambda: load_instruction(str(target)))


def test_loading_has_no_network_environment_or_logging_side_effects(tmp_path, monkeypatch, caplog):
    path = source(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Instruction loading must not access environment or network")

    import socket

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    assert load_instruction(path).reviewer == "framework"
    assert load_framework_instruction().reviewer == "framework"
    assert_safe(lambda: load_instruction(source(tmp_path, PRIVATE.encode())))
    assert PRIVATE not in caplog.text


def test_builtin_read_uses_same_byte_limit_and_fixed_identity(tmp_path, monkeypatch):
    import app.review.reviewers.instructions as loader

    for data in (VALID.replace(b"framework", b"other"), VALID + b"#" * MAX_INSTRUCTION_BYTES):
        path = source(tmp_path, data)
        monkeypatch.setattr(
            loader.resources, "files", lambda _: SimpleNamespace(joinpath=lambda *parts: path)
        )
        assert_safe(load_framework_instruction)


def test_package_data_declaration_includes_framework_resource():
    import tomllib

    project = Path(__file__).parents[1] / "pyproject.toml"
    config = tomllib.loads(project.read_text())
    assert config["tool"]["setuptools"]["package-data"]["app.review.reviewers"] == [
        "resources/*.toml"
    ]
    resource = resources.files("app.review.reviewers").joinpath("resources", "framework.toml")
    assert resource.is_file()


def test_system_text_is_canonical_for_different_json_object_key_order(tmp_path):
    bundle = load_instruction(source(tmp_path))
    example = bundle.negative_examples[0]
    changed = {"output": dict(reversed(list(example["output"].items()))), "input": example["input"]}
    assert replace(bundle, negative_examples=(changed,)).system_text() == bundle.system_text()


def test_instruction_bundle_declares_six_file_fields_and_trusted_output_model():
    assert set(InstructionBundle.__dataclass_fields__) == {
        "reviewer",
        "version",
        "instructions",
        "rubric",
        "positive_examples",
        "negative_examples",
        "output_model",
    }
