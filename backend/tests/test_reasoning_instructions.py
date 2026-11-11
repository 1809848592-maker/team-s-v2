"""本地逻辑/衔接指令包与真实当前位置、独立上下文证据契约。"""

import json
import os
import socket
from importlib import resources
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.review.extract_types import ExtractedDocument, TextLocation, TextSegment
from app.review.reasoning import _validate_output
from app.review.reviewers import instructions as loader
from app.review.reviewers.instructions import ReviewerInstructionError, load_reasoning_instruction
from app.review.reviewers.reasoning_types import ReasoningOutput

REVIEWERS = ("logic", "linkage")
UPSTREAM = "upstream:11111111-1111-4111-8111-111111111111"


def fixture(reviewer, index):
    """Reconstruct the examples' supplied sources instead of trusting their prose claims."""
    if reviewer == "logic":
        cases = [
            (
                "本模块只列出协作方式名称，不讨论其影响机制。",
                {"module.goal": "解释协作方式影响任务完成率的机制。"},
                False,
            ),
            (
                "本样本的协作频率与完成率相关。",
                {
                    "module.goal": "报告当前样本中的相关性。",
                    "module.core_argument": "当前样本的相关性只用于描述，不作因果推断。",
                    "plan.argument": "分清描述与因果解释。",
                },
                True,
            ),
            (
                "本样本中两变量同时上升，因此协作必然导致所有团队产出提高。",
                {"module.goal": "描述本样本关系。"},
                False,
            ),
            (
                "本模块据当前样本差异提出适用于所有团队的统一规律。",
                {
                    "module.goal": "解释当前样本中的协作差异。",
                    "plan.argument": "各模块只解释当前样本差异，不将结论推广到所有团队。",
                },
                True,
            ),
        ]
        text, sources, argument_available = cases[index]
        expected = {
            "goal": {"available": True, "sources": {"module.goal"}},
            "argument": {
                "available": argument_available,
                "sources": {"module.core_argument", "plan.argument"},
            },
            "support": {"available": True, "sources": set(sources)},
        }
        handoff = ""
    else:
        cases = [
            (
                "本稿将参与率定义为参加次数除以活动总次数。",
                "",
                {
                    "module.scope_in": "报告参与情况。",
                    "term:0": "参与率\n实际参与人数除以应参与人数。\n绑定方案",
                },
                {"scope": True, "term:0": True, "data_definitions": False, "upstream": False},
            ),
            (
                "本稿说明人口样本处理。",
                "本次样本只保留城市户籍人口。",
                {
                    "module.scope_in": "说明人口样本处理。",
                    "data:0": "常住人口\n统计年鉴\n包含城镇与农村常住人口。",
                },
                {"scope": True, "terms": False, "data:0": True, "upstream": False},
            ),
            (
                "本稿使用上游数据v1，保留缺失记录。",
                "",
                {
                    "module.scope_in": "使用上游清洗数据分析。",
                    UPSTREAM: "已采用交付使用数据v2，剔除缺失记录。",
                },
                {"scope": True, "terms": False, "data_definitions": False, UPSTREAM: True},
            ),
            (
                "本稿报告当前结果。",
                "",
                {},
                {"scope": False, "terms": False, "data_definitions": False, UPSTREAM: False},
            ),
        ]
        text, handoff, sources, available = cases[index]
        expected = {
            key: {
                "available": value,
                "sources": {"module.scope_in", "module.scope_out"}
                if key == "scope"
                else {key}
                if value or key == UPSTREAM
                else set(),
            }
            for key, value in available.items()
        }
    document = ExtractedDocument(
        format="md",
        sha256="0" * 64,
        segments=(
            TextSegment(
                text, TextLocation(part="markdown", line_start=index + 1, line_end=index + 1)
            ),
        ),
        warnings=(),
    )
    return expected, sources, document, handoff


@pytest.mark.parametrize("reviewer", REVIEWERS)
def test_resources_have_fixed_six_fields_trusted_schema_and_immutable_examples(reviewer):
    bundle = load_reasoning_instruction(reviewer)
    assert bundle.reviewer == reviewer and bundle.version == "2026-11-12.1"
    assert bundle.output_model is ReasoningOutput
    assert len(bundle.positive_examples) == 4 and len(bundle.negative_examples) == 2
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
@pytest.mark.parametrize("index", range(4))
def test_all_positive_examples_use_real_current_and_context_evidence(reviewer, index):
    example = load_reasoning_instruction(reviewer).positive_examples[index]
    output = ReasoningOutput.model_validate_json(json.dumps(example["output"], ensure_ascii=False))
    expected, sources, document, handoff = fixture(reviewer, index)
    _validate_output(output, reviewer, expected, sources, document, handoff)
    assert set(check.key for check in output.checks) == set(expected)
    assert all(finding.severity in {"important", "suggestion"} for finding in output.findings)
    assert all(finding.category == reviewer for finding in output.findings)
    assert all(
        check.status == "uncovered"
        for check in output.checks
        if not expected[check.key]["available"]
    )


@pytest.mark.parametrize("reviewer", REVIEWERS)
def test_negative_examples_include_structural_and_semantic_rejections(reviewer):
    bundle = load_reasoning_instruction(reviewer)
    with pytest.raises(ValidationError):
        ReasoningOutput.model_validate_json(json.dumps(bundle.negative_examples[0]["output"]))
    output = ReasoningOutput.model_validate_json(json.dumps(bundle.negative_examples[1]["output"]))
    expected, sources, document, handoff = fixture(reviewer, 0)
    with pytest.raises(ValueError):
        _validate_output(output, reviewer, expected, sources, document, handoff)


def test_support_example_has_body_only_basis_while_plan_issue_has_independent_context():
    bundle = load_reasoning_instruction("logic")
    support = ReasoningOutput.model_validate_json(json.dumps(bundle.positive_examples[2]["output"]))
    argument = ReasoningOutput.model_validate_json(
        json.dumps(bundle.positive_examples[3]["output"])
    )
    assert support.finding_links[0].check_key == "support"
    assert support.finding_links[0].context_refs == ()
    assert argument.finding_links[0].check_key == "argument"
    assert argument.finding_links[0].context_refs[0].key == "plan.argument"
    assert argument.findings[0].basis.excerpt != argument.finding_links[0].context_refs[0].excerpt


def test_linkage_examples_separate_handoff_location_and_adopted_upstream_basis():
    bundle = load_reasoning_instruction("linkage")
    handoff = ReasoningOutput.model_validate_json(json.dumps(bundle.positive_examples[1]["output"]))
    upstream = ReasoningOutput.model_validate_json(
        json.dumps(bundle.positive_examples[2]["output"])
    )
    location = handoff.findings[0].location.model_dump(exclude={"description"})
    assert location.pop("part") == "handoff_note"
    assert all(value is None for value in location.values())
    assert handoff.finding_links[0].context_refs[0].key == "data:0"
    assert upstream.finding_links[0].context_refs[0].key == UPSTREAM
    assert upstream.findings[0].location.part == "markdown"


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
        loader._parse(b'output_model="builtins.dict"\n' + raw, output_model=ReasoningOutput)
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
        "version": raw.replace(b"2026-11-12.1", b"2026-11-13.1"),
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
        assert set(ReasoningOutput.model_fields) == {
            "findings",
            "coverage",
            "checks",
            "finding_links",
        }
