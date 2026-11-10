from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.agent_boundary import enforce_identity_channel
from app.api.agent_http import router as agent_http_router
from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.collaboration import router as collaboration_router
from app.api.deliveries import router as deliveries_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.modules import router as modules_router
from app.api.notifications import router as notifications_router
from app.api.plans import router as plans_router
from app.api.projects import router as projects_router
from app.api.resources import router as resources_router
from app.api.specs import router as specs_router
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        dependencies=[Depends(enforce_identity_channel)],
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
    app.include_router(deliveries_router, prefix="/api")
    app.include_router(collaboration_router, prefix="/api")

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


app = create_app()
