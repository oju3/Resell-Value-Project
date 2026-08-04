"""Dev utility: obtains a Supabase access token (JWT) for testing protected
API endpoints, since there is no frontend yet to log in with.

Create the test user first in the Supabase Dashboard:
    Authentication -> Users -> Add user, with "Auto Confirm User" enabled
(auto-confirm skips the email round trip, which there is no inbox for here).

Usage, from the project root:

    TOKEN=$(.venv/bin/python scripts/get_test_token.py)
    curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/me

Prompts are written to stderr and only the token goes to stdout, so the
command substitution above captures the token alone.

Reads SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY via load_dotenv rather than
taking them as arguments, and reads the password with getpass, so no secret
ever appears in argv or shell history. Prints neither key.
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
publishable_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY")

if not supabase_url:
    print("SUPABASE_URL not found in .env", file=sys.stderr)
    sys.exit(1)
if not publishable_key:
    print("SUPABASE_PUBLISHABLE_KEY not found in .env", file=sys.stderr)
    sys.exit(1)

sys.stderr.write("Email: ")
sys.stderr.flush()
email = sys.stdin.readline().strip()
password = getpass.getpass("Password: ")

if not email or not password:
    print("Email and password are both required", file=sys.stderr)
    sys.exit(1)

request = urllib.request.Request(
    f"{supabase_url}/auth/v1/token?grant_type=password",
    data=json.dumps({"email": email, "password": password}).encode(),
    headers={
        "apikey": publishable_key,
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
except urllib.error.HTTPError as e:
    # Supabase returns a JSON error body; surface its message but never the
    # request headers, which carry the publishable key.
    try:
        detail = json.load(e).get("error_description") or json.load(e).get("msg")
    except Exception:
        detail = None
    print(f"Sign-in failed (HTTP {e.code}){': ' + detail if detail else ''}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Sign-in request failed: {type(e).__name__}", file=sys.stderr)
    sys.exit(1)

access_token = payload.get("access_token")
if not access_token:
    print("No access_token in Supabase response", file=sys.stderr)
    sys.exit(1)

expires_in = payload.get("expires_in")
if expires_in:
    sys.stderr.write(f"Token acquired. Expires in {expires_in} seconds.\n")

print(access_token)
