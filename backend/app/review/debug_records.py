"""本机审核失败现场；与项目报告/导出隔离，30天后不可读取并自动清理。"""

import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

RETENTION = timedelta(days=30)
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_RAW_BYTES = 2 * 1024 * 1024
_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json")


class ReviewDebugError(RuntimeError):
    def __init__(self):
        super().__init__("审核调试记录暂时不可用。")


class LocalReviewDebugStore:
    def __init__(self, root: Path, *, clock=None):
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _directory(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, 0o700)
                yield descriptor
            finally:
                os.close(descriptor)
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ReviewDebugError from None

    def _read(self, directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
                raise ValueError
            record = json.loads(stream.read(MAX_RECORD_BYTES + 1))
        if not isinstance(record, dict) or record.get("call_id") + ".json" != name:
            raise ValueError
        expires = datetime.fromisoformat(record["expires_at"])
        if expires.tzinfo is None:
            raise ValueError
        return record, expires

    def _records(self, directory):
        records = []
        for name in os.listdir(directory):
            if not _NAME.fullmatch(name):
                continue
            try:
                record, expires = self._read(directory, name)
                if expires <= self.clock():
                    os.unlink(name, dir_fd=directory)
                else:
                    records.append(record)
            except (OSError, KeyError, ValueError, TypeError, UnicodeError):
                # 不跟随链接或暴露损坏文件；无效条目不进入管理员列表。
                continue
        return records

    def write(
        self,
        *,
        call_id,
        project_id,
        job_id,
        reviewer,
        instruction_version,
        raw_output,
        errors,
        secrets=(),
    ):
        try:
            identity, project = str(UUID(str(call_id))), str(UUID(str(project_id)))
            job = str(UUID(str(job_id))) if job_id is not None else None
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reviewer):
                raise ValueError
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", instruction_version):
                raise ValueError
            if not isinstance(raw_output, str) or len(raw_output.encode()) > MAX_RAW_BYTES:
                raise ValueError
            if not isinstance(errors, (list, tuple)) or not errors:
                raise ValueError
            # 异常条目的额外字段可能产生大量逐项错误，不能因此撤销已保留的有效意见。
            bounded_errors = list(errors[:1000])
            if len(errors) > 1000:
                bounded_errors[-1] = {
                    "path": "$",
                    "code": "errors_truncated",
                    "total": len(errors),
                }
            now = self.clock()
            record = {
                "call_id": identity,
                "project_id": project,
                "job_id": job,
                "reviewer": reviewer,
                "instruction_version": instruction_version,
                "created_at": now.isoformat(),
                "expires_at": (now + RETENTION).isoformat(),
                "raw_output": raw_output,
                "errors": bounded_errors,
            }
            sensitive = tuple(value for value in secrets if isinstance(value, str) and value)

            def redact(value):
                if isinstance(value, str):
                    for secret in sensitive:
                        value = value.replace(secret, "[REDACTED]")
                    return value
                if isinstance(value, dict):
                    return {redact(key): redact(item) for key, item in value.items()}
                if isinstance(value, (list, tuple)):
                    return [redact(item) for item in value]
                return value

            encoded = json.dumps(redact(record), ensure_ascii=False, allow_nan=False)
            data = encoded.encode()
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError
            with self._directory() as directory:
                self._records(directory)
                temporary = f".{uuid4()}.tmp"
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
                    os.replace(
                        temporary, f"{identity}.json", src_dir_fd=directory, dst_dir_fd=directory
                    )
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise ReviewDebugError from None

    def list(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ReviewDebugError
        with self._directory() as directory:
            records = self._records(directory)
        return [
            {key: value for key, value in record.items() if key not in {"raw_output", "errors"}}
            for record in sorted(records, key=lambda item: item["created_at"], reverse=True)[:limit]
        ]

    def purge_expired(self):
        """供后台定时调用；即使无人打开调试页面，也会删除到期现场。"""
        with self._directory() as directory:
            self._records(directory)

    def get(self, call_id):
        try:
            identity = str(UUID(str(call_id)))
        except (ValueError, TypeError, AttributeError):
            return None
        with self._directory() as directory:
            records = self._records(directory)
        return next((item for item in records if item["call_id"] == identity), None)
