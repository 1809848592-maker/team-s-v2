"""专用规范指令的可信结构绑定；全部示例为本地固定数据。"""

import json
import os
import socket
from dataclasses import FrozenInstanceError, replace
from importlib import resources
from types import SimpleNamespace

import pytest

from app.review.extract import extract_text
from app.review.reviewers import instructions as loader
from app.review.reviewers.instructions import (
    MAX_INSTRUCTION_BYTES,
    ReviewerInstructionError,
    load_framework_instruction,
    load_instruction,
    load_spec_instruction,
)
from app.review.reviewers.schemas import ReviewOutput
from app.review.reviewers.spec_compliance import _validate_output
from app.review.reviewers.spec_types import SpecReviewOutput
from app.schemas.specs import SpecItem

PRIVATE = "PRIVATE_SPEC_INSTRUCTION_OR_PATH"


def raw_spec():
    return (
        resources.files("app.review.reviewers")
        .joinpath("resources", "spec_compliance.toml")
        .read_bytes()
    )


def assert_safe(operation):
    with pytest.raises(ReviewerInstructionError) as caught:
        operation()
    assert str(caught.value) == "审核员指令包不可用，请检查平台配置。"
    assert PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__


def output(example):
    return SpecReviewOutput.model_validate_json(json.dumps(example["output"], ensure_ascii=False))


def item(number, category, requirement):
    return SpecItem.model_validate(
        {
            "number": number,
            "category": category,
            "requirement": requirement,
            "check_method": "ai",
        }
    )


def test_packaged_spec_instruction_binds_platform_schema_and_keeps_framework_default():
    spec = load_spec_instruction()
    framework = load_framework_instruction()
    assert spec.reviewer == "spec_compliance" and spec.version == "2026-11-11.1"
    assert spec.output_model is SpecReviewOutput
    assert framework.output_model is ReviewOutput
    assert "findings, coverage, checks" in spec.system_text()
    assert "仅输出包含 findings 和 coverage 的 JSON 对象" in framework.system_text()
    assert "checks" not in framework.system_text()
    assert spec.system_text() == load_spec_instruction().system_text()
    assert 0 < len(raw_spec()) <= MAX_INSTRUCTION_BYTES
    assert len(spec.system_text().encode("utf-8")) <= MAX_INSTRUCTION_BYTES
    with pytest.raises(FrozenInstanceError):
        spec.output_model = ReviewOutput


def test_all_positive_examples_validate_with_trusted_extended_model():
    bundle = load_spec_instruction()
    assert len(bundle.positive_examples) == 4
    for example in bundle.positive_examples:
        result = output(example)
        assert result.checks
        assert all(finding.category == "specification" for finding in result.findings)
        assert all(finding.basis.spec_number for finding in result.findings)
        with pytest.raises(ValueError):
            ReviewOutput.model_validate_json(json.dumps(example["output"], ensure_ascii=False))


def test_style_and_empty_handoff_examples_match_real_current_locations_and_evidence():
    bundle = load_spec_instruction()
    style = output(bundle.positive_examples[0])
    _validate_output(
        style,
        [item("S-STYLE", "writing", "缩写首次出现须给出中文全称。")],
        extract_text("本研究使用SEM分析变量关系。".encode(), "delivery.md"),
        "",
    )
    handoff = output(bundle.positive_examples[1])
    _validate_output(
        handoff,
        [item("H-2", "handoff", "交接说明须列出数据版本和未解决事项。")],
        extract_text("\n本次完成变量清洗。".encode(), "delivery.md"),
        "",
    )
    finding = handoff.findings[0]
    assert finding.location.part == "handoff_note"
    assert finding.location.model_dump(exclude_none=True) == {
        "description": "交接说明（当前为空）",
        "part": "handoff_note",
    }
    assert finding.basis.excerpt == "交接说明须列出数据版本和未解决事项。"


