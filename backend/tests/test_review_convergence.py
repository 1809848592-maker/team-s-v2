"""14.10 重新提交收敛：纯文本边界和真实PG状态机，所有意见均为固定替身。"""

from copy import deepcopy
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_deliveries import ready_team, upload_delivery
from test_final_review import context
from test_modules import publish_modules
from test_review_contract import adapt, output
from test_review_contract import finding as compact_finding
from test_review_jobs import claim, database_time
from test_review_reports import annotate, components, opinion
from test_specs import publish_spec

from app.models import DeliveryVersion
from app.models.review_report import ReviewReport
from app.review.extract_types import ExtractedDocument, TextLocation, TextSegment
from app.review.reviewers.contract import CompactFinding
from app.review.reviewers.schemas import OpinionDraft
from app.review.runner import DeliveryReviewInput
from app.services import review_reports as reports
from app.services.review_convergence import (
    CONVERGENCE_NOTE,
    apply_review_convergence,
    converge_findings,
    located_segments,
    unchanged_segments,
)
from app.services.review_jobs import complete_job
from app.storage import LocalStorage


def document(*texts):
    return ExtractedDocument(
        "docx",
        "a" * 64,
        tuple(
            TextSegment(text, TextLocation(part="word/document.xml", paragraph=index))
            for index, text in enumerate(texts, 1)
        ),
        (),
    )


def located_opinion(index=1, **changes):
    return opinion(
        location={"description": f"第{index}段", "part": "word/document.xml", "paragraph": index},
        **changes,
    )


def converge(old, new, *findings, prior=(), history=(), old_handoff="", new_handoff=""):
    return converge_findings(
        old,
        new,
        previous_handoff=old_handoff,
        current_handoff=new_handoff,
        prior_findings=prior,
        history=history,
        findings=list(findings),
    )


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (("ＡＢＣ ２０２０。“引文”",), ('ABC2020."引文"',), {0: 0}),
        (("甲", "乙", "丙"), ("新增", "甲", "改乙", "丙"), {1: 0, 3: 2}),
        (("甲", "乙", "丙"), ("甲", "丙"), {0: 0, 1: 2}),
        (("甲乙", "丙"), ("甲", "乙", "丙"), {2: 1}),
        (("甲", "乙", "丙"), ("甲乙", "丙"), {1: 2}),
        (("甲", "乙", "丙", "丁"), ("甲", "丙", "乙", "丁"), {0: 0, 3: 3}),
        (("重复", "甲", "重复"), ("重复", "甲", "重复"), {1: 1}),
        (("甲", "乙"), ("甲", "甲", "乙"), {2: 1}),
        (("", "甲"), ("", "甲"), {1: 1}),
    ],
)
def test_ordered_alignment_never_assumes_split_merge_move_or_duplicate_unchanged(
    old, new, expected
):
    assert unchanged_segments(document(*old), document(*new)) == expected


@pytest.mark.parametrize("reviewer", ["spec_compliance", "citation_alignment", "logic", "linkage"])
@pytest.mark.parametrize("severity", ["blocking", "important"])
def test_only_new_model_findings_in_clean_unchanged_segments_are_downgraded(reviewer, severity):
    original = located_opinion(reviewer=reviewer, severity=severity)
    before = deepcopy(original)
    result, count = converge(document("未改动"), document("未改动"), original)
    assert count == 1
    assert result[0]["severity"] == "suggestion"
    assert result[0]["original_severity"] == severity
    assert result[0]["convergence_note"] == CONVERGENCE_NOTE
    assert original == before


@pytest.mark.parametrize("reviewer", ["format_checker", "citation_database", "unknown"])
def test_objective_and_unknown_reviewers_keep_severity(reviewer):
    original = located_opinion(reviewer=reviewer)
    result, count = converge(document("未改动"), document("未改动"), original)
    assert count == 0 and result == [original]


