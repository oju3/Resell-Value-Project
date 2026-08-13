"""Deadstock valuation: value, risk, liquidity, price trend, and a size breakdown.

Scope: deadstock only (conditionId 1000). No condition multiplier is applied
and condition_multipliers is not read -- see docs/scope_deadstock_only.md.

How the headline value is computed (order matters)
--------------------------------------------------
Two stages, and the sequence is the whole design:

  1. Compute Q1 and Q3 over the UNFILTERED deadstock comps.
  2. Fence at Q1 - 1.5*IQR and Q3 + 1.5*IQR, dropping comps outside it.
  3. Median the SURVIVORS. That median is `value`.

risk_pct deliberately mixes the two stages: it is (Q3 - Q1) / median * 100,
using the PRE-fence quartiles over the POST-fence median. This looks
inconsistent and is not. Fencing removes the tails by construction, so
recomputing the IQR on survivors would tighten the spread by roughly the same
proportion for every sneaker, and the metric would stop discriminating
between volatile and stable shoes -- which is its only job. The pre-fence
quartiles measure real market dispersion. Dividing by the median is what
makes the number comparable across price points: a $130 spread means
something very different on a $110 sneaker than on a $1450 one.

Pre-fence vs post-fence: the rule
---------------------------------
POST-fence (the `kept` CTE) wherever we compute a PRICE.
PRE-fence (all deadstock comps) wherever we COUNT EVENTS.

An outlier is still a real transaction, so it counts toward liquidity and
toward the listing-type breakdown. But it must not be allowed to move a
three-comp trend median, so the trend windows read from `kept`.

  value, q1/q3, size_breakdown, trend medians  -> post-fence
  sales_per_week, listing_type_breakdown       -> pre-fence

Why the windows differ per metric
---------------------------------
`value` uses every comp with no date restriction: it is a LEVEL, and more
observations make the median more stable. `sales_per_week` is a RATE, and a
rate is meaningless without a fixed period, so it uses a 30-day window.
`trend_ratio` compares two windows because direction is only visible over
time.

Trend is reported alongside `value`, never folded into it. Recency-weighting
the median would cut the effective sample from ~17 comps to a handful, and
thin samples have already produced unreliable numbers in this project (an
observed StockX pair of consecutive same-size sales at $352 and $603).
Recency-weighted valuation is a v1.1 candidate, once refresh_comps.py has
accumulated six or more months of history and the approach can be backtested
rather than assumed.

Expected: 21 of 50 sneakers currently return trend_ratio = null, because the
30-90 day window holds fewer than 3 SURVIVING comps. The backfill is
recency-skewed -- eBay's own sold-listing history is shallow -- so the older
window is the binding constraint, not the recent one. (Counted pre-fence it
would be 18; the gap is three sneakers whose older window drops below 3 once
the fence is applied, which is the post-fence rule above doing its job.)
Likewise 16 of 50 currently return an empty size_breakdown. Both should
improve as refresh_comps.py accumulates history. Expected behaviour, not a bug.

Both trend windows and liquidity are anchored to CURRENT_DATE, so all three
degrade toward null/zero if the refresh job stops running. `value` does not.
"""
from typing import Optional

# eBay conditionId for Brand New. The only condition this MVP values.
DEADSTOCK_CONDITION_ID = 1000

# Fewer than this many comps SURVIVING the fence flags low_confidence.
# From docs/comp_filtering_spec.md's Aggregation rule.
LOW_CONFIDENCE_MIN_COMPS = 5

# A size needs this many surviving comps to appear in size_breakdown. Sizes
# below it are omitted entirely rather than reported off one or two sales.
MIN_COMPS_PER_SIZE = 3

# Each trend window needs this many comps, else trend_ratio is null.
MIN_COMPS_PER_TREND_WINDOW = 3

# Liquidity and the recent trend window are both 30 days today, but they are
# separate knobs on purpose: one is a sales-rate period, the other is one arm
# of a price comparison. Changing one should not silently change the other.
LIQUIDITY_WINDOW_DAYS = 30
TREND_RECENT_WINDOW_DAYS = 30
TREND_OLDER_WINDOW_DAYS = 90

