"""Monte Carlo bear/base/bull projections from the stored trend fit.

Reads sneaker_projections (built by scripts/build_sneaker_projections.py) and
projects it forward. No modelling happens here -- run_monte_carlo below is
ported verbatim from scripts/projection_demo.py, arithmetic unchanged, which
was itself verified against three real sneakers spanning all three usable
tiers.

PROJECTS FROM TODAY, NOT FROM THE LAST OBSERVED SALE
-----------------------------------------------------
reference_date is the sneaker's EARLIEST daily row, and the stored fit's x-axis
runs in days from there. So "today" has to be expressed on that same axis
(days_since_reference = today - reference_date) before projecting days_ahead
further. Projecting from the last observed sale instead would silently shift
every horizon by however stale the data is.

UNCERTAINTY IS APPLIED ONCE, NOT ACCUMULATED
--------------------------------------------
A single random.gauss(0, residual_stdev) is drawn at the target date across
n_simulations. It is NOT compounded day by day. Accumulating it -- scaling
variance by sqrt(days) -- was a real bug caught and fixed during development;
it produced negative prices. The max(0, ...) floor is a second guard, not the
fix.

The consequence, and a documented limitation of this endpoint: the uncertainty
band is CONSTANT across all three horizons rather than widening with distance.
CT8012-005's residual_stdev is 41.53 at 30d, 60d and 90d alike. A 90-day
projection is not more uncertain than a 30-day one here, which is not how
forecast uncertainty actually behaves. Not fixed in this task.

WHY THE SEED IS FIXED
---------------------
With uncertainty applied once, the simulation draws from a single normal
distribution, so it converges to a closed form: bear/base/bull approach
expected_price -/+ 1.2816 * residual_stdev. Measured against the real fits, the
10,000-draw result lands within about $1.24 of that closed form. So an unseeded
draw would not show real uncertainty -- it would show a dollar or two of
sampling noise wobbling around a deterministic answer, which reads as
information and is not. A fixed seed makes the same request return the same
numbers, keeps responses cacheable, and keeps the endpoint testable.

This does NOT mean the Monte Carlo should be replaced by the closed form; the
ported arithmetic is kept as-is deliberately. It means the randomness is a
numerical method, not a source of per-request variation.

Linear trend only, per-sneaker tuned half-life. No non-linear model.
"""
import random
from datetime import date
from typing import Optional

# Fixed horizons. Not a query parameter and not user-configurable -- these are
# the three every test was run against.
HORIZON_DAYS = [30, 60, 90]

MONTE_CARLO_SIMULATIONS = 10000

# See "WHY THE SEED IS FIXED" above. Changing this changes every number this
# endpoint has ever returned, so it is a product decision, not a tuning knob.
RANDOM_SEED = 42

# Tiers with no reliable projection to show. Both were evaluated; neither
# yields numbers a user should act on.
SUPPRESSED_TIERS = ("suppressed", "insufficient_data")

# Match app/valuation.py so money and percentages round identically across
# sibling endpoints -- a frontend showing both should not get 36.1 from one and
# 15 decimal places from the other.
PRICE_DP = 2
PERCENT_DP = 1


# LEFT JOIN, not an inner join: a sneaker that exists but was never processed
# must be distinguishable from a sneaker id that does not exist at all. The
# join returns one row with NULL projection columns for the first case and zero
# rows for the second. 7 of 50 sneakers are in the first case today -- the ones
# whose goat_product_id was never resolved -- so this is a live path, not a
# hypothetical.
_PROJECTION_SQL = """
SELECT s.id, s.name, s.style_code,
       p.slope_per_day, p.intercept, p.residual_stdev, p.reference_date,
       p.confidence_tier, p.mape, p.n_rows, p.half_life_days
FROM sneakers s
LEFT JOIN sneaker_projections p ON p.sneaker_id = s.id
WHERE s.id = %(sneaker_id)s;
"""


def get_projection(conn, sneaker_id: int) -> Optional[dict]:
    """Stored fit for one sneaker, or None if the sneaker id does not exist.

    None means "no such sneaker" and becomes a 404 in the router. It is
    distinct from a sneaker that exists with no projection row, which returns a
    dict whose confidence_tier is None -- see build_projection_response.
    """
    with conn.cursor() as cur:
        cur.execute(_PROJECTION_SQL, {"sneaker_id": sneaker_id})
        row = cur.fetchone()

    if row is None:
        return None

    (sneaker_id, name, style_code, slope, intercept, residual_stdev,
     reference_date, tier, mape, n_rows, half_life_days) = row

    return {
        "sneaker_id": sneaker_id,
        "name": name,
        "style_code": style_code,
        "slope_per_day": float(slope) if slope is not None else None,
        "intercept": float(intercept) if intercept is not None else None,
        "residual_stdev": float(residual_stdev) if residual_stdev is not None else None,
        "reference_date": reference_date,
        "confidence_tier": tier,
        "mape": float(mape) if mape is not None else None,
        "n_rows": n_rows,
        "half_life_days": half_life_days,
    }


