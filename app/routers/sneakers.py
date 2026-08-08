"""Sneaker catalogue endpoints: search and detail.

ROUTE ORDER IS LOAD-BEARING -- /sneakers/search MUST stay declared above
/sneakers/{sneaker_id}. Both match a two-segment path, and FastAPI matches in
declaration order. Declared the other way round, a request to /sneakers/search
binds "search" to an int path parameter and returns 422 -- search would be
permanently broken while looking like a validation bug. Keeping both routes in
one file is what makes that ordering visible.

No conflict with /sneakers/{sneaker_id}/valuation in routers/valuation.py --
that is three segments and unambiguous whichever router registers first.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user_id
from app.db import get_conn
from app.sneakers import get_sneaker, search_sneakers

router = APIRouter(tags=["sneakers"])


@router.get("/sneakers/search")
def search(
    q: str = Query(..., description="Free-text search over name, style code and colourway."),
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Two-stage catalogue search: exact substring first, fuzzy only on zero hits.

    `stage` in the response says which stage produced the rows, so the frontend
    can present fuzzy hits as "no exact matches, did you mean..." rather than
    passing them off as exact.

    A blank or whitespace-only `q` is a 400, not an empty result set and not
    the whole catalogue. Returning all 50 would silently turn search into a
    browse-everything endpoint -- a different job, and one nothing has asked
    for yet -- while an empty list would be indistinguishable from a genuine
    no-match. A missing `q` is FastAPI's own 422, since the parameter is
    required.

    Authentication is required but `user_id` is deliberately unused: the
    catalogue is the same for every user. The authorization rule in app/db.py
    applies to per-user tables like owned_sneakers, and is not overlooked here.
    """
    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'q' must not be empty",
        )
    return search_sneakers(conn, query)


@router.get("/sneakers/{sneaker_id}")
def sneaker_detail(
    sneaker_id: int,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """One sneaker's catalogue fields. No valuation -- see /sneakers/{id}/valuation.

    Kept separate on purpose: one endpoint, one job. This is a single-row
    primary-key lookup; the valuation runs three aggregate queries over
    sold_comps. Bundling them would make the fast part wait on the slow part
    and fail whenever the slow part failed. Split, the frontend renders the
    sneaker page immediately and fills the numbers in when they arrive.

    404 for an unknown id, matching routers/valuation.py.
    """
    sneaker = get_sneaker(conn, sneaker_id)
    if sneaker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sneaker not found",
        )
    return sneaker
