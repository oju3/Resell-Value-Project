"""FastAPI application entrypoint.

Run locally from the project root with:

    .venv/bin/uvicorn app.main:app --reload

"app.main:app" is module path : variable name -- uvicorn imports app/main.py
and serves the `app` object defined below. Interactive docs are then at
http://127.0.0.1:8000/docs.

Phase 3 scaffolding: authentication and a health check only. Business logic
(search, portfolio, valuation, recommendations) is added as further routers.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import close_pool, init_pool
from app.routers import (
    health,
    me,
    portfolio,
    projections,
    recommendation,
    sneakers,
    valuation,
)

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Opens the connection pool before the server accepts traffic and closes
    it on shutdown. Everything before `yield` runs at startup, everything
    after at shutdown -- this replaces the deprecated @app.on_event hooks.
    """
    init_pool()
    yield
    close_pool()


app = FastAPI(
    title="Resell Value API",
    description="Sneaker resale valuation API. Phase 3 scaffolding.",
    version="0.1.0",
    lifespan=lifespan,
)

# Routers are registered here rather than defining endpoints in this file, so
# adding a Phase 3 resource means adding one file plus one line.
app.include_router(health.router)
app.include_router(me.router)
app.include_router(portfolio.router)
app.include_router(projections.router)
app.include_router(recommendation.router)
app.include_router(sneakers.router)
app.include_router(valuation.router)
