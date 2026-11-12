"""M3-16 ¥10 预算替身验证：Decimal边界、未知预留和独立进程竞争。"""

import importlib.util
import json
import multiprocessing
import os
import stat
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

MODULE_PATH = Path(__file__).parents[2] / "scripts" / "accept_m3_budget.py"
_spec = importlib.util.spec_from_file_location("accept_m3_budget_test_module", MODULE_PATH)
budget_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget_module)
RevisionBudget = budget_module.RevisionBudget
BudgetExceeded = budget_module.BudgetExceeded
BudgetStateError = budget_module.BudgetStateError
MESSAGES = [{"role": "user", "content": "中文ＡＢＣ test"}]


def profile(*, miss="0", output="1000000", maximum=1):
    return SimpleNamespace(
        model="fixed-test-model",
        max_tokens=maximum,
        prices=SimpleNamespace(input_cache_miss=Decimal(miss), output=Decimal(output)),
    )


def _concurrent_reserve(arguments):
    path, identity = arguments
    try:
        RevisionBudget(path).reserve(identity, "logic", MESSAGES, profile())
        return "reserved"
    except BudgetExceeded:
        return "denied"


def test_new_revision_starts_with_no_historical_cost_or_secret_access(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Acceptance budget must never read keys or environment")

    monkeypatch.setattr(os, "getenv", forbidden)
    result = RevisionBudget(tmp_path / "revision" / "ledger.json").summary()
    assert result == {
        "revision": "M3-16",
        "currency": "CNY",
        "limit_cny": "10",
        "call_count": 0,
        "settled_calls": 0,
        "unknown_cost_calls": 0,
        "known_estimated_cost_cny": "0",
        "reserved_unknown_cost_cny": "0",
        "conservative_total_cny": "0",
        "remaining_cny": "10",
    }


def test_reserve_uses_utf8_byte_bound_all_input_miss_and_maximum_output(tmp_path):
    path = tmp_path / "revision" / "ledger.json"
    identity = uuid4()
    actual_profile = profile(miss="0.125", output="27.0", maximum=16384)
    expected = (
        (len(json.dumps(MESSAGES, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 1024)
        * Decimal("0.125")
        + 16384 * Decimal("27.0")
    ) / Decimal("1000000")
    result = RevisionBudget(path).reserve(identity, "logic", MESSAGES, actual_profile)
    assert Decimal(result["conservative_total_cny"]) == expected
    assert Decimal(result["reserved_unknown_cost_cny"]) == expected
    assert result["unknown_cost_calls"] == 1
    assert RevisionBudget(path).summary() == result
    raw = path.read_text()
    assert "中文" not in raw and "fixed-test-model" not in raw and "DEEPSEEK" not in raw
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_suffix(".json.lock").stat().st_mode) == 0o600


def test_exact_ten_boundary_is_allowed_and_next_call_denied_without_ledger_mutation(tmp_path):
    path = tmp_path / "budget" / "ledger.json"
    budget = RevisionBudget(path)
    for _ in range(10):
        result = budget.reserve(uuid4(), "logic", [], profile())
    assert result["conservative_total_cny"] == "10" and result["remaining_cny"] == "0"
    before = path.read_bytes()
    with pytest.raises(BudgetExceeded):
        budget.reserve(uuid4(), "logic", [], profile(output="0.000001"))
    with pytest.raises(BudgetExceeded):
        budget.reserve(uuid4(), "logic", [], profile(output="1e-100"))
    assert path.read_bytes() == before


def test_decimal_fraction_does_not_round_into_false_budget_space(tmp_path):
    budget = RevisionBudget(tmp_path / "budget" / "ledger.json")
    fractional = profile(output="333333.333333333333333333")
    for _ in range(30):
        budget.reserve(uuid4(), "logic", [], fractional)
    result = budget.summary()
    assert Decimal(result["conservative_total_cny"]) == Decimal("9.99999999999999999999999")
    with pytest.raises(BudgetExceeded):
        budget.reserve(uuid4(), "logic", [], profile(output="0.000000000001"))


def test_settle_releases_only_known_estimate_and_unknown_cost_keeps_full_reservation(tmp_path):
    path = tmp_path / "budget" / "ledger.json"
    first, second = uuid4(), uuid4()
    budget = RevisionBudget(path)
    budget.reserve(first, "logic", [], profile(output="4000000"))
    budget.reserve(second, "linkage", [], profile(output="5000000"))
    before = path.read_bytes()
    result = budget.settle(first, None)
    assert path.read_bytes() == before
    assert result["unknown_cost_calls"] == 2 and result["conservative_total_cny"] == "9"
    result = RevisionBudget(path).settle(first, Decimal("0.23"))
    assert result["known_estimated_cost_cny"] == "0.23"
    assert result["reserved_unknown_cost_cny"] == "5"
    assert result["conservative_total_cny"] == "5.23"
    assert result["settled_calls"] == result["unknown_cost_calls"] == 1
    assert budget.settle(first, None) == result
    assert budget.settle(first, Decimal("0.23")) == result
    with pytest.raises(BudgetStateError):
        budget.settle(first, Decimal("0.24"))


def test_duplicate_reserve_is_idempotent_and_cannot_replace_original_request(tmp_path):
    budget = RevisionBudget(tmp_path / "budget" / "ledger.json")
    identity = uuid4()
    first = budget.reserve(identity, "logic", MESSAGES, profile())
    assert budget.reserve(str(identity).upper(), "logic", MESSAGES, profile()) == first
    with pytest.raises(BudgetStateError):
        budget.reserve(identity, "linkage", MESSAGES, profile())
    with pytest.raises(BudgetStateError):
        budget.reserve(identity, "logic", [], profile())
    with pytest.raises(BudgetStateError):
        budget.reserve(identity, "logic", MESSAGES, profile(maximum=2))
    budget.settle(identity, Decimal("0.3"))
    assert budget.reserve(identity, "logic", MESSAGES, profile())["conservative_total_cny"] == "0.3"
    assert budget.summary()["call_count"] == 1


def test_actual_cost_overrun_is_persisted_and_blocks_all_future_calls(tmp_path):
    path = tmp_path / "budget" / "ledger.json"
    identity = uuid4()
    budget = RevisionBudget(path)
    budget.reserve(identity, "logic", [], profile())
    with pytest.raises(BudgetExceeded):
        budget.settle(identity, Decimal("10.000001"))
    assert RevisionBudget(path).summary()["conservative_total_cny"] == "10.000001"
    with pytest.raises(BudgetExceeded):
        budget.reserve(uuid4(), "logic", [], profile())


@pytest.mark.parametrize(
    "amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), True, 0.1, "bad"]
)
def test_invalid_estimate_cannot_corrupt_ledger_or_release_reserved_fee(tmp_path, amount):
    budget = RevisionBudget(tmp_path / "budget" / "ledger.json")
    identity = uuid4()
    before = budget.reserve(identity, "logic", [], profile())
    with pytest.raises(BudgetStateError):
        budget.settle(identity, amount)
    assert budget.summary() == before
    with pytest.raises(BudgetStateError):
        budget.settle(uuid4(), Decimal(0))


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        '{"revision":"M3-14"}',
        '{"revision":"M3-16","currency":"CNY","limit_cny":"100","calls":{}}',
    ],
)
def test_invalid_or_different_revision_ledger_fails_closed(tmp_path, contents):
    path = tmp_path / "budget" / "ledger.json"
    path.parent.mkdir()
    path.write_text(contents)
    with pytest.raises(BudgetStateError):
        RevisionBudget(path).reserve(uuid4(), "logic", [], profile())
    assert path.read_text() == contents


