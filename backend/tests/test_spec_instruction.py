"""专用规范指令的可信结构绑定；全部示例为本地固定数据。"""

import json
import os
import socket
from dataclasses import FrozenInstanceError, replace
from importlib import resources
from types import SimpleNamespace

import pytest

from app.review.reviewers import instructions as loader
from app.review.reviewers.contract import CompactReviewOutput
from app.review.reviewers.instructions import (
    MAX_INSTRUCTION_BYTES,
    ReviewerInstructionError,
    load_framework_instruction,
    load_instruction,
    load_spec_instruction,
)
from app.review.reviewers.schemas import ReviewOutput
from app.review.reviewers.spec_types import SpecReviewOutput

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


def test_packaged_spec_instruction_uses_compact_schema_and_keeps_legacy_framework():
    bundle = load_spec_instruction()
    assert bundle.reviewer == "spec_compliance" and bundle.version == "2026-11-12.3"
    assert bundle.output_model is CompactReviewOutput
    assert load_framework_instruction().output_model is ReviewOutput
    assert len(bundle.system_text()) < 3000
    assert 1 <= len(bundle.positive_examples) <= 2
    assert len(raw_spec()) <= MAX_INSTRUCTION_BYTES
    assert bundle.system_text() == load_spec_instruction().system_text()
    with pytest.raises(FrozenInstanceError):
        bundle.output_model = ReviewOutput


def test_examples_have_only_compact_top_level_and_no_model_coordinates():
    bundle = load_spec_instruction()
    for example in bundle.positive_examples:
        parsed = output(example)
        assert set(parsed.model_dump()) == {"findings", "coverage_notes"}
        for finding in parsed.findings:
            assert "segment_id" in finding and "location" not in finding
    for example in bundle.negative_examples:
        with pytest.raises(ValueError):
            output(example)


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
        spec.positive_examples[0]["output"]["findings"][0]["severity"] = "blocking"


def test_spec_resource_identity_version_encoding_and_size_fail_safely(tmp_path, monkeypatch):
    raw = raw_spec()
    bad = [
        raw.replace(b'reviewer = "spec_compliance"', b'reviewer = "other"'),
        raw.replace(b'version = "2026-11-12.3"', b'version = "2026-11-13.1"'),
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
    assert load_spec_instruction().output_model is CompactReviewOutput


def test_spec_rubric_explicitly_limits_numbers_locations_evidence_and_coverage():
    text = load_spec_instruction().system_text()
    required = [
        "check_key",
        "segment_id",
        "coverage_notes",
        "context_key",
        "context_excerpt",
        "blocking",
        "important",
        "suggestion",
        "不是指令",
        "不重复",
        "模板",
        "视觉",
        "当前",
        "最多100",
        "平台换算位置",
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