# Rounding: prices 2dp, ratios 3dp, percentages 1dp. sales_per_week is a rate
# rather than any of those three and gets 2dp, which is how a figure like
# 3.03 sales/week reads naturally.
PRICE_DP = 2
RATIO_DP = 3
PERCENT_DP = 1
RATE_DP = 2


# Shared by the metrics query and the size-breakdown query. Defined once so
# the fence can never drift between them -- if the two disagreed, the size
# medians would be filtered differently from the headline median.
#
# `kept` CROSS JOINs `fence`, which is a single row, purely to make the two
# bounds visible to every comp for the comparison. That join is how the
# two-stage sequence is expressed in SQL.
_FENCE_CTE = """
WITH deadstock AS (
    SELECT sold_price, size, listing_type, ended_at
    FROM sold_comps
    WHERE sneaker_id = %(sneaker_id)s
      AND condition_id = %(deadstock_condition_id)s
),
bounds AS (
    SELECT
        percentile_cont(0.25) WITHIN GROUP (ORDER BY sold_price) AS q1,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY sold_price) AS q3,
        COUNT(*) AS comp_count_raw,
        -- Pre-fence on purpose: liquidity counts events, not prices.
        COUNT(*) FILTER (
            WHERE ended_at >= CURRENT_DATE - %(liquidity_window_days)s
        ) AS liquidity_count
    FROM deadstock
),
fence AS (
    SELECT
        q1, q3, comp_count_raw, liquidity_count,
        q1 - 1.5 * (q3 - q1) AS lower_fence,
        q3 + 1.5 * (q3 - q1) AS upper_fence
    FROM bounds
),
kept AS (
    SELECT d.*
    FROM deadstock d
    CROSS JOIN fence f
    WHERE d.sold_price BETWEEN f.lower_fence AND f.upper_fence
)
"""

# One row if the sneaker exists, zero rows if it does not -- which is the
# whole 404 check, with no separate existence query.
#
# That works because `fence`, `headline` and `trend` are ungrouped aggregates:
# they return exactly one row even over zero comps (with NULL medians and
# COUNT 0), so the CROSS JOINs can never eliminate the sneakers row.
_METRICS_SQL = _FENCE_CTE + """
, headline AS (
    SELECT
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price) AS value,
        COUNT(*) AS comp_count_used
    FROM kept
),
trend AS (
    -- Post-fence: one outlier must not move a 3-comp window's median.
    -- The windows are half-open so a comp landing exactly on the 30-day
    -- boundary belongs to the recent window only, never to both.
    SELECT
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE ended_at >= CURRENT_DATE - %(trend_recent_days)s)
            AS recent_median,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE ended_at >= CURRENT_DATE - %(trend_older_days)s
                      AND ended_at <  CURRENT_DATE - %(trend_recent_days)s)
            AS older_median,
        COUNT(*) FILTER (
            WHERE ended_at >= CURRENT_DATE - %(trend_recent_days)s
        ) AS trend_recent_count,
        COUNT(*) FILTER (
            WHERE ended_at >= CURRENT_DATE - %(trend_older_days)s
              AND ended_at <  CURRENT_DATE - %(trend_recent_days)s
        ) AS trend_older_count
    FROM kept
)
SELECT
    s.id, s.name, s.style_code,
    f.q1, f.q3, f.comp_count_raw, f.liquidity_count,
    h.value, h.comp_count_used,
    t.recent_median, t.older_median, t.trend_recent_count, t.trend_older_count
FROM sneakers s
CROSS JOIN fence f
CROSS JOIN headline h
CROSS JOIN trend t
WHERE s.id = %(sneaker_id)s;
"""

# Post-fence: these are prices. ORDER BY size::numeric because size is TEXT --
# sorted as text, '10' would come before '7'. Every stored size matches
# ^[0-9]+(\\.[0-9]+)?$, so the cast is safe.
_SIZE_BREAKDOWN_SQL = _FENCE_CTE + """
SELECT
    size,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price) AS median_price,
    COUNT(*) AS comp_count
FROM kept
WHERE size IS NOT NULL
GROUP BY size
HAVING COUNT(*) >= %(min_comps_per_size)s
ORDER BY size::numeric;
"""