def test_symlinked_ledger_and_lock_are_never_followed(tmp_path):
    private = tmp_path / "private.json"
    private.write_text("do not read or change")
    path = tmp_path / "budget" / "ledger.json"
    path.parent.mkdir()
    path.symlink_to(private)
    with pytest.raises(BudgetStateError):
        RevisionBudget(path).summary()
    path.unlink()
    path.with_suffix(".json.lock").unlink()
    path.with_suffix(".json.lock").symlink_to(private)
    with pytest.raises(BudgetStateError):
        RevisionBudget(path).reserve(uuid4(), "logic", [], profile())
    assert private.read_text() == "do not read or change"


def test_atomic_write_failure_preserves_prior_ledger_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "budget" / "ledger.json"
    budget = RevisionBudget(path)
    budget.reserve(uuid4(), "logic", [], profile())
    before = path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("substitute atomic rename failure")

    monkeypatch.setattr(budget_module.os, "replace", fail_replace)
    with pytest.raises(BudgetStateError):
        budget.reserve(uuid4(), "logic", [], profile())
    assert path.read_bytes() == before
    assert sorted(item.name for item in path.parent.iterdir()) == [
        "ledger.json",
        "ledger.json.lock",
    ]


def test_independent_process_reservations_cannot_race_past_ten_yuan(tmp_path):
    path = tmp_path / "budget" / "ledger.json"
    tasks = [(str(path), str(uuid4())) for _ in range(18)]
    with ProcessPoolExecutor(
        max_workers=6, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        results = list(pool.map(_concurrent_reserve, tasks))
    assert results.count("reserved") == 10 and results.count("denied") == 8
    summary = RevisionBudget(path).summary()
    assert summary["call_count"] == 10 and summary["conservative_total_cny"] == "10"
