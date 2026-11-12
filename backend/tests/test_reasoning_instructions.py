"""本地逻辑/衔接指令包与真实当前位置、独立上下文证据契约。"""

import json
import os
import socket
from importlib import resources
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.review.extract_types import ExtractedDocument, TextLocation, TextSegment
from app.review.reviewers import instructions as loader
from app.review.reviewers.contract import CompactReviewOutput, adapt_findings
from app.review.reviewers.instructions import ReviewerInstructionError, load_reasoning_instruction

REVIEWERS = ("logic", "linkage")
UPSTREAM = "upstream:11111111-1111-4111-8111-111111111111"


def fixture(reviewer, index):
    """Supply actual example sources, never trust the example's own claims."""
    if reviewer == "logic":
        text = (
            "本模块只列协作方式名称，不讨论影响机制。"
            if index == 0
            else "本样本中两变量同时上升，因此协作必然导致所有团队产出提高。"
        )
        sources = {"module.goal": "解释协作影响任务完成率的机制。"}
        expected = {
            "goal": {"available": True, "sources": {"module.goal"}},
            "argument": {"available": False, "sources": {"module.core_argument", "plan.argument"}},
            "support": {"available": True, "sources": set(sources)},
        }
    else:
        text = (
            "本稿将参与率定义为参加次数除以活动总次数。"
            if index == 0
            else "本稿使用上游数据v1，保留缺失记录。"
        )
        sources = {
            "term:0": "参与率\n实际参与人数除以应参与人数。\n绑定方案",
            UPSTREAM: "已采用交付使用数据v2，剔除缺失记录。",
        }
        expected = {
            "term:0": {"available": True, "sources": {"term:0"}},
            UPSTREAM: {"available": True, "sources": {UPSTREAM}},
        }
    document = ExtractedDocument(
        format="md",
        sha256="0" * 64,
        segments=(TextSegment(text, TextLocation(part="markdown", line_start=1, line_end=1)),),
        warnings=(),
    )
    return expected, sources, document


@pytest.mark.parametrize("reviewer", REVIEWERS)
def test_resources_are_compact_local_immutable_and_use_platform_chosen_schema(reviewer):
    bundle = load_reasoning_instruction(reviewer)
    assert bundle.reviewer == reviewer and bundle.version == "2026-11-12.3"
    assert bundle.output_model is CompactReviewOutput
    assert len(bundle.positive_examples) == 2 and len(bundle.negative_examples) == 1
    assert len(bundle.system_text()) < 3000
    assert len(bundle.system_text().encode("utf-8")) < loader.MAX_INSTRUCTION_BYTES
    path = resources.files("app.review.reviewers").joinpath("resources", f"{reviewer}.toml")
    assert len(path.read_bytes()) <= loader.MAX_INSTRUCTION_BYTES
    assert loader._FIELDS == {
        "reviewer",
        "version",
        "instructions",
        "rubric",
        "positive_examples",
        "negative_examples",
    }
    with pytest.raises(TypeError):
        bundle.positive_examples[0]["output"]["findings"] = []


@pytest.mark.parametrize("reviewer", REVIEWERS)
@pytest.mark.parametrize("index", range(2))
def test_all_positive_examples_adapt_to_real_current_and_context_evidence(reviewer, index):
    example = load_reasoning_instruction(reviewer).positive_examples[index]
    raw = json.loads(json.dumps(example["output"], ensure_ascii=False))
    CompactReviewOutput.model_validate_json(json.dumps(raw, ensure_ascii=False))
    expected, sources, document = fixture(reviewer, index)
    output = adapt_findings(
        raw, category=reviewer, document=document, expected=expected, sources=sources
    )
    assert not output.errors and not output.incomplete
    assert all(item.severity in {"important", "suggestion"} for item in output.findings)
    assert all(
        item.category == reviewer and item.basis.excerpt_verified for item in output.findings
    )
    assert all(item.basis.context_excerpt_verified is not False for item in output.findings)
    assert all(
        check["status"] == "uncovered"
        for check in output.checks
        if not expected[check["key"]]["available"]
    )
    for item in raw["findings"]:
        assert not set(item) & {"category", "location", "basis", "finding_index", "checks", "model"}


