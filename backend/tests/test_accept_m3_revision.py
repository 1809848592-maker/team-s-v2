"""双层编排只使用subprocess替身，不读取.env、不调用模型或数据库。"""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).parents[2] / "scripts/accept_m3_revision.py"
_spec = importlib.util.spec_from_file_location("accept_m3_revision_tests", MODULE_PATH)
revision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(revision)
PRIVATE = "secret-model-key-never-visible-from-child"
DATABASE = "postgresql+psycopg://fixed-test-db"


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.chdir(revision.BACKEND)
    calls, outputs = [], []

    def install(*responses):
        prepared = iter(responses)

        def run(command, **options):
            assert options == {
                "cwd": revision.BACKEND,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "check": False,
            }
            calls.append(command)
            response = next(prepared)
            if isinstance(response, Exception):
                raise response
            destination = Path(command[command.index("--output-dir") + 1])
            mode = command[command.index("--mode") + 1]
            layer = command[command.index("--layer") + 1]
            if response is not None:
                folder = destination / "run-fixed-child"
                folder.mkdir()
                report = {
                    "mode": mode,
                    "layer": layer,
                    "success": response.get("exit_code", 0) == 0,
                    **{key: value for key, value in response.items() if key != "exit_code"},
                }
                (folder / "report.json").write_text(json.dumps(report))
                outputs.append(report)
            return SimpleNamespace(
                returncode=response.get("exit_code", 0) if response is not None else 2
            )

        monkeypatch.setattr(revision.subprocess, "run", run)

    def invoke(mode="mock", *extra):
        arguments = ["--database-url", DATABASE, "--output-dir", str(tmp_path / "evidence")]
        if mode != "default":
            arguments.extend(("--mode", mode))
        if mode == "live":
            arguments.extend(("--key-file", str(revision.ROOT / ".env")))
        result = revision.main([*arguments, *extra])
        reports = list((tmp_path / "evidence").glob("run-*/report.json"))
        assert len(reports) == 1
        return result, json.loads(reports[0].read_text())

    return SimpleNamespace(install=install, invoke=invoke, calls=calls, outputs=outputs)


def command_pairs(calls):
    return [(call[call.index("--mode") + 1], call[call.index("--layer") + 1]) for call in calls]


def test_default_dry_run_uses_two_mock_layers_without_key_or_live_claims(driver):
    driver.install({}, {})
    code, report = driver.invoke("default")
    assert code == 0 and report["success"]
    assert command_pairs(driver.calls) == [("mock", "reviewers"), ("mock", "e2e")]
    assert all("--key-file" not in call for call in driver.calls)
    assert report["reviewer_layer_passed"] and report["mock_reviewer_layer_passed"]
    assert report["mock_e2e_passed"] and report["workflow_validated"]
    assert report["real_e2e_passed"] is report["mock_fallback_passed"] is False
    assert report["budget_ledger"] is None


def test_live_layers_each_run_once_with_shared_budget_and_only_key_file_path(driver, monkeypatch):
    original_open = revision.os.open

    def forbid_env(path, *args, **kwargs):
        assert Path(path).name != ".env", "Orchestrator must never read the model key"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(revision.os, "open", forbid_env)
    driver.install({}, {})
    code, report = driver.invoke("live")
    assert code == 0 and report["reviewer_layer_passed"] and report["real_e2e_passed"]
    assert report["mock_fallback_passed"] is False
    assert report["budget_ledger"] == str(revision.BUDGET_PATH)
    assert command_pairs(driver.calls) == [("live", "reviewers"), ("live", "e2e")]
    for command in driver.calls:
        assert command[command.index("--key-file") + 1] == str(revision.ROOT / ".env")
        assert PRIVATE not in " ".join(command)


@pytest.mark.parametrize("failure", sorted(revision.QUALITY_FAILURES))
def test_real_quality_failure_is_preserved_and_only_one_mock_e2e_fallback(driver, failure):
    driver.install({}, {"exit_code": 1, "failure": {"code": failure}}, {})
    code, report = driver.invoke("live")
    assert code == 1 and report["success"] is False
    assert report["reviewer_layer_passed"] and report["mock_fallback_passed"]
    assert report["real_e2e_passed"] is False and report["workflow_validated"] is True
    assert report["layers"][1]["failure_code"] == failure
    assert report["layers"][1]["exit_code"] == 1
    assert command_pairs(driver.calls) == [("live", "reviewers"), ("live", "e2e"), ("mock", "e2e")]
    assert "--key-file" not in driver.calls[-1]
    # 非零退出的本次真实报告仍在独立目录内原样保留。
    raw_report = Path(report["layers"][1]["report_path"])
    assert json.loads(raw_report.read_text())["failure"]["code"] == failure


