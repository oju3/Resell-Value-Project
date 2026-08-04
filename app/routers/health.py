"""Unauthenticated liveness/readiness endpoint."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import check_database

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    """Reports whether the app is up AND whether Postgres actually answered.

    "database": "ok" means a real SELECT 1 round-tripped to Supabase, not
    merely that DATABASE_URL is set -- a config-only check would report
    healthy while the database was unreachable, which is precisely the
    outage a health check exists to catch.

    Returns 503 when the database is unreachable rather than 200 with an
    error body: uptime monitors and load balancers act on the status code,
    and a 200 would tell them the service is fine. The app itself is still
    running, hence "degraded" rather than a hard failure.
    """
    database_ok = check_database()
    if not database_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "error"},
        )
    return {"status": "ok", "database": "ok"}
