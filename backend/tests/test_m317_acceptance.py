"""14.10.6验收判据与显式手动修订输入：固定响应，无真实调用。"""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "m317_acceptance", Path(__file__).parents[2] / "scripts/accept_m3.py"
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def test_recovery_completed_accepts_review_returned_for_important_findings():
    assert driver.recovery_completed(
        {
            "coverage": "completed",
            "review_status": "returned",
            "opinions": [{"severity": "important"}],
        }
    )
    assert driver.recovery_completed(
        {"coverage": "completed", "review_status": "pending_final_review"}
    )


@pytest.mark.parametrize("coverage", ["incomplete", "bypassed", "not_covered", None])
def test_incomplete_or_bypassed_is_not_recovery_success(coverage):
    assert not driver.recovery_completed({"coverage": coverage})


def test_revision_input_is_data_with_bounded_required_fields(tmp_path):
    p = tmp_path / "revision.json"
    data = {
        "paragraphs": ["按意见修改的合成正文。"],
        "handoff_note": "交接说明。",
        "change_note": "修正术语与论证。",
    }
    p.write_text(json.dumps(data))
    assert driver.revision_file(p) == data
    data["paragraphs"] = ["\0"]
    p.write_text(json.dumps(data))
    with pytest.raises(driver.AcceptanceFailure):
        driver.revision_file(p)


def test_m317_cli_is_explicit_and_shares_no_new_budget_option():
    args = driver.parser().parse_args(["--database-url", "fixed-test", "--milestone", "M3-17"])
    assert args.mode == "mock" and args.milestone == "M3-17" and args.layer == "e2e"
    assert not hasattr(args, "budget_path")


def recovery_source(tmp_path):
    evidence = tmp_path / ".verification/failed-e2e"
    evidence.mkdir(parents=True)
    marker = {
        "revision": "M3-17",
        "layer": "e2e",
        "evidence": str(evidence),
    }
    (evidence.parent / "M3-17-live-e2e-started.json").write_text(json.dumps(marker))
    report = {
        "revision": "M3-17",
        "layer": "e2e",
        "mode": "live",
        "success": False,
        "failure": {"code": "revised_delivery_did_not_reach_final_review"},
        "real_model_network_calls": 10,
        "cleanup": {"schema_removed": True, "http_stopped": True, "storage_removed": True},
        "steps": [],
    }
    return evidence, report


def test_recovery_continuation_links_failed_e2e_without_rewriting_it(tmp_path):
    import hashlib

    evidence, report = recovery_source(tmp_path)
    path = evidence / "report.json"
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    result = driver.failed_e2e_for_recovery(tmp_path)
    assert result == {"failed_e2e_report": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    assert path.read_bytes() == raw


@pytest.mark.parametrize("invalid", ["passed", "free_preflight", "recovery_executed", "unclean"])
def test_recovery_continuation_cannot_rerun_completed_or_unrelated_work(tmp_path, invalid):
    evidence, report = recovery_source(tmp_path)
    if invalid == "passed":
        report["success"] = True
    elif invalid == "free_preflight":
        report["real_model_network_calls"] = 0
    elif invalid == "recovery_executed":
        (evidence / "disconnected-review.json").write_text("{}")
    else:
        report["cleanup"]["http_stopped"] = False
    (evidence / "report.json").write_text(json.dumps(report))
    with pytest.raises(driver.AcceptanceFailure):
        driver.failed_e2e_for_recovery(tmp_path)


def test_recovery_continuation_requires_original_marker(tmp_path):
    with pytest.raises(driver.AcceptanceFailure):
        driver.failed_e2e_for_recovery(tmp_path)


def test_recovery_continuation_rejects_marker_outside_verification(tmp_path):
    evidence, _ = recovery_source(tmp_path)
    marker = evidence.parent / "M3-17-live-e2e-started.json"
    marker.write_text(json.dumps({"revision": "M3-17", "layer": "e2e", "evidence": str(tmp_path)}))
    with pytest.raises(driver.AcceptanceFailure):
        driver.failed_e2e_for_recovery(tmp_path)
