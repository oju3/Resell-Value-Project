"""Portfolio endpoints: add, list, delete, mark sold, sales history.

Every endpoint here is per-user. The user id comes from the verified JWT via
get_current_user_id and is passed into every query -- see the authorization
note at the top of app/portfolio.py for why that filter, and not RLS, is what
keeps one user's portfolio out of another's response.

ROUTE ORDER: /portfolio/sales is declared above /portfolio/{owned_id}. Both
match a two-segment path and FastAPI matches in declaration order. There is no
GET /portfolio/{owned_id} today so nothing collides yet, but declaring in the
safe order costs nothing and stops a future detail endpoint from silently
turning sales history into a 422.
"""
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.auth import get_current_user_id
from app.db import get_conn
from app.portfolio import (
    add_pair,
    delete_pair,
    list_portfolio,
    list_sales,
    mark_sold,
)

router = APIRouter(tags=["portfolio"])


class AddPairRequest(BaseModel):
    """The Literal types mirror the CHECK constraints in schema.sql, so an
    invalid source is a 422 with a readable message before the database is
    touched. The CHECK remains the backstop for any other writer."""
    sneaker_id: int
    size: str
    purchase_price: float
    purchase_date: date
    purchase_source: Literal["snkrs", "stockx", "goat", "ebay", "in_store", "other"]


class SellPairRequest(BaseModel):
    sale_price: float
    # Must match platform_fees.platform exactly -- a mismatch would produce a
    # zero-row fee lookup rather than an error.
    sale_platform: Literal["ebay", "stockx", "goat"]
    sale_date: date


@router.post("/portfolio", status_code=status.HTTP_201_CREATED)
def add_portfolio_pair(
    body: AddPairRequest,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Records a pair the user owns.

    No condition field: the MVP values deadstock only, so every pair is
    deadstock by definition -- see docs/scope_deadstock_only.md.

    A user may add the same sneaker more than once (different sizes, different
    prices), so there is no uniqueness check and no conflict handling.
    """
    return add_pair(
        conn,
        user_id=user_id,
        sneaker_id=body.sneaker_id,
        size=body.size,
        purchase_price=body.purchase_price,
        purchase_date=body.purchase_date,
        purchase_source=body.purchase_source,
    )


@router.get("/portfolio")
def get_portfolio(
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """The user's unsold pairs with current valuation and gross unrealized P/L.

    `unrealized_pl_gross` is BEFORE fees. Netting it requires choosing a
    platform to sell on, which is the recommendation engine's job and does not
    exist yet -- so the field says gross in its own name rather than relying on
    a reader checking the docs.

    `current_value` and `unrealized_pl_gross` are null when a sneaker has no
    comps. Null means "no valuation available", not zero.
    """
    return list_portfolio(conn, user_id)


@router.get("/portfolio/sales")
def get_sales_history(
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Sales history with realized P/L, net of platform fees.

    Every figure was frozen when the sale was recorded, so this reads history
    rather than recomputing it -- changing a fee rate in platform_fees will not
    retroactively alter what a past sale earned.
    """
    return list_sales(conn, user_id)


@router.delete("/portfolio/{owned_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_portfolio_pair(
    owned_id: int,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Hard-deletes a pair the user owns. 404 if it is not theirs or not there.

    404 rather than 403 for someone else's pair, deliberately: a 403 would
    confirm that the id exists, which leaks the size of another user's
    portfolio to anyone willing to enumerate ids.
    """
    if not delete_pair(conn, user_id, owned_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pair not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/portfolio/{owned_id}/sell", status_code=status.HTTP_201_CREATED)
def sell_portfolio_pair(
    owned_id: int,
    body: SellPairRequest,
    conn=Depends(get_conn),
    user_id: str = Depends(get_current_user_id),
):
    """Moves a pair to sales history, computing realized P/L net of fees.

    The fee comes from platform_fees, which is price-banded -- what you pay
    depends on the sale price, not just the platform. See the boundary note in
    app/portfolio.py.

    Atomic: the pair cannot end up in both tables or in neither. 404 if the
    pair is not the user's (same non-disclosure reasoning as delete).
    """
    result = mark_sold(
        conn,
        user_id=user_id,
        owned_id=owned_id,
        sale_price=body.sale_price,
        sale_platform=body.sale_platform,
        sale_date=body.sale_date,
    )

    if result == "no_fee_band":
        # Reachable only if platform_fees loses a band that the Literal above
        # still allows -- a server-side data gap, not bad input, so it must not
        # be reported as the caller's mistake.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No fee band configured for {body.sale_platform} at this sale price",
        )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pair not found",
        )
    return result
