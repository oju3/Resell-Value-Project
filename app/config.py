"""Single place where .env is read.

The scripts in scripts/ each call load_dotenv() themselves because they are
independent one-shot processes. The API is one long-lived process, so it
loads .env exactly once here at import and every other module reads these
constants -- that way there is one answer to "where does this value come
from," not one per file.

Fails fast at import if a required variable is missing: a web server that
starts successfully and then 500s on the first request is harder to diagnose
than one that refuses to start with a clear message.

Never prints or logs the values themselves -- only the names of missing ones.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL")

_missing = [
    name for name, value in (
        ("SUPABASE_URL", SUPABASE_URL),
        ("DATABASE_URL", DATABASE_URL),
    )
    if not value
]
if _missing:
    raise RuntimeError(
        "Missing required environment variable(s) in .env: " + ", ".join(_missing)
    )

# Derived from SUPABASE_URL rather than stored separately -- fewer .env
# entries that can drift out of sync with each other.
#
# JWKS_URL serves Supabase's *public* signing keys. Fetching it needs no
# credentials, which is the whole point of the asymmetric setup: this API
# never holds anything that could mint a token. See app/auth.py.
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"

# Claims every Supabase-issued access token must carry. ISSUER pins tokens to
# this specific Supabase project -- without it, a validly-signed token from
# any other project would be accepted.
ISSUER = f"{SUPABASE_URL}/auth/v1"
AUDIENCE = "authenticated"

# Client-side pool bounds. Small on purpose: DATABASE_URL already points at
# Supabase's transaction pooler (port 6543), so this pool exists to avoid
# per-request TCP/TLS handshakes, not to manage database capacity.
DB_POOL_MIN_CONN = 1
DB_POOL_MAX_CONN = 10
