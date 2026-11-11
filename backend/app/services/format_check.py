"""内部只读检查入口；使用正式交付绑定的规范快照，读取文件前释放数据库连接。"""

import hashlib
from uuid import UUID

from sqlalchemy import select

from app.models.delivery import DeliveryVersion
from app.models.spec import SpecVersion
from app.review.extract_types import ExtractionLimits
from app.review.format_check import inspect_document
from app.storage import Storage


class FormatCheckError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            {
                "source_unavailable": "正式交付或绑定规范不可用，格式检查未完成。",
                "source_mismatch": "交付原文件与记录不一致，格式检查未完成。",
                "limit_exceeded": "交付超过处理上限，格式检查未完成。",
            }.get(code, "格式检查未完成。")
        )


def inspect_delivery_format(
    sessions,
    *,
    project_id: UUID,
    delivery_id: UUID,
    storage: Storage,
    limits: ExtractionLimits | None = None,
) -> dict:
    """不新增公开权限、不更改状态、不处理待确认稿或共享材料。"""
    limits = limits if limits is not None else ExtractionLimits()
    with sessions() as db:
        source = db.execute(
            select(
                DeliveryVersion.filename,
                DeliveryVersion.sha256,
                DeliveryVersion.size,
                SpecVersion.id.label("spec_version_id"),
                SpecVersion.version.label("spec_version"),
                SpecVersion.items,
            )
            .join(
                SpecVersion,
                (SpecVersion.id == DeliveryVersion.spec_version_id)
                & (SpecVersion.project_id == DeliveryVersion.project_id),
            )
            .where(DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id)
        ).one_or_none()
    if source is None:
        raise FormatCheckError("source_unavailable")
    if source.size > limits.max_file_bytes:
        raise FormatCheckError("limit_exceeded")
    try:
        with storage.path(source.sha256).open("rb") as stream:
            data = stream.read(limits.max_file_bytes + 1)
    except (OSError, ValueError):
        raise FormatCheckError("source_unavailable") from None
    if len(data) > limits.max_file_bytes:
        raise FormatCheckError("limit_exceeded")
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise FormatCheckError("source_mismatch")
    return {
        **inspect_document(data, source.filename, source.items, limits=limits),
        "delivery_id": str(delivery_id),
        "spec_version_id": str(source.spec_version_id),
        "spec_version": source.spec_version,
    }
