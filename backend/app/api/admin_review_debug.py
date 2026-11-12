"""仅平台管理员读取本机失败现场，不属于项目导出。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.api.admin_usage import Administrator
from app.review.debug_records import LocalReviewDebugStore, ReviewDebugError

router = APIRouter(prefix="/admin/review-debug", tags=["admin-review-debug"])


def _store(request):
    return LocalReviewDebugStore(request.app.state.settings.review_debug_path)


@router.get("")
def list_debug(
    user: Administrator,
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=200),
):
    try:
        records = _store(request).list(limit=limit)
    except ReviewDebugError:
        raise HTTPException(
            503, "审核调试记录暂时不可用。", headers={"Cache-Control": "no-store"}
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return {"records": records, "retention_days": 30}


@router.get("/{call_id}")
def detail_debug(call_id: UUID, user: Administrator, request: Request, response: Response):
    try:
        record = _store(request).get(call_id)
    except ReviewDebugError:
        raise HTTPException(
            503, "审核调试记录暂时不可用。", headers={"Cache-Control": "no-store"}
        ) from None
    if record is None:
        raise HTTPException(404, "记录不存在或已过期。", headers={"Cache-Control": "no-store"})
    response.headers["Cache-Control"] = "no-store"
    return record
