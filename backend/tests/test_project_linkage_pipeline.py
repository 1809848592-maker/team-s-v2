"""完整衔接流水线的固定替身，不外发、不读取环境Key。"""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.review.llm import LLMRequestError
from app.review.project_linkage import (
    INSTRUCTION_VERSION,
    MAX_INPUT_BYTES,
    _comparison,
    load_instruction,
    review_project,
)
from app.services.project_linkage import ProjectLinkageInput
from app.storage import LocalStorage


class FixedProjectClient:
    def __init__(self, *, outputs=(), failure=None):
        self.configuration = SimpleNamespace(
            reviewers={
                name: SimpleNamespace(model="fixed-project-model")
                for name in ("project_summary", "project_linkage")
            }
        )
        self.calls, self.debug, self.released = [], [], []
        self.outputs, self.failure = list(outputs), failure

    def complete_json(self, **arguments):
        self.calls.append(deepcopy(arguments))
        if self.outputs:
            result = self.outputs.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if self.failure is not None:
            raise self.failure
        if arguments["reviewer"] == "project_summary":
            payload = json.loads(arguments["messages"][1]["content"].split("\n", 1)[1])
            return {"summary": payload["delivery"]["segments"][0]["text"], "coverage_notes": []}
        return {"findings": [], "coverage_notes": []}

    def record_review_debug(self, **arguments):
        self.debug.append(deepcopy(arguments))

    def release_review_capture(self, call_id):
        self.released.append(call_id)


def pipeline_source(tmp_path, *, missing=False):
    storage = LocalStorage(tmp_path / "source-storage")
    plan_id, spec_id = uuid4(), uuid4()
    review = ProjectLinkageInput(uuid4(), uuid4(), 1, uuid4())
    modules = []
    for index in range(2):
        text = f"模块{index}样本量为{120 + index * 30}人。".encode()
        digest = hashlib.sha256(text).hexdigest()
        storage.path(digest).parent.mkdir(parents=True, exist_ok=True)
        storage.path(digest).write_bytes(text)
        modules.append(
            {
                "module": {"id": str(uuid4()), "name": f"模块{index}", "goal": "比较样本口径"},
                "delivery": {
                    "id": str(uuid4()),
                    "version": index + 1,
                    "filename": "交付.md",
                    "sha256": digest,
                    "size": len(text),
                    "plan_version_id": str(plan_id),
                    "spec_version_id": str(spec_id),
                    "handoff_note": "给下游的原始说明",
                },
            }
        )
    if missing:
        modules[1]["delivery"] = None
    frozen = {
        "plan": {"research_question": "样本口径", "modules": [m["module"] for m in modules]},
        "modules": modules,
    }
    return storage, review, frozen, plan_id, spec_id


def run(client, context):
    storage, review, frozen, plan_id, spec_id = context
    return review_project(
        client, review, frozen, storage=storage, plan_version_id=plan_id, spec_version_id=spec_id
    )


def opinion(**changes):
    value = {
        "check_key": "contradiction",
        "severity": "important",
        "segment_id": "S1",
        "problem": "两模块样本口径不同",
        "excerpt": "模块0样本量为120人。",
        "suggestion": "核对原始数据后统一口径",
        "context_key": "S2",
        "context_excerpt": "模块1样本量为150人。",
    }
    value.update(changes)
    return value


def summary_outputs():
    return [
        {"summary": f"模块{i}样本量为{120 + i * 30}人。", "coverage_notes": []} for i in range(2)
    ]