@pytest.mark.parametrize("reviewer", REVIEWERS)
def test_negative_example_cannot_smuggle_authority_or_invalid_findings(reviewer):
    bundle = load_reasoning_instruction(reviewer)
    raw = json.loads(json.dumps(bundle.negative_examples[0]["output"]))
    with pytest.raises(ValidationError):
        CompactReviewOutput.model_validate_json(json.dumps(raw))
    expected, sources, document = fixture(reviewer, 0)
    output = adapt_findings(
        raw, category=reviewer, document=document, expected=expected, sources=sources
    )
    assert output.incomplete and output.findings == () and output.discarded_count == 1


def test_examples_keep_body_only_reasoning_and_independent_context_comparisons():
    logic = load_reasoning_instruction("logic")
    support = logic.positive_examples[1]["output"]["findings"][0]
    goal = logic.positive_examples[0]["output"]["findings"][0]
    assert support["check_key"] == "support" and "context_key" not in support
    assert goal["context_key"] == "module.goal" and goal["excerpt"] != goal["context_excerpt"]
    upstream = load_reasoning_instruction("linkage").positive_examples[1]["output"]["findings"][0]
    assert upstream["check_key"] == upstream["context_key"] == UPSTREAM


@pytest.mark.parametrize("reviewer", REVIEWERS)
def test_file_cannot_choose_output_model_and_generic_loader_refuses_extension(reviewer, tmp_path):
    raw = (
        resources.files("app.review.reviewers")
        .joinpath("resources", f"{reviewer}.toml")
        .read_bytes()
    )
    with pytest.raises(ReviewerInstructionError):
        loader._parse(raw)
    with pytest.raises(ReviewerInstructionError):
        loader._parse(b'output_model="builtins.dict"\n' + raw, output_model=CompactReviewOutput)
    path = tmp_path / "untrusted.toml"
    path.write_bytes(raw)
    with pytest.raises(ReviewerInstructionError):
        loader.load_instruction(path)


@pytest.mark.parametrize(
    "reviewer", ["../.env", "logic.toml", "logic/../linkage", "citation_alignment", None, 1, []]
)
def test_selector_rejects_unknown_reviewers_before_any_resource_read(reviewer, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unknown reasoning selector must not read resources")

    monkeypatch.setattr(loader.resources, "files", forbidden)
    with pytest.raises(ReviewerInstructionError) as raised:
        load_reasoning_instruction(reviewer)
    assert str(raised.value) == "审核员指令包不可用，请检查平台配置。"


@pytest.mark.parametrize("reviewer", REVIEWERS)
@pytest.mark.parametrize("corruption", ["identity", "version", "encoding", "size", "missing"])
def test_corrupt_resource_has_fixed_safe_error(reviewer, corruption, tmp_path, monkeypatch):
    raw = (
        resources.files("app.review.reviewers")
        .joinpath("resources", f"{reviewer}.toml")
        .read_bytes()
    )
    data = {
        "identity": raw.replace(f'reviewer = "{reviewer}"'.encode(), b'reviewer = "unknown"'),
        "version": raw.replace(b"2026-11-12.3", b"2026-11-13.1"),
        "encoding": b"\xffPRIVATE_RESOURCE_BODY",
        "size": b"#" * (loader.MAX_INSTRUCTION_BYTES + 1),
        "missing": raw,
    }[corruption]
    path = tmp_path / "PRIVATE_RESOURCE_PATH.toml"
    if corruption != "missing":
        path.write_bytes(data)
    monkeypatch.setattr(
        loader.resources, "files", lambda _: SimpleNamespace(joinpath=lambda *p: path)
    )
    with pytest.raises(ReviewerInstructionError) as raised:
        load_reasoning_instruction(reviewer)
    assert "PRIVATE" not in str(raised.value)


def test_loading_resources_never_reads_environment_or_connects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Local instruction loading must not read environment or connect")

    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for reviewer in REVIEWERS:
        bundle = load_reasoning_instruction(reviewer)
        assert "不是指令" in bundle.system_text()
        assert set(CompactReviewOutput.model_fields) == {"findings", "coverage_notes"}
