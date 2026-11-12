"""显式传入应用设置，避免构造客户端时隐式读取环境或初始化数据库。"""

import httpx2

from app.config import Settings
from app.review.debug_records import LocalReviewDebugStore
from app.review.llm import LLMClient
from app.review.model_config import ModelConfiguration
from app.services.llm_usage import QuotaLimits


def build_llm_client(
    sessions,
    settings: Settings,
    *,
    configuration: ModelConfiguration | None = None,
    transport: httpx2.BaseTransport | None = None,
) -> LLMClient:
    return LLMClient(
        sessions,
        configuration=configuration,
        secrets={"DEEPSEEK_API_KEY": settings.deepseek_api_key},
        live_enabled=settings.llm_enable_live_calls,
        transport=transport,
        debug_store=LocalReviewDebugStore(settings.review_debug_path),
        quota_limits=QuotaLimits(
            project_daily_reviews=settings.llm_project_daily_reviews,
            platform_daily_reviews=settings.llm_platform_daily_reviews,
            platform_daily_tokens=settings.llm_platform_daily_tokens,
            max_calls_per_review=settings.llm_max_calls_per_review,
        ),
    )
