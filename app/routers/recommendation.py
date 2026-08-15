"""HOLD/SELL recommendation endpoint.

Thin by design, matching app/routers/valuation.py and
app/routers/projections.py: the query and the arithmetic live in
app/recommendation.py so they can be called from a script or a test without
standing up HTTP. This file only maps that result onto status codes.

Its own router file, following the one-router-per-concern pattern this project
already uses. No route-ordering hazard -- /sneakers/{sneaker_id}/recommendation
is three segments, so it cannot collide with /sneakers/search or
/sneakers/{sneaker_id} in app/routers/sneakers.py.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user_id
from app.db import get_conn
from app.recommendation import get_recommendation

router = APIRouter(tags=["recommendation"])


@router.get("/sneakers/{sneaker_id}/recommendation")
def sneaker_recommendation(
    sneaker_id: int,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """HOLD or SELL, comparing GOAT proceeds today against the 30-day projection.

    Requires authentication, but `user_id` is deliberately never used in the
    query. This is market data -- the same sneaker recommends the same way for
    every user -- so there is no user-scoped row to filter. The authorization
    note in app/db.py applies to owned_sneakers and other per-user tables; it
    is not being overlooked here.

    Both sides of the comparison price on GOAT and are netted of GOAT fees.
    ebay_median appears as a labelled reference figure only and is not part of
    the verdict -- see app/recommendation.py for why mixing platforms produced
    a misleading signal.

    Two outcomes, and only the first is an error:

      404  the sneaker id does not exist.
      200  everything else, including every no-verdict case. `recommendation`
           is null and `reason` says which gate stopped it:
             no_goat_data                          no GOAT price to compare from
             insufficient_data_for_recommendation  tier suppressed/insufficient/null
             invalid_projection                    projected net proceeds <= 0

    A no-verdict response is not an error. "We will not call this one" is a
    correct answer to a valid question, consistent with how
    app/routers/valuation.py treats a sneaker with no comps and how
    app/routers/projections.py treats a suppressed one.

    The threshold separating HOLD from SELL is a stated judgment call rather
    than a measured value -- app/recommendation.py documents why it is the one
    number in this pipeline without evidence behind it.
    """
    recommendation = get_recommendation(conn, sneaker_id)
    if recommendation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sneaker not found",
        )
    return recommendation
