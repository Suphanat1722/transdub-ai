from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import health, jobs, settings, voices
from .core.config import STATIC_DIR, ensure_directories
from .core.logging import configure_logging
from .repositories import database
from .services.worker import worker

APP_VERSION = "1.2.0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_directories()
    database.init_db()
    worker.start()
    yield
    worker.stop()


def create_app() -> FastAPI:
    configure_logging()
    application = FastAPI(title="TransDub AI", version=APP_VERSION, lifespan=lifespan)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @application.middleware("http")
    async def protect_local_mutations(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlparse

                parsed = urlparse(origin)
                if parsed.hostname not in {"localhost", "127.0.0.1"}:
                    return JSONResponse({"detail": "Origin ไม่ได้รับอนุญาต"}, status_code=403)
        return await call_next(request)

    application.include_router(health.router)
    application.include_router(voices.router)
    application.include_router(jobs.router)
    application.include_router(settings.router)
    application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return application


app = create_app()