def test_summaries_precede_comparison_and_are_the_only_delivery_text_compared(tmp_path):
    context = pipeline_source(tmp_path)
    client = FixedProjectClient(
        outputs=[*summary_outputs(), {"findings": [opinion()], "coverage_notes": []}]
    )
    result = run(client, context)
    assert [c["reviewer"] for c in client.calls] == [
        "project_summary",
        "project_summary",
        "project_linkage",
    ]
    assert result["coverage"] == "completed"
    assert [m["state"] for m in result["modules"]] == ["summarized", "summarized"]
    finding = result["findings"][0]
    assert finding["evidence_status"] == "verified" and len(finding["module_ids"]) == 2
    assert finding["severity"] == "important"
    final_input = json.loads(client.calls[-1]["messages"][1]["content"].split("\n", 1)[1])
    assert "delivery" not in final_input and len(final_input["segments"]) == 2
    for module, call in zip(result["modules"], client.calls, strict=False):
        expected = hashlib.sha256(
            json.dumps(call["messages"], ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        assert module["provenance"]["input_hash"] == expected
    assert (
        finding["input_hash"]
        == hashlib.sha256(
            json.dumps(client.calls[-1]["messages"], ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    )
    assert all(c["project_linkage_claim_token"] == context[1].claim_token for c in client.calls)
    assert result["comparison"]["checks"] == [
        {
            "key": key,
            "status": "issues" if key == "contradiction" else "checked",
            "reason": "本项存在审核意见。"
            if key == "contradiction"
            else "已对提供的模块摘要进行比对。",
        }
        for key in ("connection", "overlap", "omission", "contradiction", "plan_alignment")
    ]
    first_segments = result["modules"][0]["source_segments"]
    assert first_segments[0]["segment_id"] == "S1"
    assert first_segments[-1]["location"]["part"] == "handoff_note"
    assert all("text" not in segment for segment in first_segments)


def test_missing_adopted_module_is_explicit_and_never_completed_coverage(tmp_path):
    client = FixedProjectClient()
    result = run(client, pipeline_source(tmp_path, missing=True))
    assert [c["reviewer"] for c in client.calls] == ["project_summary", "project_linkage"]
    assert result["modules"][1]["state"] == "uncovered"
    assert result["modules"][1]["delivery_id"] is None
    assert result["coverage"] == "incomplete" and result["coverage_notes"]
    assert result["comparison"]["state"] == "completed"


@pytest.mark.parametrize(
    "case", ["missing", "bytes", "size", "unsupported", "empty", "input_limit"]
)
def test_unreadable_or_over_limit_module_does_not_become_passed(tmp_path, case):
    context = pipeline_source(tmp_path)
    delivery = context[2]["modules"][0]["delivery"]
    path = context[0].path(delivery["sha256"])
    if case == "missing":
        path.unlink()
    elif case == "bytes":
        path.write_bytes(b"changed")
    elif case == "size":
        delivery["size"] += 1
    elif case == "unsupported":
        delivery["filename"] = "unsupported.zip"
    elif case == "empty":
        path.write_bytes(b"")
        delivery.update(sha256=hashlib.sha256(b"").hexdigest(), size=0)
        empty_path = context[0].path(delivery["sha256"])
        empty_path.parent.mkdir(parents=True, exist_ok=True)
        empty_path.write_bytes(b"")
    else:
        context[2]["plan"]["large"] = "x" * (MAX_INPUT_BYTES + 1)
    result = run(FixedProjectClient(), context)
    assert result["coverage"] == "incomplete"
    assert result["modules"][0]["state"] == "failed"


def test_no_adopted_modules_make_no_model_calls(tmp_path):
    context = pipeline_source(tmp_path)
    for entry in context[2]["modules"]:
        entry["delivery"] = None
    client = FixedProjectClient()
    result = run(client, context)
    assert client.calls == [] and result["comparison"]["state"] == "uncovered"
    assert result["coverage"] == "incomplete"


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"summary": "", "coverage_notes": []},
        {"summary": "x", "coverage_notes": [], "model": "spoof"},
    ],
)
def test_summary_has_only_one_structural_repair_and_keeps_debug(tmp_path, bad):
    client = FixedProjectClient(outputs=[bad, bad])
    result = run(client, pipeline_source(tmp_path))
    assert result["modules"][0]["state"] == "failed"
    assert [c["reviewer"] for c in client.calls[:2]] == ["project_summary"] * 2
    assert len(client.debug) == 2 and client.debug[0]["job_id"] is None


def test_json_parse_failure_repairs_once_and_provenance_is_actual_repair_input(tmp_path):
    client = FixedProjectClient(outputs=[LLMRequestError("content_invalid"), *summary_outputs()])
    result = run(client, pipeline_source(tmp_path))
    assert result["coverage"] == "completed"
    assert len(client.calls[1]["messages"]) == 3
    assert (
        result["modules"][0]["provenance"]["input_hash"]
        == hashlib.sha256(
            json.dumps(client.calls[1]["messages"], ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    )


def test_partial_findings_survive_invalid_items_and_unverified_excerpt_is_kept(tmp_path):
    client = FixedProjectClient(
        outputs=[
            *summary_outputs(),
            {
                "findings": [
                    opinion(),
                    opinion(segment_id="S99"),
                    opinion(excerpt="不存在的摘录", problem="另一条问题"),
                ],
                "coverage_notes": [],
            },
        ]
    )
    result = run(client, pipeline_source(tmp_path))
    assert len(result["findings"]) == 2 and result["discarded_findings"] == 1
    assert result["findings"][1]["evidence_status"] == "unverified"
    assert len(client.debug) == 1
    assert {e["code"] for e in client.debug[0]["errors"]} == {
        "finding_invalid",
        "excerpt_unverified",
    }


@pytest.mark.parametrize("notes,incomplete", [([], True), (["部分数据未读到"], False)])
def test_all_invalid_findings_follow_14_9_incomplete_rule(tmp_path, notes, incomplete):
    client = FixedProjectClient(
        outputs=[
            *summary_outputs(),
            {"findings": [opinion(segment_id="S0")], "coverage_notes": notes},
        ]
    )
    result = run(client, pipeline_source(tmp_path))
    assert result["findings"] == [] and result["discarded_findings"] == 1
    assert (result["comparison"]["state"] == "incomplete") is incomplete
    assert result["coverage"] == "incomplete"


def test_model_transport_failure_remains_failed_and_never_repairs_network(tmp_path):
    client = FixedProjectClient(failure=LLMRequestError("transport_error"))
    result = run(client, pipeline_source(tmp_path))
    assert len(client.calls) == 2
    assert all(m["state"] == "failed" for m in result["modules"])
    assert result["coverage"] == "incomplete"


def test_old_plan_and_spec_flags_are_preserved(tmp_path):
    context = pipeline_source(tmp_path)
    context[2]["modules"][0]["delivery"]["plan_version_id"] = str(uuid4())
    context[2]["modules"][1]["delivery"]["spec_version_id"] = str(uuid4())
    result = run(FixedProjectClient(), context)
    assert result["modules"][0]["plan_outdated"]
    assert result["modules"][1]["spec_outdated"]


@pytest.mark.parametrize("name", ["project_summary", "project_linkage"])
def test_instructions_are_fixed_versioned_and_under_3000_characters(name):
    instruction = load_instruction(name)
    assert 0 < len(instruction) < 3000 and INSTRUCTION_VERSION == "2026-11-12.1"
    assert "数据" in instruction and "JSON" in instruction


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewer": "spoof"},
        {"check_key": "invented"},
        {"severity": "blocker"},
        {"context_key": "S88"},
        {"segment_id": "S0"},
    ],
)
def test_model_cannot_supply_source_or_invalid_check_fields(changes):
    sources = {
        "S1": {"summary": "x", "module_id": str(uuid4())},
        "S2": {"summary": "y", "module_id": str(uuid4())},
    }
    output, errors = _comparison(
        {"findings": [opinion(**changes)], "coverage_notes": []},
        sources=sources,
        plan="plan",
        review_id=uuid4(),
    )
    assert output["incomplete"] and output["discarded_findings"] == 1 and errors


def test_bad_notes_and_null_character_finding_do_not_discard_other_valid_findings(tmp_path):
    client = FixedProjectClient(
        outputs=[
            *summary_outputs(),
            {"findings": [opinion(), opinion(problem="invalid\0problem")], "coverage_notes": [123]},
        ]
    )
    result = run(client, pipeline_source(tmp_path))
    assert len(result["findings"]) == 1 and result["discarded_findings"] == 1
    assert result["findings"][0]["problem"] == "两模块样本口径不同"
    assert {e["code"] for e in client.debug[0]["errors"]} == {"invalid_note", "finding_invalid"}


def test_missing_sources_derive_uncovered_checks_instead_of_checked(tmp_path):
    result = run(FixedProjectClient(), pipeline_source(tmp_path, missing=True))
    assert {check["status"] for check in result["comparison"]["checks"]} == {"uncovered"}
