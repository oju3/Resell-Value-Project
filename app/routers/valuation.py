"""Deadstock valuation endpoint.

Thin by design: the query and the arithmetic live in app/valuation.py, so
they can be called from a script or a test without standing up HTTP. This
file only maps that result onto status codes.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user_id
from app.db import get_conn
from app.goat_sales import get_goat_median
from app.valuation import get_valuation

router = APIRouter(tags=["valuation"])


@router.get("/sneakers/{sneaker_id}/valuation")
def sneaker_valuation(
    sneaker_id: int,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Current deadstock value for one sneaker, with risk, liquidity and trend.

    Requires authentication, but `user_id` is deliberately never used in the
    query. This is market data -- the same sneaker is worth the same to every
    user -- so there is no user-scoped row to filter. The authorization note
    in app/db.py (every query must carry `WHERE user_id = %s`) applies to
    owned_sneakers and other per-user tables; it is not being overlooked here.

    Two outcomes that are easy to confuse and are kept distinct:

      404  the sneaker id does not exist.
      200  the sneaker exists but has no comps -- `value` is null, both comp
           counts are 0, low_confidence is true.

    The second is not an error. "We have no data for this shoe" is a correct
    answer to a valid question, and collapsing it into a 404 would leave the
    client unable to tell a bad id from a scraper that has not run yet.
    Returning an invented number instead is ruled out by
    docs/scope_deadstock_only.md.
    """
    valuation = get_valuation(conn, sneaker_id)
    if valuation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sneaker not found",
        )
    # Two medians from two markets, as two labelled fields. Never blended,
    # never averaged -- they measure different things. goat_median is null for
    # a sneaker with no GOAT data; goat_sample_size carries the row count so a
    # thin sample is visible without a confidence label.
    valuation.update(get_goat_median(conn, sneaker_id))
    return valuation
