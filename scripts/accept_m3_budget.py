"""M3-16 修订后真实验收的跨进程 Decimal 费用预留账本，不读取模型密钥。"""

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from uuid import UUID, uuid4

REVISION = "M3-16"
LIMIT_CNY = Decimal("10")
MILLION = Decimal("1000000")
MAX_LEDGER_BYTES = 4 * 1024 * 1024


class BudgetExceeded(RuntimeError):
    def __init__(self):
        super().__init__("M3-16 新增真实验收估算费用将超出 ¥10，已停止追加调用。")


class BudgetStateError(RuntimeError):
    def __init__(self):
        super().__init__("M3-16 费用账本无效或调用状态冲突，未追加调用。")


def _money(value):
    if not isinstance(value, (Decimal, str, int)) or isinstance(value, bool):
        raise BudgetStateError
    try:
        result = Decimal(value)
    except (ValueError, InvalidOperation):
        raise BudgetStateError from None
    if not result.is_finite() or result < 0:
        raise BudgetStateError
    return result


def _identity(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise BudgetStateError from None


def _amount(value):
    return format(value, "f")


def _exact_sum(values):
    values = tuple(values)
    nonzero = [value for value in values if value]
    if not nonzero:
        return Decimal(0)
    precision = (
        max(value.adjusted() for value in nonzero)
        - min(value.as_tuple().exponent for value in nonzero)
        + len(str(len(nonzero)))
        + 3
    )
    with localcontext() as context:
        context.prec = max(28, precision)
        return sum(values, Decimal(0))


def _token_cost(tokens, price):
    with localcontext() as context:
        context.prec = max(28, len(price.as_tuple().digits) + len(str(tokens)) + 3)
        return Decimal(tokens) * price / MILLION


class RevisionBudget:
    """单一文件覆盖本修订所有进程；未知费用始终保留最大调用费用。"""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.name in {"", ".", ".."}:
            raise BudgetStateError

    @contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(directory, 0o700)
                descriptor = os.open(
                    self.path.name + ".lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise BudgetStateError
                    os.fchmod(descriptor, 0o600)
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    yield directory
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError, UnicodeError):
            raise BudgetStateError from None

    def _read(self, directory):
        try:
            descriptor = os.open(
                self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return {"revision": REVISION, "currency": "CNY", "limit_cny": "10", "calls": {}}
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LEDGER_BYTES:
                raise BudgetStateError
            os.fchmod(stream.fileno(), 0o600)
            try:
                ledger = json.loads(stream.read(MAX_LEDGER_BYTES + 1))
            except (ValueError, UnicodeError, RecursionError):
                raise BudgetStateError from None
        if (
            not isinstance(ledger, dict)
            or set(ledger) != {"revision", "currency", "limit_cny", "calls"}
            or ledger["revision"] != REVISION
            or ledger["currency"] != "CNY"
            or ledger["limit_cny"] != "10"
            or not isinstance(ledger["calls"], dict)
        ):
            raise BudgetStateError
        for identity, item in ledger["calls"].items():
            if identity != _identity(identity) or not isinstance(item, dict):
                raise BudgetStateError
            if set(item) != {"reviewer", "fingerprint", "reserved_cost_cny", "estimated_cost_cny"}:
                raise BudgetStateError
            if not isinstance(item["reviewer"], str) or not re.fullmatch(
                r"[a-z][a-z0-9_]{0,63}", item["reviewer"]
            ):
                raise BudgetStateError
            if not isinstance(item["fingerprint"], str) or not re.fullmatch(
                r"[a-f0-9]{64}", item["fingerprint"]
            ):
                raise BudgetStateError
            _money(item["reserved_cost_cny"])
            if item["estimated_cost_cny"] is not None:
                _money(item["estimated_cost_cny"])
        return ledger

    def _write(self, directory, ledger):
        data = json.dumps(ledger, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
        if len(data) > MAX_LEDGER_BYTES:
            raise BudgetStateError
        temporary = f".{self.path.name}.{uuid4()}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @staticmethod
    def _summary(ledger):
        known_amounts, reserved_amounts = [], []
        settled = 0
        for item in ledger["calls"].values():
            if item["estimated_cost_cny"] is None:
                reserved_amounts.append(_money(item["reserved_cost_cny"]))
            else:
                known_amounts.append(_money(item["estimated_cost_cny"]))
                settled += 1
        known, reserved = _exact_sum(known_amounts), _exact_sum(reserved_amounts)
        total = _exact_sum((known, reserved))
        return {
            "revision": REVISION,
            "currency": "CNY",
            "limit_cny": "10",
            "call_count": len(ledger["calls"]),
            "settled_calls": settled,
            "unknown_cost_calls": len(ledger["calls"]) - settled,
            "known_estimated_cost_cny": _amount(known),
            "reserved_unknown_cost_cny": _amount(reserved),
            "conservative_total_cny": _amount(total),
            "remaining_cny": _amount(max(Decimal(0), _exact_sum((LIMIT_CNY, total.copy_negate())))),
        }

    def reserve(self, call_id, reviewer, messages, profile):
        identity = _identity(call_id)
        if not isinstance(reviewer, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reviewer):
            raise BudgetStateError
        if not isinstance(messages, list):
            raise BudgetStateError
        try:
            encoded = json.dumps(messages, ensure_ascii=False, allow_nan=False).encode("utf-8")
            maximum = profile.max_tokens
            if type(maximum) is not int or maximum < 1:
                raise BudgetStateError
            input_price = _money(profile.prices.input_cache_miss)
            output_price = _money(profile.prices.output)
            cost = _exact_sum(
                (
                    _token_cost(len(encoded) + 1024, input_price),
                    _token_cost(maximum, output_price),
                )
            )
            fingerprint = hashlib.sha256(
                encoded
                + json.dumps(
                    {
                        "reviewer": reviewer,
                        "model": profile.model,
                        "max_tokens": maximum,
                        "input_cache_miss": _amount(input_price),
                        "output": _amount(output_price),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
            raise BudgetStateError from None
        with self._locked() as directory:
            ledger = self._read(directory)
            old = ledger["calls"].get(identity)
            if old is not None:
                if old["reviewer"] != reviewer or old["fingerprint"] != fingerprint:
                    raise BudgetStateError
                return self._summary(ledger)
            before = self._summary(ledger)
            if _exact_sum((_money(before["conservative_total_cny"]), cost)) > LIMIT_CNY:
                raise BudgetExceeded
            ledger["calls"][identity] = {
                "reviewer": reviewer,
                "fingerprint": fingerprint,
                "reserved_cost_cny": _amount(cost),
                "estimated_cost_cny": None,
            }
            self._write(directory, ledger)
            return self._summary(ledger)

    def settle(self, call_id, estimated_cost):
        identity = _identity(call_id)
        amount = None if estimated_cost is None else _money(estimated_cost)
        with self._locked() as directory:
            ledger = self._read(directory)
            item = ledger["calls"].get(identity)
            if item is None:
                raise BudgetStateError
            previous = item["estimated_cost_cny"]
            if amount is not None:
                if previous is not None and _money(previous) != amount:
                    raise BudgetStateError
                item["estimated_cost_cny"] = _amount(amount)
                self._write(directory, ledger)
            result = self._summary(ledger)
            if _money(result["conservative_total_cny"]) > LIMIT_CNY:
                raise BudgetExceeded
            return result

    def summary(self):
        with self._locked() as directory:
            return self._summary(self._read(directory))