@pytest.mark.parametrize("severity", ["blocking", "important", "suggestion"])
@pytest.mark.parametrize("reviewer", ["logic", "citation_database", "format_checker"])
def test_any_old_opinion_on_a_segment_prevents_downgrade(severity, reviewer):
    previous = located_opinion(reviewer=reviewer, severity=severity)
    current = located_opinion(problem="全新且无法匹配的不同问题。")
    result, count = converge(
        document("未改动"), document("未改动"), current, prior=[previous], history=[previous]
    )
    assert count == 0 and result[0]["severity"] == "important"


def test_description_only_docx_format_finding_blocks_its_paragraph_without_blocking_clean_other():
    old = opinion(
        reviewer="format_checker",
        location={"description": "正文第 1 段，文字片段 1"},
    )
    doc = document("有旧字体意见", "无旧意见")
    result, count = converge(doc, doc, located_opinion(1), located_opinion(2), prior=[old])
    assert count == 1
    assert [item["severity"] for item in result] == ["important", "suggestion"]


@pytest.mark.parametrize("location", [{"description": "不确定位置"}, {"part": "unknown"}])
def test_unknown_old_location_conservatively_disables_downgrade(location):
    result, count = converge(
        document("相同"), document("相同"), located_opinion(), prior=[opinion(location=location)]
    )
    assert count == 0 and result[0]["severity"] == "important"


def test_scope_location_old_opinion_covers_all_possible_fragments_and_new_scope_is_not_guessed():
    doc = document("甲", "乙")
    scope = opinion(location={"description": "正文", "part": "word/document.xml"})
    assert located_segments(scope, doc) == {0, 1}
    result, count = converge(doc, doc, located_opinion(2), prior=[scope])
    assert count == 0 and result[0]["severity"] == "important"
    assert converge(doc, doc, scope)[1] == 0


def test_earlier_history_match_prevents_downgrade_even_if_previous_round_was_clean():
    current = located_opinion()
    earlier = {**current, "problem": current["problem"].replace("。", "！")}
    result, count = converge(document("相同"), document("相同"), current, history=[earlier])
    assert count == 0 and result[0]["severity"] == "important"


def test_changed_fragment_remains_important_and_unmodified_fragment_can_converge():
    result, count = converge(
        document("旧段", "相同"),
        document("改过的段", "相同"),
        located_opinion(1),
        located_opinion(2),
    )
    assert count == 1
    assert [item["severity"] for item in result] == ["important", "suggestion"]


@pytest.mark.parametrize("old_note,new_note,expected", [("交 接。", "交接.", 1), ("旧", "新", 0)])
def test_handoff_is_compared_independently_of_body_changes(old_note, new_note, expected):
    current = opinion(location={"description": "交接说明", "part": "handoff_note"})
    result, count = converge(
        document("旧正文"), document("新正文"), current, old_handoff=old_note, new_handoff=new_note
    )
    assert count == expected
    assert result[0]["severity"] == ("suggestion" if expected else "important")


def test_server_metadata_is_not_inherited_and_legacy_opinions_keep_original_shape():
    legacy = opinion()
    assert (
        OpinionDraft.model_validate(
            {key: legacy[key] for key in OpinionDraft.model_fields if key in legacy}
        )
        .model_dump(mode="json")
        .get("original_severity")
        is None
    )
    forged = located_opinion(original_severity="blocking", convergence_note="伪造")
    result, count = converge(document("旧"), document("改动"), forged)
    assert count == 0 and "original_severity" not in result[0]
    assert "convergence_note" not in result[0]


@pytest.mark.parametrize(
    "field,value", [("original_severity", "blocking"), ("convergence_note", "伪造")]
)
def test_model_contract_rejects_platform_convergence_fields(field, value):
    forged = compact_finding(**{field: value})
    with pytest.raises(ValidationError):
        CompactFinding.model_validate(forged)
    response = adapt(output(compact_finding(), forged))
    assert len(response.findings) == 1 and response.discarded_count == 1