# Pre-fence: this counts events, so it needs no fence and no CTE chain.
# Returned as raw counts with no weighting applied -- auctions clear below
# Buy It Now, and keeping them separable is what lets a later version weight
# them. Not grouped into two buckets: the data holds three listing types
# (buy_it_now, best_offer_accepted, auction), and hardcoding two would
# silently hide one of them.
_LISTING_TYPE_SQL = """
SELECT listing_type, COUNT(*) AS comp_count
FROM sold_comps
WHERE sneaker_id = %(sneaker_id)s
  AND condition_id = %(deadstock_condition_id)s
GROUP BY listing_type
ORDER BY comp_count DESC;
"""


# Batched headline value for many sneakers at once, used by the portfolio list
# so that showing N pairs costs ONE query instead of N x 3.
#
# WHY THIS DUPLICATES THE FENCE LOGIC IN _FENCE_CTE ABOVE
# -------------------------------------------------------
# The obvious move is to make _FENCE_CTE group-aware and share it. It does not
# work, for a specific reason worth keeping: get_valuation depends on ungrouped
# aggregates returning exactly ONE row even over zero comps (NULL medians,
# COUNT 0). That property is what makes `fetchone() is None` a reliable 404
# check and what produces the null-metric zero-comp response. Add
# GROUP BY sneaker_id and a sneaker with no comps returns NO row at all, and
# both behaviours collapse.
#
# So the two forms genuinely cannot be one query. The fence maths below must
# stay identical to _FENCE_CTE -- change one, change the other.
#
# Deliberately narrower than get_valuation: headline value and comp counts
# only, no trend, size breakdown or listing types. The list view does not
# display them, and per-pair detail is the valuation endpoint's job.
_BATCH_VALUES_SQL = """
WITH deadstock AS (
    SELECT sneaker_id, sold_price
    FROM sold_comps
    WHERE condition_id = %(deadstock_condition_id)s
      AND sneaker_id = ANY(%(sneaker_ids)s)
),
bounds AS (
    SELECT sneaker_id,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY sold_price) AS q1,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY sold_price) AS q3,
           COUNT(*) AS comp_count_raw
    FROM deadstock
    GROUP BY sneaker_id
),
fence AS (
    SELECT sneaker_id, comp_count_raw,
           q1 - 1.5 * (q3 - q1) AS lower_fence,
           q3 + 1.5 * (q3 - q1) AS upper_fence
    FROM bounds
),
kept AS (
    SELECT d.sneaker_id, d.sold_price
    FROM deadstock d
    JOIN fence f ON f.sneaker_id = d.sneaker_id
    WHERE d.sold_price BETWEEN f.lower_fence AND f.upper_fence
)
SELECT k.sneaker_id,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY k.sold_price) AS value,
       COUNT(*) AS comp_count_used,
       MAX(f.comp_count_raw) AS comp_count_raw
FROM kept k
JOIN fence f ON f.sneaker_id = k.sneaker_id
GROUP BY k.sneaker_id;
"""


def get_values_for_sneakers(conn, sneaker_ids) -> dict:
    """Headline deadstock value for many sneakers in one query.

    Returns {sneaker_id: {"value", "comp_count_raw", "comp_count_used",
    "low_confidence"}}. A sneaker with no comps is simply ABSENT from the
    result -- callers must treat a missing key as "no valuation available"
    rather than as zero. Verified to match get_valuation exactly across the
    catalogue.
    """
    ids = list({int(i) for i in sneaker_ids})
    if not ids:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            _BATCH_VALUES_SQL,
            {
                "deadstock_condition_id": DEADSTOCK_CONDITION_ID,
                "sneaker_ids": ids,
            },
        )
        rows = cur.fetchall()

    return {
        sneaker_id: {
            "value": _round(value, PRICE_DP),
            "comp_count_used": comp_count_used,
            "comp_count_raw": comp_count_raw,
            "low_confidence": comp_count_used < LOW_CONFIDENCE_MIN_COMPS,
        }
        for sneaker_id, value, comp_count_used, comp_count_raw in rows
    }


def _round(value: Optional[float], places: int) -> Optional[float]:
    """round() that passes None through, so every "no data" path stays null
    instead of raising."""
    return None if value is None else round(value, places)


