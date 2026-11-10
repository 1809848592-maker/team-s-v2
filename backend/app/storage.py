"""内容寻址存储；文件名只作为显示信息，不参与存储路径。"""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class UploadTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredFile:
    sha256: str
    size: int


class Storage(Protocol):
    def put(self, stream: BinaryIO, max_bytes: int) -> StoredFile: ...

    def path(self, sha256: str) -> Path: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, sha256: str) -> Path:
        if _SHA256.fullmatch(sha256) is None:
            raise ValueError("无效的文件内容哈希")
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def put(self, stream: BinaryIO, max_bytes: int) -> StoredFile:
        if max_bytes < 0:
            raise ValueError("文件大小上限不能为负数")
        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=incoming, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                while chunk := stream.read(min(_CHUNK_BYTES, max_bytes - size + 1)):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLargeError("文件超过上传大小上限")
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            stored = StoredFile(sha256=digest.hexdigest(), size=size)
            destination = self.path(stored.sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 同一文件系统内以完整文件原子建立哈希路径；并发相同内容复用已有文件。
                os.link(temporary_path, destination)
            except FileExistsError:
                pass
            return stored
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
