import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.http import HardeningMiddleware
from app.core.logging import configure_logging
from app.db.session import make_engine, make_sessionmaker
from app.marketdata.providers.registry import ProviderRegistry, build_default_registry
from app.marketdata.service import utcnow
from app.monitoring.health import database_check

log = logging.getLogger("agentx")


def create_app(
    settings: Settings | None = None, provider_registry: ProviderRegistry | None = None, clock: Callable[[], datetime] | None = None
) -> FastAPI:
    """`provider_registry` and `clock` are test hooks for deterministic providers/time; production uses the
    defaults (Yahoo, the real clock). Nothing market-data related runs on start-up: ingestion is only ever
    started explicitly."""
    settings = settings or get_settings()

    docs = settings.api_docs_enabled  # /docs, /redoc, /openapi.json (off in staging/production)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.engine.dispose()

    app = FastAPI(title="Agent-X Backend", version="0.1.0", lifespan=lifespan, docs_url="/docs" if docs else None,
                  redoc_url="/redoc" if docs else None, openapi_url="/openapi.json" if docs else None)
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)
    app.state.provider_registry = provider_registry or build_default_registry()
    app.state.clock = clock or utcnow

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness: the process is up. Does not touch the database."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready(response: Response) -> dict[str, str]:
        """Readiness: the database is reachable and its schema is at this code's migration head. Public, so it says
        nothing more (details: GET /api/v1/system/status, authenticated). Optional components never affect it."""
        check = database_check(app.state.engine)
        if check["database"] != "ok" or not check["migration"]["compatible"]:
            log.warning("readiness check failed", extra={"fields": {
                "database": check["database"], "migration": check["migration"],
                "error_category": check.get("error_category")}})
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ready"}

    app.include_router(api_router)
    if settings.cors_allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_allow_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-Request-ID"], max_age=600)
    app.add_middleware(HardeningMiddleware, max_body_bytes=settings.max_request_body_bytes, hsts=settings.is_production)
    return app


def build_app() -> FastAPI:
    configure_logging()
    return create_app()
