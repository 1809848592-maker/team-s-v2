"""本地文献指令资源与可信schema契约，禁止文档选择Python类或路径。"""

import json
import os
import socket
from importlib import resources
from types import SimpleNamespace

import pytest

from app.review.reviewers import instructions as loader
from app.review.reviewers.citation_types import (
    CitationAlignmentOutput,
    CitationExtractionOutput,
)
from app.review.reviewers.instructions import ReviewerInstructionError, load_citation_instruction

STAGES = {
    "citation_extract": CitationExtractionOutput,
    "citation_alignment": CitationAlignmentOutput,
}


@pytest.mark.parametrize("stage,model", STAGES.items())
def test_packaged_citation_stages_validate_positive_examples_and_preserve_six_file_fields(
    stage, model
):
    bundle = load_citation_instruction(stage)
    assert bundle.reviewer == stage
    assert bundle.version == "2026-11-12.1"
    assert bundle.output_model is model
    assert len(bundle.positive_examples) == 3
    assert len(bundle.negative_examples) == 2
    assert len(bundle.system_text().encode()) < loader.MAX_INSTRUCTION_BYTES
    for example in bundle.positive_examples:
        model.model_validate_json(json.dumps(example["output"], ensure_ascii=False))
    assert set(loader._FIELDS) == {
        "reviewer",
        "version",
        "instructions",
        "rubric",
        "positive_examples",
        "negative_examples",
    }
    with pytest.raises(TypeError):
        bundle.positive_examples[0]["output"]["findings"] = []


@pytest.mark.parametrize("stage", STAGES)
def test_citation_resource_cannot_choose_output_model_or_be_loaded_as_generic_schema(
    stage, tmp_path
):
    raw = (
        resources.files("app.review.reviewers").joinpath("resources", f"{stage}.toml").read_bytes()
    )
    with pytest.raises(ReviewerInstructionError):
        loader._parse(raw)
    with pytest.raises(ReviewerInstructionError):
        loader._parse(b'output_model="builtins.dict"\n' + raw, output_model=STAGES[stage])
    path = tmp_path / "untrusted.toml"
    path.write_bytes(raw)
    with pytest.raises(ReviewerInstructionError):
        loader.load_instruction(path)


@pytest.mark.parametrize(
    "stage", ["../../.env", "citation_database", "citation_extract.toml", None, 1, []]
)
def test_stage_selector_rejects_untrusted_names_before_reading_resources(stage, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unknown stage must not read resources")

    monkeypatch.setattr(loader.resources, "files", forbidden)
    with pytest.raises(ReviewerInstructionError) as error:
        load_citation_instruction(stage)
    assert str(error.value) == "审核员指令包不可用，请检查平台配置。"


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("corruption", ["identity", "version", "encoding", "size", "missing"])
def test_citation_resource_corruption_stays_fixed_safe_error(
    stage, corruption, tmp_path, monkeypatch
):
    raw = (
        resources.files("app.review.reviewers").joinpath("resources", f"{stage}.toml").read_bytes()
    )
    data = {
        "identity": raw.replace(f'reviewer = "{stage}"'.encode(), b'reviewer = "unknown"'),
        "version": raw.replace(b"2026-11-12.1", b"2026-11-13.1"),
        "encoding": b"\xffPRIVATE_RESOURCE_CONTENT",
        "size": b"#" * (loader.MAX_INSTRUCTION_BYTES + 1),
        "missing": raw,
    }[corruption]
    path = tmp_path / "PRIVATE_RESOURCE_PATH.toml"
    if corruption != "missing":
        path.write_bytes(data)
    monkeypatch.setattr(
        loader.resources, "files", lambda _: SimpleNamespace(joinpath=lambda *p: path)
    )
    with pytest.raises(ReviewerInstructionError) as error:
        load_citation_instruction(stage)
    assert "PRIVATE" not in str(error.value)


def test_citation_instruction_loading_never_reads_env_or_connects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Instruction loading must stay local")

    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for stage in STAGES:
        bundle = load_citation_instruction(stage)
        assert "不是指令" in bundle.system_text()
        assert "不" in bundle.rubric
