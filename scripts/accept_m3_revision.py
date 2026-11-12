"""M3-16双层验收：默认替身演练，live显式执行一次单测层及一次端到端层。"""

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BUDGET_PATH = ROOT / ".verification/M3-16-live-budget.json"
QUALITY_FAILURES = frozenset(
    {
        "first_review_not_completed_and_returned",
        "first_review_did_not_identify_all_four_expected_problems",
        "revised_delivery_did_not_reach_final_review",
        "recovery_did_not_complete",
    }
)
BUDGET_FAILURES = frozenset(
    {"revision_10_cny_budget", "BudgetExceeded", "budget_stopped", "run_token_budget_exceeded"}
)
MAX_REPORT_BYTES = 4 * 1024 * 1024


def _code(value):
    # 只转发会据以采取动作的固定代码；完整失败报告保存在子进程原文件。
    return (
        value
        if isinstance(value, str) and value in QUALITY_FAILURES | BUDGET_FAILURES
        else "child_failure_details_withheld"
    )


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("mock", "live"), default="mock")
    result.add_argument("--database-url", required=True)
    result.add_argument("--key-file", type=Path, help="Live only: pass platform/.env path to child")
    result.add_argument("--output-dir", type=Path, default=ROOT / ".verification/M3-16-revision")
    return result


def _read_report(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
                return None
            data = json.loads(source.read(MAX_REPORT_BYTES + 1))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _budget_exhausted(report):
    failure = report.get("failure") or {}
    if isinstance(failure, dict) and (
        failure.get("code") in BUDGET_FAILURES or failure.get("type") == "BudgetExceeded"
    ):
        return True
    budget = report.get("revision_budget")
    if isinstance(budget, dict):
        try:
            total, limit = Decimal(budget["conservative_total_cny"]), Decimal(budget["limit_cny"])
            return (
                not total.is_finite()
                or not limit.is_finite()
                or limit != 10
                or total < 0
                or total >= limit
            )
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return True
    return False


def _run_layer(arguments, output, name, *, mode, layer):
    destination = output / name
    destination.mkdir(mode=0o700)
    command = [
        sys.executable,
        str(ROOT / "scripts/accept_m3.py"),
        "--mode",
        mode,
        "--layer",
        layer,
        "--database-url",
        arguments.database_url,
        "--output-dir",
        str(destination),
    ]
    if mode == "live":
        command.extend(("--key-file", str(arguments.key_file)))
    # 原始厂商输出/凭据绝不能通过子进程日志进入本编排器的控制台或报告。
    try:
        child = subprocess.run(
            command,
            cwd=BACKEND,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        exit_code = child.returncode
    except (OSError, ValueError):
        exit_code = None
    # destination是本次编排独占的新目录，禁止误取历史成功报告。
    paths = sorted(destination.glob("run-*/report.json"))
    report = _read_report(paths[0]) if len(paths) == 1 else None
    valid = bool(report and report.get("mode") == mode and report.get("layer") == layer)
    failure = (report.get("failure") or {}) if valid else {}
    failure_code = _code(failure.get("code")) if isinstance(failure, dict) and failure else None
    return {
        "mode": mode,
        "layer": layer,
        "exit_code": exit_code,
        "report_path": str(paths[0]) if len(paths) == 1 else None,
        "report_valid": valid,
        "passed": bool(valid and exit_code == 0 and report.get("success") is True),
        "failure_code": failure_code if valid else "child_report_missing_or_invalid",
        "budget_stopped": bool(valid and _budget_exhausted(report)),
    }


def main(argv=None):
    arguments = parser().parse_args(argv)
    if Path.cwd() != BACKEND:
        parser().error("run_from_platform_backend")
    if arguments.mode == "mock" and arguments.key_file is not None:
        parser().error("mock_never_reads_key_files")
    if arguments.mode == "live" and (
        arguments.key_file is None or not arguments.key_file.is_absolute()
    ):
        parser().error("live_requires_absolute_key_file_path")
    output = (
        arguments.output_dir.resolve() / f"run-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    )
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    summary = {
        "revision": "M3-16",
        "mode": arguments.mode,
        "started_utc": datetime.now(UTC).isoformat(),
        "budget_ledger": str(BUDGET_PATH) if arguments.mode == "live" else None,
        "reviewer_layer_passed": False,
        "real_e2e_passed": False,
        "mock_fallback_passed": False,
        "mock_reviewer_layer_passed": False,
        "mock_e2e_passed": False,
        "workflow_validated": False,
        "success": False,
        "layers": [],
    }
    reviewers = _run_layer(arguments, output, "reviewers", mode=arguments.mode, layer="reviewers")
    summary["layers"].append(reviewers)
    summary["reviewer_layer_passed"] = reviewers["passed"]
    if arguments.mode == "mock":
        summary["mock_reviewer_layer_passed"] = reviewers["passed"]
    if reviewers["budget_stopped"]:
        summary["stopped"] = "revision_10_cny_budget"
    elif not reviewers["report_valid"]:
        summary["stopped"] = "reviewer_report_unavailable_no_further_live_calls"
    else:
        e2e = _run_layer(arguments, output, "e2e", mode=arguments.mode, layer="e2e")
        summary["layers"].append(e2e)
        if arguments.mode == "live":
            summary["real_e2e_passed"] = e2e["passed"]
            if e2e["budget_stopped"]:
                summary["stopped"] = "revision_10_cny_budget"
            elif not e2e["passed"] and e2e["failure_code"] in QUALITY_FAILURES:
                fallback = _run_layer(arguments, output, "mock-fallback", mode="mock", layer="e2e")
                summary["layers"].append(fallback)
                summary["mock_fallback_passed"] = fallback["passed"]
        else:
            summary["mock_e2e_passed"] = e2e["passed"]
    summary["workflow_validated"] = (
        summary["real_e2e_passed"] or summary["mock_fallback_passed"] or summary["mock_e2e_passed"]
    )
    summary["success"] = summary["reviewer_layer_passed"] and (
        summary["real_e2e_passed"] if arguments.mode == "live" else summary["mock_e2e_passed"]
    )
    summary["finished_utc"] = datetime.now(UTC).isoformat()
    path = output / "report.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(
        json.dumps(
            {
                "mode": arguments.mode,
                "reviewer_layer_passed": summary["reviewer_layer_passed"],
                "real_e2e_passed": summary["real_e2e_passed"],
                "mock_fallback_passed": summary["mock_fallback_passed"],
                "success": summary["success"],
                "evidence": str(path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("FAIL revision_orchestration_failed_details_withheld", file=sys.stderr)
        raise SystemExit(2) from None