def test_failed_mock_fallback_does_not_trigger_more_calls_or_claim_workflow_correct(driver):
    driver.install(
        {},
        {"exit_code": 1, "failure": {"code": "recovery_did_not_complete"}},
        {"exit_code": 1, "failure": {"code": "fixed_mock_failure"}},
    )
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 3
    assert not report["workflow_validated"] and not report["mock_fallback_passed"]


@pytest.mark.parametrize("failure", ["revision_10_cny_budget", "run_token_budget_exceeded"])
def test_reviewer_budget_stop_prevents_any_subsequent_real_call(driver, failure):
    driver.install({"exit_code": 1, "failure": {"code": failure}})
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 1
    assert report["stopped"] == "revision_10_cny_budget"
    assert not report["real_e2e_passed"] and not report["mock_fallback_passed"]


def test_exhausted_known_or_unknown_reservation_stops_before_second_live_layer(driver):
    driver.install({"revision_budget": {"conservative_total_cny": "10", "limit_cny": "10"}})
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 1 and report["reviewer_layer_passed"]
    assert report["stopped"] == "revision_10_cny_budget"


def test_reviewer_quality_failure_runs_independent_e2e_but_does_not_pass_reviewers(driver):
    driver.install({"exit_code": 1, "failure": {"code": "reviewer_quality_failed"}}, {})
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 2
    assert report["reviewer_layer_passed"] is False and report["real_e2e_passed"] is True
    assert report["workflow_validated"] and not report["success"]


@pytest.mark.parametrize("response", [None, OSError(PRIVATE)])
def test_child_launch_or_report_failure_is_safe_and_does_not_start_more_live_calls(
    driver, response, capsys
):
    driver.install(response)
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 1
    assert report["stopped"] == "reviewer_report_unavailable_no_further_live_calls"
    assert PRIVATE not in capsys.readouterr().out + json.dumps(report)


def test_nonquality_e2e_failure_is_not_retried_live_or_mock(driver):
    driver.install({}, {"exit_code": 1, "failure": {"code": "admin_usage_does_not_reconcile"}})
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 2
    assert report["mock_fallback_passed"] is False


def test_unexpected_private_child_error_is_not_copied_to_summary_or_console(driver, capsys):
    driver.install({}, {"exit_code": 1, "failure": {"code": PRIVATE}})
    code, report = driver.invoke("live")
    assert code == 1 and len(driver.calls) == 2
    assert report["layers"][1]["failure_code"] == "child_failure_details_withheld"
    assert PRIVATE not in capsys.readouterr().out + json.dumps(report)


def test_reused_output_root_cannot_pick_an_older_success_report(driver, tmp_path):
    stale = tmp_path / "evidence/run-old/reviewers/run-old"
    stale.mkdir(parents=True)
    (stale / "report.json").write_text('{"mode":"live","layer":"reviewers","success":true}')
    driver.install(None)
    assert (
        revision.main(
            [
                "--mode",
                "live",
                "--key-file",
                str(revision.ROOT / ".env"),
                "--database-url",
                DATABASE,
                "--output-dir",
                str(tmp_path / "evidence"),
            ]
        )
        == 1
    )
    assert len(driver.calls) == 1
    reports = [path for path in (tmp_path / "evidence").glob("run-*/report.json")]
    assert (
        len(reports) == 1 and json.loads(reports[0].read_text())["reviewer_layer_passed"] is False
    )


def test_mismatched_report_and_nonzero_success_claim_cannot_pass(driver):
    driver.install({"mode": "mock", "success": True})
    code, report = driver.invoke("live")
    assert code == 1 and not report["reviewer_layer_passed"] and len(driver.calls) == 1


def test_child_nonzero_exit_does_not_pass_despite_report_success_true(driver):
    driver.install({"success": True, "exit_code": 1}, {})
    code, report = driver.invoke("live")
    assert code == 1 and not report["reviewer_layer_passed"]


def test_rejects_wrong_cwd_and_key_in_mock_without_starting_child(driver, tmp_path, monkeypatch):
    driver.install()
    with pytest.raises(SystemExit):
        revision.main(["--database-url", DATABASE, "--key-file", "/unused/.env"])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        revision.main(["--database-url", DATABASE])
    assert driver.calls == []


@pytest.mark.parametrize("key_args", [[], ["--key-file", "relative/.env"]])
def test_live_requires_explicit_absolute_key_path(driver, key_args):
    driver.install()
    with pytest.raises(SystemExit):
        revision.main(["--database-url", DATABASE, "--mode", "live", *key_args])
    assert driver.calls == []
