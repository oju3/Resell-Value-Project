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
from fastapi.middleware.cors import CORSMiddleware

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

# The Vite dev server runs on a different origin from this API (:5173 vs
# :8000), and a different port is a different origin -- so without these
# headers the browser blocks every response the frontend reads, including the
# preflight OPTIONS that the Authorization header itself triggers.
#
# BOTH HOST SPELLINGS ARE LISTED ON PURPOSE.
# Starlette matches allow_origins by exact string, so http://localhost:5173 and
# http://127.0.0.1:5173 are two different origins even though they reach the
# same dev server. Listing only one produces a confusing failure: the
# middleware looks correctly installed, and requests fail CORS anyway depending
# on which URL the browser was pointed at.
#
# EXPLICIT ORIGINS, NOT "*" -- and not merely as a tightening.
# allow_origins=["*"] is INCOMPATIBLE with allow_credentials=True: the CORS
# spec forbids returning `Access-Control-Allow-Origin: *` alongside
# `Access-Control-Allow-Credentials: true`, and Starlette will not send the
# pair. A wildcard here would not be a laxer version of this configuration, it
# would be a broken one.
#
# Deployment: a hosted frontend adds its origin to this list. It does not
# replace the list with a wildcard, and these dev entries should come out once
# there is a real one.
FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    # Not load-bearing today: app/auth.py reads the token from an
    # `Authorization: Bearer` header, which is not a credential in the CORS
    # sense (that means cookies and TLS client certs). Set now so a later move
    # to a cookie-held refresh token does not fail as an unexplained CORS error.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
