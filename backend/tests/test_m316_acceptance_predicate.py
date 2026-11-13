"""验收判定遵守14.9可选上下文；只用固定响应，绝不调用真实模型。"""

import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "m316_driver", Path(__file__).parents[2] / "scripts/accept_m3.py"
)
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)


def fixture():
    return {
        "alignment": {
            "status": "completed",
            "findings": [
                {
                    "severity": "important",
                    "location": {"paragraph": 3},
                    "basis": {
                        "excerpt_verified": True,
                        "excerpt": "尚未收集任何样本，我们已经证实。",
                    },
                }
            ],
        },
        "issue_links": [{"kind": "support_insufficient", "finding_index": 0, "reference_id": None}],
    }


def test_preseed_hit_does_not_require_optional_reference_context():
    value = fixture()
    assert driver.citation_alignment_preseed_hit(value)
    with_context = deepcopy(value)
    with_context["issue_links"][0]["reference_id"] = "R1"
    assert driver.citation_alignment_preseed_hit(with_context)


@pytest.mark.parametrize(
    "path,value",
    [
        (("alignment", "status"), "incomplete"),
        (("alignment", "findings", 0, "severity"), "suggestion"),
        (("alignment", "findings", 0, "location", "paragraph"), 5),
        (("alignment", "findings", 0, "basis", "excerpt_verified"), False),
        (("issue_links", 0, "kind"), "missing_reference"),
        (("issue_links", 0, "finding_index"), 100),
    ],
)
def test_other_issues_unverified_or_incomplete_do_not_count(path, value):
    sample = fixture()
    node = sample
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert not driver.citation_alignment_preseed_hit(sample)


def test_mock_reviewer_cli_hits_every_preseed_and_cleans_up(tmp_path):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")
    backend = Path(__file__).parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(backend.parent / "scripts/accept_m3.py"),
            "--mode",
            "mock",
            "--layer",
            "reviewers",
            "--database-url",
            database_url,
            "--output-dir",
            str(tmp_path / "evidence"),
        ],
        cwd=backend,
        env={**os.environ, "DEEPSEEK_API_KEY": "", "LLM_ENABLE_LIVE_CALLS": "false"},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    report = json.loads(next((tmp_path / "evidence").glob("run-*/report.json")).read_text())
    assert result.returncode == 0, report.get("reviewer_results", report.get("failure"))
    assert report["success"] and all(report["cleanup"].values())
    assert report["real_model_network_calls"] == report["real_bibliography_network_requests"] == 0
    assert set(report["reviewer_results"]) == set(driver.REVIEWERS)
    for row in report["reviewer_results"].values():
        assert row["passed"] and row["successes"] == row["model_calls"] == 2
        assert all(trial["preseed_hit"] and trial["structurally_valid"] for trial in row["trials"])
