"""Monte Carlo projection endpoint.

Thin by design, matching app/routers/valuation.py: the query and the arithmetic
live in app/projections.py so they can be called from a script or a test
without standing up HTTP. This file only maps that result onto status codes.

Its own router rather than a route bolted onto valuation.py: that file is
scoped to deadstock valuation and says so in its docstring, while projections
have their own table, module and build script. One router file per concern is
the pattern this project already follows.

No route-ordering hazard -- /sneakers/{sneaker_id}/projection is three
segments, so it cannot collide with /sneakers/search or /sneakers/{sneaker_id}
in app/routers/sneakers.py.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user_id
from app.db import get_conn
from app.projections import build_projection_response

router = APIRouter(tags=["projections"])


@router.get("/sneakers/{sneaker_id}/projection")
def sneaker_projection(
    sneaker_id: int,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Bear/base/bull Monte Carlo projections at 30, 60 and 90 days.

    Requires authentication, but `user_id` is deliberately never used in the
    query. This is market data -- the same sneaker projects the same way for
    every user -- so there is no user-scoped row to filter. The authorization
    note in app/db.py applies to owned_sneakers and other per-user tables; it
    is not being overlooked here.

    Three outcomes, and only the first is an error:

      404  the sneaker id does not exist.
      200  the sneaker exists but has no sneaker_projections row --
           confidence_tier is null, horizons is []. 7 of 50 sneakers are in
           this state today (the ones whose goat_product_id never resolved).
      200  the sneaker is projected, or was evaluated and suppressed.

    "Never processed" is a 200, not a 404, for the same reason
    app/routers/valuation.py returns 200 for a sneaker with no comps: "we have
    no projection for this shoe yet" is a correct answer to a valid question,
    and a 404 would leave the client unable to tell a bad id from a build job
    that has not run yet. confidence_tier being null is what distinguishes it,
    and it is distinct from the 'insufficient_data' tier, which means a row
    exists and was evaluated as unprojectable.

    Horizons are fixed at 30/60/90 days -- not a query parameter.

    Known limitation carried from app/projections.py: the uncertainty band is
    constant across all three horizons rather than widening with distance.
    """
    projection = build_projection_response(conn, sneaker_id)
    if projection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sneaker not found",
        )
    return projection
