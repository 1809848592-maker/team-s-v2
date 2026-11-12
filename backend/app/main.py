from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from app.api.admin_usage import router as admin_usage_router
from app.api.agent_boundary import enforce_identity_channel
from app.api.agent_http import router as agent_http_router
from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.collaboration import router as collaboration_router
from app.api.deliveries import router as deliveries_router
from app.api.drafts import router as drafts_router
from app.api.events import router as events_router
from app.api.final_reviews import router as final_reviews_router
from app.api.health import router as health_router
from app.api.model_disclosure import router as model_disclosure_router
from app.api.modules import router as modules_router
from app.api.notifications import router as notifications_router
from app.api.plans import router as plans_router
from app.api.projects import router as projects_router
from app.api.resources import router as resources_router
from app.api.reviews import router as reviews_router
from app.api.spec_drafting import router as spec_drafting_router
from app.api.specs import router as specs_router
from app.config import Settings, get_settings
from app.mcp_auth import MCPAuthentication
from app.mcp_server import create_mcp_app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    mcp_app = create_mcp_app(settings)

    @asynccontextmanager
    async def lifespan(application):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        dependencies=[Depends(enforce_identity_channel)],
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error: RequestValidationError) -> JSONResponse:
        # 校验反馈保留字段位置和原因，避免将请求中的密码回显到响应中。
        details = [{key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()]
        return JSONResponse(
            status_code=422, content={"detail": details}, headers={"Cache-Control": "no-store"}
        )

    app.include_router(health_router, prefix="/api")
    app.include_router(admin_usage_router, prefix="/api")
    app.include_router(model_disclosure_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(agents_router, prefix="/api")
    app.include_router(agent_http_router, prefix="/api")
    app.include_router(projects_router, prefix="/api")
    app.include_router(events_router, prefix="/api")
    app.include_router(plans_router, prefix="/api")
    app.include_router(modules_router, prefix="/api")
    app.include_router(notifications_router, prefix="/api")
    app.include_router(resources_router, prefix="/api")
    app.include_router(specs_router, prefix="/api")
    app.include_router(spec_drafting_router, prefix="/api")
    app.include_router(deliveries_router, prefix="/api")
    app.include_router(reviews_router, prefix="/api")
    app.include_router(final_reviews_router, prefix="/api")
    app.include_router(drafts_router, prefix="/api")
    app.include_router(collaboration_router, prefix="/api")

    # 原生ASGI路由保留精确/mcp地址，SDK负责协议及自身生命周期。
    protected_mcp = MCPAuthentication(mcp_app, owner=app)
    app.router.routes.extend(
        [Route("/mcp", endpoint=protected_mcp), Route("/mcp/", endpoint=protected_mcp)]
    )

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


app = create_app()