def run_monte_carlo(projection, days_ahead, n_simulations=MONTE_CARLO_SIMULATIONS,
                    seed=RANDOM_SEED):
    """Projects forward from TODAY, not from the fit's last observed date --
    this matters: reference_date is the sneaker's earliest daily row, and the
    fit's x-axis runs in days from there, so 'today' has to be expressed on
    that same axis before projecting days_ahead further.

    Ported verbatim from scripts/projection_demo.py. Do not change the
    arithmetic without re-verifying against that script's output.
    """
    random.seed(seed)
    days_since_reference = (date.today() - projection["reference_date"]).days
    target_x = days_since_reference + days_ahead

    expected_price = projection["intercept"] + projection["slope_per_day"] * target_x

    if projection["residual_stdev"] is None:
        # Can't simulate uncertainty without a measured spread. Return the
        # point estimate only, flagged, rather than inventing a stdev.
        return {"expected_price": expected_price, "simulated": False}

    final_prices = [
        max(0, expected_price + random.gauss(0, projection["residual_stdev"]))
        for _ in range(n_simulations)
    ]
    final_prices.sort()

    return {
        "expected_price": expected_price,
        "bear_10th": final_prices[int(0.10 * n_simulations)],
        "base_50th": final_prices[int(0.50 * n_simulations)],
        "bull_90th": final_prices[int(0.90 * n_simulations)],
        "simulated": True,
    }


def build_projection_response(conn, sneaker_id: int) -> Optional[dict]:
    """Full projection payload for one sneaker, or None if the id doesn't exist.

    Three outcomes, deliberately distinguishable:

      confidence_tier is None      the sneaker exists but was never processed
                                   into sneaker_projections. horizons is [].
                                   Encoded as null rather than a new tier value
                                   so it cannot be confused with the four real
                                   tiers, and so it stays out of the table's
                                   CHECK constraint vocabulary.

      tier in SUPPRESSED_TIERS     evaluated and found unreliable, or not
                                   evaluable at all. horizons is [].

      otherwise                    horizons carries one entry per HORIZON_DAYS.

    horizons is ALWAYS present, even when empty, so the response shape does not
    change between cases -- a client can read `horizons` unconditionally.

    In every non-projecting case the price keys are ABSENT, not null and not 0.
    A suppressed sneaker showing "bear: 0" would read as a prediction of
    worthlessness rather than an absence of prediction.
    """
    projection = get_projection(conn, sneaker_id)
    if projection is None:
        return None

    response = {
        "sneaker_id": projection["sneaker_id"],
        "name": projection["name"],
        "style_code": projection["style_code"],
        "confidence_tier": projection["confidence_tier"],
        # None-guarded: mape is null for a sneaker that was never processed.
        "mape": None if projection["mape"] is None else round(projection["mape"], PERCENT_DP),
        "n_rows": projection["n_rows"],
        "half_life_days": projection["half_life_days"],
        "horizons": [],
    }

    tier = projection["confidence_tier"]
    if tier is None or tier in SUPPRESSED_TIERS:
        return response

    for days_ahead in HORIZON_DAYS:
        result = run_monte_carlo(projection, days_ahead)
        horizon = {
            "days_ahead": days_ahead,
            "expected_price": round(result["expected_price"], PRICE_DP),
        }
        # simulated is False only when residual_stdev is NULL, which no
        # normal/low_confidence row currently has -- the branch is unreachable
        # on today's data and is kept rather than dropped. When it does fire,
        # the three range keys are omitted for the same reason they are omitted
        # for a suppressed sneaker: there is no measured spread to report, and
        # a fabricated one would be worse than their absence.
        if result["simulated"]:
            horizon["bear"] = round(result["bear_10th"], PRICE_DP)
            horizon["base"] = round(result["base_50th"], PRICE_DP)
            horizon["bull"] = round(result["bull_90th"], PRICE_DP)
        response["horizons"].append(horizon)

    return response
