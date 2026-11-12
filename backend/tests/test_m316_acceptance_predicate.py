"""验收判定遵守14.9可选上下文；只用固定响应，绝不调用真实模型。"""

import importlib.util
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
