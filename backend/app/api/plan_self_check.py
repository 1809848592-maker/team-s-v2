from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.review.factory import build_llm_client
from app.review.llm import LLMDisabledError, LLMRequestError
from app.review.model_config import LLMConfigurationError
from app.review.plan_self_check import SelfCheckInstructionError, SelfCheckOutputError
from app.schemas.plan_self_check import PlanSelfCheckGenerate, PlanSelfCheckResult
from app.services import model_disclosure, plan_self_check, projects
from app.services.llm_usage import LLMDuplicateCallError, LLMUsageError, QuotaExceededError
from app.services.modules import ModuleValidationError

router = APIRouter(prefix="/projects/{project_id}/plan", tags=["plan self check"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]
HEADERS = {"Cache-Control": "no-store"}


@router.post(
    "/self-check", response_model=PlanSelfCheckResult, dependencies=[Depends(require_csrf)]
)
def generate(
    project_id: UUID,
    body: PlanSelfCheckGenerate,
    user: CurrentUser,
    db: Database,
    request: Request,
    response: Response,
):
    user_id = user.id
    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    # 认证查询的事务也不能跨网络持有连接。
    db.rollback()
    try:
        client = build_llm_client(sessions, request.app.state.settings)
        result = plan_self_check.generate(sessions, project_id, user_id, body, client=client)
    except ModuleValidationError as error:
        raise HTTPException(422, str(error), headers=HEADERS) from None
    except projects.LeaderRequiredError:
        raise HTTPException(403, "只有项目负责人可以执行方案自检", headers=HEADERS) from None
    except projects.ProjectError as error:
        mapped = project_error(error)
        mapped.headers = HEADERS
        raise mapped from None
    except (plan_self_check.SelfCheckDuplicateError, LLMDuplicateCallError) as error:
        raise HTTPException(409, str(error), headers=HEADERS) from None
    except (
        model_disclosure.ModelConsentRequiredError,
        model_disclosure.DisclosureVersionError,
    ) as error:
        raise HTTPException(409, str(error), headers=HEADERS) from None
    except QuotaExceededError as error:
        seconds = max(1, int((error.retry_at - datetime.now(UTC)).total_seconds()))
        return JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"模型额度不足，方案自检未完成；可于 {error.retry_at.isoformat()} 后重试。"
                ),
                "scope": error.scope,
                "retry_at": error.retry_at.isoformat(),
            },
            headers={**HEADERS, "Retry-After": str(seconds)},
        )
    except (LLMDisabledError, LLMConfigurationError, SelfCheckInstructionError):
        raise HTTPException(
            503, "平台模型尚未启用或配置不可用，草稿仍可编辑和发布。", headers=HEADERS
        ) from None
    except SelfCheckOutputError as error:
        raise HTTPException(502, str(error), headers=HEADERS) from None
    except (LLMRequestError, LLMUsageError):
        raise HTTPException(
            502, "方案自检未完成，请稍后重新自检。草稿仍可编辑和发布。", headers=HEADERS
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return result
