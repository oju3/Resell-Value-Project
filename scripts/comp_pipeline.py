"""Shared eBay-sold-comp pipeline: actor calls, title/price parsing, the
docs/comp_filtering_spec.md filter rules, and sold_comps/comp_rejections
writes. Imported by scripts/apify_backfill.py (one-time, 90-day history) and
scripts/refresh_comps.py (recurring, rotating-subset top-up) so both share
one implementation of "what counts as a valid comp" instead of two that can
drift apart.

Never prints, logs, or echoes APIFY_TOKEN or DATABASE_URL. Any URL that gets
logged has the token query param redacted first.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation

ACTOR_ID = "caffein.dev~ebay-sold-listings"
ACTOR_RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
ACTOR_RECENT_RUNS_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"
ACCOUNT_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"

# docs/comp_filtering_spec.md, "Scrape parameters"
MIN_PRICE_BY_TIER = {1: 150, 2: 120, 3: 80}

# docs/comp_filtering_spec.md, "Exclusions" — word-boundary regex, not substring
EXCLUSION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bGS\b", r"\(PS\)", r"\(TD\)", r"\bYouth\b", r"\bToddler\b",
    r"Big Kids", r"Little Kids", r"Preschool",
)]
LOT_BUNDLE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"lot of", r"2 pairs", r"bundle",
)]

# docs/comp_filtering_spec.md, "Size"
DUAL_SIZE_PATTERN = re.compile(r"Size\s*(\d{1,2}(?:\.5)?)\s*M\s*/\s*\d{1,2}(?:\.5)?\s*W", re.IGNORECASE)
SIZE_PATTERN = re.compile(r"(?:Size|Sz)\.?\s*(\d{1,2}(?:\.5)?)(Y)?\b", re.IGNORECASE)


def redact_url(url):
    parsed = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qsl(parsed.query)
    redacted_qs = [(k, "***" if k == "token" else v) for k, v in qs]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(redacted_qs), parsed.fragment)
    )


def call_actor(token, query, min_price, count, days_to_scrape):
    url = ACTOR_RUN_URL + "?" + urllib.parse.urlencode({"token": token})
    payload = {
        "count": count,
        "daysToScrape": days_to_scrape,
        "ebaySite": "ebay.com",
        "includeCompletedListings": True,
        "itemCondition": "new",
        "keywords": [query],
        "minPrice": min_price,
        "sortOrder": "endedRecently",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"actor call failed ({redact_url(url)}): HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"actor call failed ({redact_url(url)}): {e.reason}") from None


def fetch_run_cost(token):
    """Best-effort: run-sync-get-dataset-items returns neither a run id nor cost
    data in its response (confirmed against Apify's OpenAPI spec — the only
    documented headers are the X-Apify-Pagination-* ones), so there's no run id
    to look up cost by. Instead, since call_actor blocks until the run finishes,
    the run we just paid for is the actor's most recently finished run — fetch it
    from the runs list, which includes usageTotalUsd directly. Returns None (not
    0) if that run can't be confidently identified as ours or the call fails."""
    url = ACTOR_RECENT_RUNS_URL + "?" + urllib.parse.urlencode({"token": token, "desc": "true", "limit": 1})
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    items = data.get("data", {}).get("items", [])
    if not items:
        return None
    run = items[0]
    if run.get("status") != "SUCCEEDED":
        return None
    finished_at = run.get("finishedAt")
    if not finished_at:
        return None
    try:
        finished = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    if (datetime.utcnow() - finished).total_seconds() > 120:
        return None
    return run.get("usageTotalUsd")


def fetch_remaining_budget_usd(token):
    """Best-effort: remaining monthly Apify spend (maxMonthlyUsageUsd minus
    monthlyUsageUsd) from the account limits endpoint. Returns None if the
    call fails or the response is missing the expected fields — this API
    has no stronger reliability guarantee than fetch_run_cost above, so
    callers must treat None as "unknown," not "zero remaining," and decide
    for themselves whether to fail open or closed on that ambiguity."""
    url = ACCOUNT_LIMITS_URL + "?" + urllib.parse.urlencode({"token": token})
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    try:
        max_monthly = data["data"]["limits"]["maxMonthlyUsageUsd"]
        current = data["data"]["current"]["monthlyUsageUsd"]
    except (KeyError, TypeError):
        return None
    return max_monthly - current


def extract_size(title):
    if not title:
        return None, False
    dual = DUAL_SIZE_PATTERN.search(title)
    if dual:
        return float(dual.group(1)), False
    match = SIZE_PATTERN.search(title)
    if match:
        return float(match.group(1)), bool(match.group(2))
    return None, False