def get_valuation(conn, sneaker_id: int) -> Optional[dict]:
    """Returns the valuation for one sneaker, or None if the id doesn't exist.

    None means "no such sneaker" and becomes a 404 in the router. It is
    distinct from a sneaker that exists but has no comps -- that returns a
    real dict with null metrics, because "we have no data for this shoe" is a
    correct answer to a valid question, not an error.

    percentile_cont returns double precision, so the medians and quartiles
    arrive as Python floats rather than Decimal. Nothing here selects a raw
    NUMERIC column, which is what keeps Decimal out of the JSON response.
    """
    params = {
        "sneaker_id": sneaker_id,
        "deadstock_condition_id": DEADSTOCK_CONDITION_ID,
        "liquidity_window_days": LIQUIDITY_WINDOW_DAYS,
        "trend_recent_days": TREND_RECENT_WINDOW_DAYS,
        "trend_older_days": TREND_OLDER_WINDOW_DAYS,
        "min_comps_per_size": MIN_COMPS_PER_SIZE,
    }

    with conn.cursor() as cur:
        cur.execute(_METRICS_SQL, params)
        row = cur.fetchone()
        if row is None:
            return None

        (
            sneaker_id, name, style_code,
            q1, q3, comp_count_raw, liquidity_count,
            value, comp_count_used,
            recent_median, older_median, trend_recent_count, trend_older_count,
        ) = row

        cur.execute(_SIZE_BREAKDOWN_SQL, params)
        size_rows = cur.fetchall()

        cur.execute(_LISTING_TYPE_SQL, params)
        listing_type_rows = cur.fetchall()

    # A sneaker with no comps at all knows nothing -- every derived figure
    # stays null rather than reporting a manufactured zero. Note this is
    # narrower than it looks: a sneaker WITH comps but none in the last 30
    # days still reports sales_per_week = 0.0, which is a real observation
    # ("no recent sales"), not an absence of data.
    has_comps = comp_count_raw > 0

    # (Q3 - Q1) / median * 100. NULLIF-style guard on value: sold_price is
    # NOT NULL and the observed minimum is $75, so a zero median cannot
    # currently happen, but dividing by it would 500 the endpoint.
    risk_pct = None
    if has_comps and q1 is not None and q3 is not None and value:
        risk_pct = (q3 - q1) / value * 100

    sales_per_week = None
    if has_comps:
        sales_per_week = liquidity_count / LIQUIDITY_WINDOW_DAYS * 7

    # Null unless BOTH windows are thick enough. The threshold is on sample
    # size, not on the ratio itself: a large move off 30 sales is a real
    # event worth seeing, while the same move off one sale is noise. Both
    # counts are returned either way, so a null ratio explains itself.
    trend_ratio = None
    if (
        trend_recent_count >= MIN_COMPS_PER_TREND_WINDOW
        and trend_older_count >= MIN_COMPS_PER_TREND_WINDOW
        and older_median
    ):
        trend_ratio = recent_median / older_median

    return {
        "sneaker_id": sneaker_id,
        "name": name,
        "style_code": style_code,

        # Renamed from "value": this endpoint now returns two medians from two
        # different markets, so neither may carry a name implying it is THE
        # value. get_values_for_sneakers() above deliberately still returns
        # "value" -- it feeds the portfolio's current_value and is eBay-only.
        "ebay_median": _round(value, PRICE_DP),
        "q1": _round(q1, PRICE_DP),
        "q3": _round(q3, PRICE_DP),
        "risk_pct": _round(risk_pct, PERCENT_DP),
        "sales_per_week": _round(sales_per_week, RATE_DP),

        "trend_ratio": _round(trend_ratio, RATIO_DP),
        "trend_recent_count": trend_recent_count,
        "trend_older_count": trend_older_count,

        "comp_count_raw": comp_count_raw,
        "comp_count_used": comp_count_used,
        "low_confidence": comp_count_used < LOW_CONFIDENCE_MIN_COMPS,

        "size_breakdown": [
            {
                "size": size,
                "median_price": _round(median_price, PRICE_DP),
                "comp_count": comp_count,
            }
            for size, median_price, comp_count in size_rows
        ],
        "listing_type_breakdown": {
            listing_type: comp_count for listing_type, comp_count in listing_type_rows
        },
    }
