"""智能体 Bearer 不得借用人类会话通道；专有接口另行进行智能体授权。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import agent_permissions, agents

Database = Annotated[Session, Depends(get_session)]
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _path_uuid(request: Request, name: str) -> UUID | None:
    value = request.path_params.get(name)
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _human_action(request: Request) -> str:
    # 只读取已匹配路由的固定模板，不把实际路径、查询或请求正文带入审计。
    route_path = getattr(request.scope.get("route"), "path", "")
    if request.method not in _WRITE_METHODS:
        return "human_route"
    if "/plan" in route_path.split("/projects/{project_id}")[-1]:
        return "publish_plan"
    if route_path.endswith("/spec/versions"):
        return "publish_spec"
    if route_path.endswith("/requests/{request_id}/resolve"):
        return "resolve_request"
    if route_path.endswith("/modules/{module_id}/deliveries"):
        return "submit_delivery"
    if route_path.endswith("/confirm"):
        return "confirm_draft"
    if "/review" in route_path:
        return "final_review" if route_path.endswith("/final") else "annotate_review"
    if route_path.endswith("/modules") or route_path.endswith("/modules/{module_id}"):
        return "update_module"
    # 读方案、回报进度等动作也必须经专有智能体接口，不能以 Cookie 进入人类接口。
    return "human_route"


def _unauthenticated() -> HTTPException:
    return HTTPException(
        401,
        "智能体令牌无效或已吊销",
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
    )


def enforce_identity_channel(request: Request, db: Database) -> None:
    path = request.url.path
    if path == "/api/agent" or path.startswith("/api/agent/"):
        return
    if path == "/mcp" or path.startswith("/mcp/"):
        return
    if path != "/api" and not path.startswith("/api/"):
        return
    authorization = request.headers.get("authorization", "")
    parts = authorization.split(maxsplit=1)
    if not parts or parts[0].casefold() != "bearer":
        return
    try:
        agent = agents.authenticate_agent(db, parts[1] if len(parts) == 2 else None)
    except agents.AgentUnauthenticatedError:
        raise _unauthenticated() from None
    action = _human_action(request)
    scope = {
        "project_id": _path_uuid(request, "project_id"),
        "module_id": _path_uuid(request, "module_id"),
        "delivery_id": _path_uuid(request, "delivery_id"),
        "request_id": _path_uuid(request, "request_id"),
    }
    try:
        agent_permissions.authorize(db, agent, action, **scope)
        # 通道隔离独立于业务允许列表；即使未来允许列表扩展，也不能落入人类处理器。
        agent_permissions.deny(db, agent, action, "human_session_required", **scope)
    except agent_permissions.AgentPermissionDeniedError as error:
        if error.reason == "agent_inactive":
            raise _unauthenticated() from None
        raise HTTPException(
            403,
            "智能体必须使用专用接入接口，不能执行此人类操作",
            headers={"Cache-Control": "no-store"},
        ) from None
