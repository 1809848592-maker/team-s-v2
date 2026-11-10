"""在SDK协议处理前认证每个请求，并在独立事务记录调用结果。"""

from contextlib import contextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.db import get_session
from app.events import record_event
from app.mcp_runtime import MCPRequestContext, mcp_request_context
from app.services import agents


class MCPAuthentication:
    def __init__(self, application, *, owner: FastAPI):
        self.application = application
        self.owner = owner

    def authenticate(self, raw_token):
        dependency = self.owner.dependency_overrides.get(get_session, get_session)
        with contextmanager(dependency)() as db:
            agent = agents.authenticate_agent(db, raw_token)
            return agent.id, agent.project_id, agent.owner_id

    def audit(self, identity, method, status):
        dependency = self.owner.dependency_overrides.get(get_session, get_session)
        with contextmanager(dependency)() as db:
            agent_id, project_id, owner_id = identity
            record_event(
                db,
                project_id=project_id,
                actor_type="agent",
                actor_id=agent_id,
                represented_user_id=owner_id,
                event_type="agent_call",
                object_type="agent",
                object_id=agent_id,
                details={"method": method, "route": "/mcp", "status_code": status},
            )
            db.commit()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.application(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        parts = headers.get(b"authorization", b"").decode("latin-1").split(maxsplit=1)
        raw_token = parts[1] if len(parts) == 2 and parts[0].casefold() == "bearer" else None
        try:
            identity = await run_in_threadpool(self.authenticate, raw_token)
        except agents.AgentUnauthenticatedError:
            response = JSONResponse(
                {"detail": "智能体令牌无效或已吊销"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        token = mcp_request_context.set(MCPRequestContext(self.owner, raw_token))
        status = 500

        async def no_store_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            await self.application(scope, receive, no_store_send)
        finally:
            mcp_request_context.reset(token)
            await run_in_threadpool(self.audit, identity, scope["method"], status)