def docx_bytes(*paragraphs):
    from xml.sax.saxutils import escape

    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            + "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
            + "</w:body></w:document>",
        )
    return stream.getvalue()


def submit(environment, team, *paragraphs, **options):
    response = upload_delivery(
        team.member, team, docx_bytes(*paragraphs), filename="交付.docx", **options
    )
    assert response.status_code == 201, response.text
    return response.json()


def process(environment, team, delivery, data=None):
    claimed = claim(environment, database_time(environment))
    assert claimed is not None and str(claimed.delivery_id) == delivery["id"]
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    storage = LocalStorage(environment.client.app.state.settings.storage_dir)
    value = apply_review_convergence(
        environment.sessions, review, data if data is not None else components(), storage=storage
    )
    reference = reports.stage_report(environment.sessions, review, value)
    with environment.sessions() as db:
        assert complete_job(db, claimed, reference)
        db.commit()
        result = reports.get_report_internal(db, db.get(DeliveryVersion, claimed.delivery_id))
    return result


def test_same_delivery_revision_reaches_final_review_and_preserves_frozen_original(
    auth_environment,
):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "有问题的首段", "未改动的新发现位置")
    old = process(auth_environment, team, first, components(located_opinion(1)))
    assert old["state"] == "returned"
    annotate(
        auth_environment,
        team,
        first,
        old["opinions"][0]["finding_id"],
        handling="adopted",
        reason="已改",
    )
    with auth_environment.sessions() as db:
        original_reports = {
            row.id: deepcopy(row.components) for row in db.scalars(select(ReviewReport))
        }
    second = submit(auth_environment, team, "已修订的首段", "未改动的新发现位置")
    raw = components(located_opinion(2, severity="blocking", problem="此前未提出的新问题。"))
    result = process(auth_environment, team, second, raw)
    assert result["state"] == "pending_final_review" and result["coverage"] == "completed"
    found = result["opinions"][0]["opinion"]
    assert found["severity"] == "suggestion" and found["original_severity"] == "blocking"
    assert found["convergence_note"] == CONVERGENCE_NOTE
    assert result["components"]["reasoning"]["convergence"]["baseline_delivery_id"] == first["id"]
    assert raw["reasoning"]["logic"]["findings"][0]["severity"] == "blocking"
    final = context(auth_environment, team, second)
    assert final["opinions"][0]["opinion"]["original_severity"] == "blocking"
    assert final["opinions"][0]["opinion"]["convergence_note"] == CONVERGENCE_NOTE
    with auth_environment.sessions() as db:
        for identity, snapshot in original_reports.items():
            assert db.get(ReviewReport, identity).components == snapshot
        assert reports.load_review_history(db, db.get(DeliveryVersion, UUID(second["id"])))


@pytest.mark.parametrize("changed", [True, False])
def test_changed_or_previously_flagged_fragments_still_return(auth_environment, changed):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "原来的首段")
    previous = process(auth_environment, team, first, components(located_opinion(1)))
    annotate(
        auth_environment,
        team,
        first,
        previous["opinions"][0]["finding_id"],
        handling="adopted",
        reason="已修改",
    )
    second = submit(auth_environment, team, "改动的首段" if changed else "原来的首段")
    result = process(auth_environment, team, second, components(located_opinion(1)))
    assert (
        result["state"] == "returned"
        and result["opinions"][0]["opinion"]["severity"] == "important"
    )
    assert "original_severity" not in result["opinions"][0]["opinion"]


def test_not_adopted_old_problem_uses_existing_dedup_without_severity_rewrite(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "原段")
    old = process(auth_environment, team, first, components(located_opinion()))
    finding = old["opinions"][0]["finding_id"]
    annotate(auth_environment, team, first, finding)
    second = submit(auth_environment, team, "原段")
    result = process(auth_environment, team, second, components(located_opinion()))
    assert result["state"] == "pending_final_review"
    assert result["opinions"][0]["opinion"]["severity"] == "important"
    assert result["opinions"][0]["duplicate_of"] == finding
    assert "convergence_note" not in result["opinions"][0]["opinion"]