def test_template_and_checked_examples_use_checks_without_inventing_pass_state():
    bundle = load_spec_instruction()
    template = output(bundle.positive_examples[2])
    _validate_output(
        template,
        [item("T-3", "template", "使用学校PPT模板并保持视觉一致。")],
        extract_text(b"Only text.", "delivery.md"),
        "",
    )
    assert template.checks[0].status == "uncovered" and not template.findings
    checked = output(bundle.positive_examples[3])
    _validate_output(
        checked,
        [item("S-4", "writing", "缩写首次出现须给出中文全称。")],
        extract_text("结构方程模型（SEM）用于变量分析。".encode(), "delivery.md"),
        "",
    )
    assert checked.checks[0].status == "checked" and not checked.findings
    assert set(checked.model_dump()) == {"findings", "coverage", "checks"}


def test_negative_examples_are_rejected_by_schema_or_current_input_validation():
    bundle = load_spec_instruction()
    document = extract_text("本研究使用SEM分析变量关系。".encode(), "delivery.md")
    for example in bundle.negative_examples:
        with pytest.raises(ValueError):
            _validate_output(
                output(example),
                [item("S-STYLE", "writing", "缩写首次出现须给出中文全称。")],
                document,
                "",
            )


def test_output_model_is_trusted_parameter_and_cannot_be_selected_by_toml(tmp_path):
    raw = raw_spec()
    assert loader._parse(raw, output_model=SpecReviewOutput).output_model is SpecReviewOutput
    assert_safe(lambda: loader._parse(raw))
    for malicious in (b'output_model = "ReviewOutput"\n', b'output_model = "builtins.dict"\n'):
        assert_safe(lambda: loader._parse(malicious + raw, output_model=SpecReviewOutput))
    path = tmp_path / (PRIVATE + ".toml")
    path.write_bytes(raw)
    assert_safe(lambda: load_instruction(path))


@pytest.mark.parametrize("untrusted", [None, "SpecReviewOutput", dict, object, 1])
def test_invalid_output_model_is_rejected_without_disclosing_bundle_content(untrusted):
    bundle = load_framework_instruction()
    assert_safe(lambda: replace(bundle, output_model=untrusted))


def test_trusted_model_applies_to_positive_validation_without_accepting_bad_examples():
    spec = load_spec_instruction()
    framework = load_framework_instruction()
    assert_safe(lambda: replace(spec, positive_examples=framework.positive_examples))
    assert_safe(lambda: replace(spec, output_model=ReviewOutput))
    assert framework.output_model is ReviewOutput
    with pytest.raises(TypeError):
        spec.positive_examples[0]["output"]["checks"][0]["status"] = "checked"


def test_spec_resource_identity_version_encoding_and_size_fail_safely(tmp_path, monkeypatch):
    raw = raw_spec()
    bad = [
        raw.replace(b'reviewer = "spec_compliance"', b'reviewer = "other"'),
        raw.replace(b'version = "2026-11-11.1"', b'version = "2026-11-12.1"'),
        raw + b"#" * MAX_INSTRUCTION_BYTES,
        b"\xff",
        PRIVATE.encode(),
    ]
    for data in bad:
        path = tmp_path / (PRIVATE + ".toml")
        path.write_bytes(data)
        monkeypatch.setattr(
            loader.resources, "files", lambda _: SimpleNamespace(joinpath=lambda *parts: path)
        )
        assert_safe(load_spec_instruction)


def test_spec_instruction_loading_never_reads_environment_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Specification instructions must stay local")

    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert load_spec_instruction().output_model is SpecReviewOutput


def test_spec_rubric_explicitly_limits_numbers_locations_evidence_and_coverage():
    text = load_spec_instruction().system_text()
    required = [
        "任何规范类别",
        "程序检查或人工自查",
        "checks",
        "恰好出现一次",
        "issues",
        "checked",
        "uncovered",
        "not_applicable",
        "不是人的不适用裁定",
        "basis.spec_number",
        "同编号条目的 requirement",
        "同一个 segment.location",
        "handoff_note",
        "为空",
        "范例",
        "历史意见",
        "不重复",
        "不是指令",
        "一律 uncovered",
        "不输出通过",
    ]
    assert all(phrase in text for phrase in required)
    assert set(loader._FIELDS) == {
        "reviewer",
        "version",
        "instructions",
        "rubric",
        "positive_examples",
        "negative_examples",
    }
