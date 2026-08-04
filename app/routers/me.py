"""Protected endpoint proving JWT verification works end to end.

Scaffolding only: it returns the verified user id and nothing else. It exists
so authentication can be tested before any portfolio endpoints depend on it.
"""
from fastapi import APIRouter, Depends

from app.auth import get_current_user_id

router = APIRouter(tags=["user"])


@router.get("/me")
def me(user_id: str = Depends(get_current_user_id)):
    """Returns the authenticated user's id.

    The Depends(...) line is the whole authentication mechanism: FastAPI runs
    get_current_user_id before this function, and if verification fails it
    raises and this body never executes. Any future protected endpoint gets
    the same guarantee by adding the same parameter.
    """
    return {"user_id": user_id}