@pytest.mark.parametrize("binding", ["plan", "spec"])
def test_changed_plan_or_spec_disables_convergence(auth_environment, binding):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "未改动")
    process(auth_environment, team, first)
    if binding == "plan":
        team.plan = publish_modules(team, team.modules, "新方案")
    else:
        team.spec = publish_spec(team.leader, team.project["id"])
    second = submit(auth_environment, team, "未改动")
    result = process(auth_environment, team, second, components(located_opinion()))
    assert (
        result["state"] == "returned"
        and result["opinions"][0]["opinion"]["severity"] == "important"
    )
    assert "convergence" not in result["components"]["reasoning"]


def test_first_submission_has_no_convergence(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "首次提交")
    result = process(auth_environment, team, first, components(located_opinion()))
    assert result["state"] == "returned"
    assert "convergence" not in result["components"]["reasoning"]


def test_higher_incomplete_version_is_skipped_and_failed_stage_never_becomes_passed(
    auth_environment,
):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "基线", "未改动")
    process(auth_environment, team, first)
    second = submit(auth_environment, team, "中间版已改", "未改动")
    incomplete = process(auth_environment, team, second, components(failure="reasoning"))
    assert incomplete["coverage"] == "incomplete"
    third = submit(auth_environment, team, "基线", "未改动")
    raw = components(failure="reasoning")
    raw["specification"] = {
        "status": "completed",
        "review": {
            "status": "completed",
            "findings": [located_opinion(2, reviewer="spec_compliance")],
        },
    }
    result = process(auth_environment, team, third, raw)
    assert result["coverage"] == "incomplete" and result["state"] == "ai_review_partial_failure"
    assert result["opinions"][0]["opinion"]["severity"] == "suggestion"
    metadata = result["components"]["reasoning"]["convergence"]
    assert metadata["baseline_delivery_id"] == first["id"]
    assert result["components"]["reasoning"]["status"] == "incomplete"


@pytest.mark.parametrize("missing", [False, True])
def test_untrusted_or_missing_baseline_bytes_never_downgrade(auth_environment, missing):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "基线")
    process(auth_environment, team, first)
    second = submit(auth_environment, team, "基线")
    storage = LocalStorage(auth_environment.client.app.state.settings.storage_dir)
    if missing:
        storage.path(first["sha256"]).unlink()
    else:
        storage.path(first["sha256"]).write_bytes(b"tampered")
    result = process(auth_environment, team, second, components(located_opinion()))
    assert (
        result["state"] == "returned"
        and result["opinions"][0]["opinion"]["severity"] == "important"
    )
    assert "convergence" not in result["components"]["reasoning"]


def test_objective_format_and_database_findings_stay_blocking(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team, "甲", "乙")
    process(auth_environment, team, first)
    second = submit(auth_environment, team, "甲", "乙")
    raw = components(located_opinion(2))
    raw["format"].update(
        status="issues",
        checker_version="fixed",
        sha256=second["sha256"],
        findings=[
            {
                "blocking": True,
                "location": "正文第 1 段，文字片段 1",
                "message": "字体不符合规范",
                "spec_number": "F1",
                "requirement": "宋体",
            }
        ],
    )
    raw["citations"]["database_findings"] = [
        {
            **located_opinion(
                1, category="citation", severity="blocking", reviewer="citation_database"
            ),
            "source": "program",
        }
    ]
    result = process(auth_environment, team, second, raw)
    assert result["state"] == "returned"
    assert {
        item["opinion"]["reviewer"]: item["opinion"]["severity"] for item in result["opinions"]
    } == {
        "format_checker": "blocking",
        "citation_database": "blocking",
        "logic": "suggestion",
    }
