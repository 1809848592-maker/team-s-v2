from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error: RequestValidationError) -> JSONResponse:
        # 校验反馈保留字段位置和原因，避免将请求中的密码回显到响应中。
        details = [{key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": details})

    app.include_router(health_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


app = create_app()
