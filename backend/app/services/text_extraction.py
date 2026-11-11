"""仅供可信审核流水线使用；查询正式版本后释放Session，再读取绑定原文件。"""

import hashlib
from uuid import UUID

from sqlalchemy import select

from app.models.delivery import DeliveryVersion
from app.review.extract import extract_text
from app.review.extract_types import ExtractedDocument, ExtractionError, ExtractionLimits
from app.storage import Storage


def extract_delivery_text(
    sessions,
    *,
    project_id: UUID,
    delivery_id: UUID,
    storage: Storage,
    limits: ExtractionLimits | None = None,
) -> ExtractedDocument:
    """不新增公开读权限，不处理共享材料/私稿，不改变交付或审核状态。"""
    limits = limits if limits is not None else ExtractionLimits()
    with sessions() as db:
        source = db.execute(
            select(DeliveryVersion.filename, DeliveryVersion.sha256, DeliveryVersion.size).where(
                DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id
            )
        ).one_or_none()
    if source is None:
        raise ExtractionError("source_unavailable")
    if source.size > limits.max_file_bytes:
        raise ExtractionError("limit_exceeded")
    try:
        with storage.path(source.sha256).open("rb") as stream:
            data = stream.read(limits.max_file_bytes + 1)
    except (OSError, ValueError):
        raise ExtractionError("source_unavailable") from None
    if len(data) > limits.max_file_bytes:
        raise ExtractionError("limit_exceeded")
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ExtractionError("source_mismatch")
    return extract_text(data, source.filename, limits=limits)