def format_size(value):
    if value == int(value):
        return str(int(value))
    return str(value)


def parse_decimal(value):
    """Returns (Decimal, True) if value is missing or parses cleanly, (None, False)
    if value is present but not a valid number. The actor returns soldPrice,
    shippingPrice, and totalPrice as quoted strings (e.g. "1450.00"); Decimal(str(...))
    handles both that and the rare case where a field already comes back numeric."""
    if value is None:
        return None, True
    try:
        return Decimal(str(value)), True
    except InvalidOperation:
        return None, False


def parse_ended_at(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.split("T")[0], "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def filter_comp(item, seen_thumbnails):
    """Returns ("accept", row_dict) or ("reject", rejection_rule)."""
    item_id = item.get("itemId")
    if not item_id:
        return "reject", "missing_item_id"

    title = item.get("title") or ""

    if item.get("soldCurrency") != "USD":
        return "reject", "currency_not_usd"

    for pattern in EXCLUSION_PATTERNS:
        if pattern.search(title):
            return "reject", "exclusion_youth_kids"
    for pattern in LOT_BUNDLE_PATTERNS:
        if pattern.search(title):
            return "reject", "exclusion_lot_bundle"

    size_value, is_youth_suffix = extract_size(title)
    if size_value is not None and (is_youth_suffix or size_value < 7):
        return "reject", "size_below_men_7"

    thumb = item.get("thumbnailUrl")
    if thumb:
        if thumb in seen_thumbnails:
            return "reject", "duplicate_thumbnail"
        seen_thumbnails.add(thumb)

    ended_at = parse_ended_at(item.get("endedAt"))
    if ended_at is None:
        return "reject", "unparseable_ended_at"

    sold_price, sold_price_ok = parse_decimal(item.get("soldPrice"))
    if sold_price is None:
        return "reject", "missing_sold_price" if sold_price_ok else "unparseable_sold_price"

    shipping_price, shipping_price_ok = parse_decimal(item.get("shippingPrice"))
    if not shipping_price_ok:
        return "reject", "unparseable_shipping_price"

    total_price, total_price_ok = parse_decimal(item.get("totalPrice"))
    if not total_price_ok:
        return "reject", "unparseable_total_price"

    condition_id = item.get("conditionId")
    if condition_id is None:
        return "reject", "missing_condition_id"

    row = {
        "item_id": item_id,
        "title": title or None,
        "sold_price": sold_price,
        "shipping_price": shipping_price,
        "total_price": total_price,
        "currency": item.get("soldCurrency"),
        "size": format_size(size_value) if size_value is not None else None,
        "condition_id": condition_id,
        "listing_type": item.get("listingType"),
        "thumbnail_url": thumb,
        "ended_at": ended_at,
    }
    return "accept", row


def write_comps(conn, sneaker_id, rows):
    """Returns (written, skipped_item_ids). skipped_item_ids are rows whose item_id
    already existed in sold_comps (ON CONFLICT DO NOTHING triggered) — e.g. the same
    eBay listing matched an earlier sneaker's search query, since item_id is unique
    across the whole table, not per sneaker."""
    written = 0
    skipped_item_ids = []
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO sold_comps
                    (sneaker_id, item_id, title, sold_price, shipping_price, total_price,
                     currency, size, condition_id, listing_type, thumbnail_url, ended_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (item_id) DO NOTHING
                """,
                (sneaker_id, row["item_id"], row["title"], row["sold_price"], row["shipping_price"],
                 row["total_price"], row["currency"], row["size"], row["condition_id"],
                 row["listing_type"], row["thumbnail_url"], row["ended_at"]),
            )
            if cur.rowcount:
                written += cur.rowcount
            else:
                skipped_item_ids.append(row["item_id"])
    return written, skipped_item_ids


def find_existing_owners(conn, item_ids):
    """Looks up which sneaker_id already holds each given item_id, so a skipped
    (deduped) row can be reported as 'already scraped under sneaker X' rather than
    just a bare count."""
    if not item_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT item_id, sneaker_id FROM sold_comps WHERE item_id = ANY(%s)", (item_ids,))
        return dict(cur.fetchall())


def log_rejections(conn, sneaker_id, rejected_items):
    with conn.cursor() as cur:
        for item, rule in rejected_items:
            cur.execute(
                "INSERT INTO comp_rejections (sneaker_id, item_id, title, rejection_rule) VALUES (%s, %s, %s, %s)",
                (sneaker_id, item.get("itemId"), item.get("title"), rule),
            )
