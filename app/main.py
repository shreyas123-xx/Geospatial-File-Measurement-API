"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from app.api import files
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory

DESCRIPTION = """
Upload a **Shapefile (.zip)**, **KML** or **KMZ** and get back every feature with its geometry, CRS,
attributes and **area / perimeter / length**.

Measurements are never taken in raw lat/lon degrees: geometries are reprojected to a local equal-area
(polygons) or equidistant (lines) projection and cross-checked against ellipsoidal geodesic calculations.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    Base.metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(
        title="Geospatial File Measurement API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(files.router)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        detail = exc.detail
        if not isinstance(detail, dict):
            detail = {"code": "http_error", "message": str(detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            {"error": {"code": "validation_error", "message": "Invalid request parameters.", "errors": errors}},
            status_code=422,
        )

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    return app


app = create_app()
