"""MCP请求的身份与数据库作用域，不把凭据暴露为工具参数。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import FastAPI

from app.db import get_session
from app.services import agents


@dataclass(frozen=True)
class MCPRequestContext:
    app: FastAPI
    raw_token: str


mcp_request_context: ContextVar[MCPRequestContext] = ContextVar("mcp_request_context")


@contextmanager
def open_agent_session():
    context = mcp_request_context.get()
    dependency = context.app.dependency_overrides.get(get_session, get_session)
    with contextmanager(dependency)() as db:
        try:
            agent = agents.authenticate_agent(db, context.raw_token)
            yield db, agent, context.app.state.settings
        finally:
            db.rollback()
