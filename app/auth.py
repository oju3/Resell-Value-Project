"""Supabase JWT verification.

Approach: local verification against Supabase's published JWKS.
------------------------------------------------------------------
This Supabase project signs access tokens with ES256 (elliptic curve) and
publishes the matching *public* keys at
{SUPABASE_URL}/auth/v1/.well-known/jwks.json. This module fetches those keys,
caches them, and verifies each token's signature and claims in-process.

The property that makes this the right choice: the API holds only public
keys. It can verify a token but cannot mint one, so an attacker who fully
compromises this server still cannot forge a session for another user. The
rejected alternative -- legacy HS256 with Supabase's shared JWT secret --
uses one symmetric key for both signing and verifying, so any server holding
it can issue tokens for anyone.

Tradeoff, stated plainly: local verification cannot detect a token that was
revoked before it expired.
------------------------------------------------------------------
A signed-out or banned user's access token stays cryptographically valid
until its `exp` passes, and nothing here will reject it. The alternative --
calling GET /auth/v1/user on Supabase for every request -- would catch
revocation immediately, at the cost of a network round trip on every
protected endpoint and making Supabase's uptime a hard dependency of this
API's uptime. Supabase access tokens default to roughly a one-hour lifetime,
which bounds the exposure window, so that trade is accepted deliberately.
If this project ever needs immediate revocation (an admin ban that must take
effect at once, say), this is the module that has to change, and the change
is to validate remotely on sensitive endpoints -- not to weaken what is here.

Verification is not decoding. jwt.decode() with the right arguments checks
the signature, the expiry, the audience, and the issuer. Reading claims out
of a token without those checks would let anyone hand us any user id they
liked.
"""
import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from app.config import AUDIENCE, ISSUER, JWKS_URL

logger = logging.getLogger(__name__)

# Asymmetric algorithms only. This list must never include HS256: JWTs are
# vulnerable to an algorithm-confusion attack where an attacker takes the
# public key everyone can read, signs a token with it as an HMAC secret, and
# sets alg=HS256. Pinning to asymmetric algorithms makes that impossible.
# RS256 is allowed alongside ES256 purely so a future Supabase key rotation
# to RSA does not lock every user out; both are asymmetric, so neither
# weakens the guarantee above.
ALLOWED_ALGORITHMS = ["ES256", "RS256"]

# Caches keys in memory and only refetches when it sees a token with an
# unknown `kid` (i.e. after a key rotation). Verification is therefore a
# local operation, not a network call per request.
_jwk_client = PyJWKClient(JWKS_URL)

# auto_error=False is deliberate. With the default (True), FastAPI's
# HTTPBearer raises 403 when the Authorization header is missing, which is
# the wrong code -- 403 means "authenticated but not allowed," and a caller
# with no credentials at all has not authenticated. Handling the missing
# case below lets this API return a spec-correct 401 with WWW-Authenticate,
# independent of FastAPI's version-specific default.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """401 with WWW-Authenticate: Bearer, which RFC 7235 requires on a 401 --
    it tells the client which scheme to authenticate with."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency: verifies the bearer token, returns the user's UUID.

    Add `user_id: str = Depends(get_current_user_id)` to any endpoint to
    require authentication. The returned id is the value that portfolio
    queries must filter on -- see the authorization note in app/db.py.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Not authenticated")

    token = credentials.credentials

    # Three different failures can happen while picking a signing key, and
    # they do NOT all mean the same thing to the caller -- one is the
    # client's fault and two are ours, so they must not share a status code.
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
    except jwt.InvalidTokenError as e:
        # Structurally broken token ("abc", "aaa.bbb.ccc", bad base64).
        # PyJWKClient has to parse the token's header to read its `kid`
        # before it can choose a key, so a malformed token fails HERE rather
        # than in jwt.decode() below. Easy to mistake for an upstream
        # problem; it is a bad credential -> 401.
        logger.warning("Rejected token: %s", type(e).__name__)
        raise _unauthorized("Invalid authentication credentials")
    except PyJWKClientConnectionError:
        # Supabase's JWKS endpoint is unreachable. Our dependency is down,
        # the caller's token may be perfectly good -> 503, worth retrying.
        logger.exception("Could not reach Supabase JWKS endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable",
        )
    except PyJWKClientError as e:
        # Reached the JWKS fine, but no published key matches this token's
        # `kid` -- a forged or long-stale token -> 401.
        logger.warning("Token presented an unrecognised signing key id: %s", type(e).__name__)
        raise _unauthorized("Invalid authentication credentials")
    except Exception:
        logger.exception("Unexpected error fetching Supabase signing keys")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable",
        )

    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=AUDIENCE,
            issuer=ISSUER,
            # Reject a token that omits any of these rather than treating a
            # missing claim as "nothing to check." A token with no `exp`
            # would otherwise be valid forever.
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        # Distinguished from other failures on purpose: this is the one case
        # the client should respond to by refreshing its token and retrying,
        # rather than treating the credential as permanently bad. It leaks
        # nothing -- whoever holds the token can already read its exp.
        raise _unauthorized("Token has expired")
    except jwt.InvalidTokenError as e:
        # Everything else -- bad signature, wrong issuer, wrong audience,
        # malformed structure -- returns one generic message. Telling a
        # caller precisely which check failed helps them iterate towards a
        # token that passes. The detail is logged here instead.
        logger.warning("Rejected token: %s", type(e).__name__)
        raise _unauthorized("Invalid authentication credentials")

    user_id = payload.get("sub")
    if not user_id:
        logger.warning("Verified token contained no subject claim")
        raise _unauthorized("Invalid authentication credentials")

    return user_id
