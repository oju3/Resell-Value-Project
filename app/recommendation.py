"""HOLD/SELL verdict: sell on GOAT today, or hold for the 30-day projection?

Combines two already-built pieces and adds no price modelling of its own:
  net_now       = goat_median (app/goat_sales.py) minus the GOAT fee at that price
  net_projected = the projection's 30-day 'base' (app/projections.py) minus the
                  GOAT fee at THAT price
  percent_gain  = (net_projected - net_now) / net_now * 100
  verdict       = HOLD if percent_gain > THRESHOLD_PERCENT_GAIN else SELL

BOTH SIDES ARE GOAT. THIS MATTERS.
-----------------------------------
An earlier design compared eBay's current median against GOAT's projected
price. That conflates a real price trend with the cross-platform price gap:
measured across the catalogue, projection-vs-ebay_median ratios spanned
0.77-2.51 with 11 of 41 sneakers more than 20% apart, so most of the resulting
"gain" was platform difference, not time. Same-platform comparison collapses
that spread to 0.85-1.14 with 0 of 41 beyond 20%.

ebay_median is still reported, clearly labelled, as a reference figure. It is
NOT part of the arithmetic.

KNOWN LIMITATION -- TABLE GRAIN MISMATCH
----------------------------------------
The two GOAT figures come from different tables at different grains.
goat_median is the IQR-fenced median of market_sales (individual transactions);
the projection is fitted on goat_daily_sales (GOAT's own daily averages).
Evaluating the fit at day 0 against goat_median gives a residual ratio of
roughly 0.85-1.14 (median 1.02, 32 of 41 within 10%).

So a few percent of percent_gain can be table mismatch rather than real trend,
and at a 30-day horizon where the median gain is only about 1.7%, that is not
negligible. Fixing it would mean using the fit's own day-0 value as net_now,
which would make percent_gain purely slope x days but would stop using the
observed current market price. Deliberately not done here.

THRESHOLD_PERCENT_GAIN IS A JUDGMENT CALL, NOT A MEASUREMENT
-------------------------------------------------------------
This is the one number in the pipeline with no evidence behind it, and that
distinction should not be blurred.

Everything else was measured. half_life_days was tuned per sneaker by sweeping
candidates against backtest MAPE. The confidence tiers were set from the real
MAPE distribution across 43 sneakers. The fuzzy-search threshold was calibrated
against the actual catalogue. Those numbers can be re-derived from data.

5.0% cannot. There is no historical record of "user held, and here is what
happened" to backtest against -- this project has never observed a HOLD/SELL
outcome. 5.0 is a starting assumption about how much upside justifies the
carrying risk of not selling today. It is open to revision, and revising it
needs outcome data that does not exist yet, not a re-run of an existing sweep.

Treat it as provisional. Do not cite it alongside the tuned constants as if it
shares their provenance.

GATING
------
No verdict is returned unless all three hold. Each failure reports its own
reason rather than a generic null:

  no_goat_data       goat_median is null -- nothing to price "now" from.
                     7 of 50 sneakers (those whose goat_product_id never
                     resolved). Checked FIRST: for those 7 the confidence gate
                     also fires, and "no GOAT data" is the root cause that
                     explains the missing projection, not the other way round.

  insufficient_data_for_recommendation
                     confidence_tier is suppressed, insufficient_data, or null.
                     A HOLD/SELL call implies more confidence than the
                     projection has; app/routers/projections.py already refuses
                     to show numbers for these, and this refuses the verdict
                     for the same reason.

  invalid_projection net_projected <= 0. The trend is linear and unbounded
                     below, so a steep enough decline extrapolates through zero
                     -- and GOAT's $5 fixed fee can make net proceeds negative
                     while the price is still positive. Arithmetically valid,
                     meaningless as advice.

low_confidence sneakers DO get a verdict, with the tier in the response so the
frontend can flag it. Silently treating them as 'normal' would hide exactly
what the tier exists to expose.
"""
from typing import Optional

from app.fees import get_fee_for_price
from app.goat_sales import get_goat_median
from app.projections import (
    HORIZON_DAYS,
    SUPPRESSED_TIERS,
    get_projection,
    run_monte_carlo,
)
from app.valuation import get_valuation

# See the docstring section above before changing this. It is not a tuned
# constant and must not be presented as one.
THRESHOLD_PERCENT_GAIN = 5.0

# Both sides of the comparison price on GOAT, so both use GOAT's fee schedule.
COMPARISON_PLATFORM = "goat"

# The single horizon this verdict uses. Chosen as the shortest available: the
# trend is linear, so error compounds with distance, and the projection's
# uncertainty band does NOT widen with horizon (a documented limitation of
# app/projections.py) -- meaning a 90-day figure is less trustworthy than a
# 30-day one in a way the response cannot express. Measured across the
# catalogue, 90 days also produces the absurd outputs the invalid_projection
# gate exists to catch.
RECOMMENDATION_HORIZON_DAYS = 30
assert RECOMMENDATION_HORIZON_DAYS in HORIZON_DAYS, (
    "recommendation horizon must be one the projection endpoint also serves"
)

# Match app/valuation.py and app/projections.py so a frontend showing several
# of these endpoints gets one precision per kind of quantity.
MONEY_DP = 2
PERCENT_DP = 1

REASON_NO_GOAT_DATA = "no_goat_data"
REASON_INSUFFICIENT = "insufficient_data_for_recommendation"
REASON_INVALID_PROJECTION = "invalid_projection"


def _net_proceeds(conn, price):
    """Price minus the GOAT fee at that price's band. None if no band covers it.

    The band is looked up against THIS price, not carried over from the other
    side of the comparison -- a price that crosses a threshold lands in a
    different band. GOAT has only one band today ([0, infinity)), so no crossing
    can actually occur on this platform; the lookup is still done per price so
    the logic stays correct if that changes.
    """
    band = get_fee_for_price(conn, price, COMPARISON_PLATFORM)
    if band is None:
        return None
    fee_percent, fixed_fee = band
    return price - (price * float(fee_percent) / 100 + float(fixed_fee))


def _no_verdict(base, reason):
    """A response with no HOLD/SELL call, carrying the reason instead.

    The numeric fields are null rather than absent here, unlike the projection
    endpoint's price keys. They are always-present slots whose value is
    genuinely unknown, not claims being withheld -- and a stable shape lets the
    frontend read them unconditionally.
    """
    base.update({
        "recommendation": None,
        "reason": reason,
        "net_now": None,
        "net_projected": None,
        "percent_gain": None,
    })
    return base


def get_recommendation(conn, sneaker_id: int) -> Optional[dict]:
    """HOLD/SELL for one sneaker, or None if the sneaker id does not exist.

    None means "no such sneaker" and becomes a 404 in the router, matching
    app/routers/valuation.py and app/routers/projections.py.
    """
    projection = get_projection(conn, sneaker_id)
    if projection is None:
        return None

    goat = get_goat_median(conn, sneaker_id)
    valuation = get_valuation(conn, sneaker_id)

    response = {
        "sneaker_id": projection["sneaker_id"],
        "name": projection["name"],
        "style_code": projection["style_code"],
        "confidence_tier": projection["confidence_tier"],
        "horizon_days": RECOMMENDATION_HORIZON_DAYS,
        # Reference figures, not inputs to the verdict. goat_median IS the basis
        # of net_now; ebay_median is reported for comparison only and is
        # deliberately absent from the arithmetic -- see the docstring.
        "goat_median": goat["goat_median"],
        "ebay_median": valuation["ebay_median"] if valuation else None,
    }

    if goat["goat_median"] is None:
        return _no_verdict(response, REASON_NO_GOAT_DATA)

    tier = projection["confidence_tier"]
    if tier is None or tier in SUPPRESSED_TIERS:
        return _no_verdict(response, REASON_INSUFFICIENT)

    result = run_monte_carlo(projection, RECOMMENDATION_HORIZON_DAYS)
    if not result["simulated"]:
        # residual_stdev was NULL, so there is no percentile band and no 'base'
        # figure to compare against. Unreachable on current data (no
        # normal/low_confidence row has a null residual_stdev). It shares the
        # invalid_projection reason because the outcome is the same -- the
        # projection cannot yield a usable comparison figure -- but it is a
        # distinct condition from net_projected <= 0.
        return _no_verdict(response, REASON_INVALID_PROJECTION)

    net_now = _net_proceeds(conn, float(goat["goat_median"]))
    net_projected = _net_proceeds(conn, result["base_50th"])

    # A missing band is a server-side data gap, not a caller error. Reported
    # rather than defaulted to a fee of zero.
    if net_now is None or net_projected is None:
        return _no_verdict(response, REASON_INVALID_PROJECTION)

    # net_now <= 0 would also make percent_gain a division by zero or a sign
    # flip. Both sides must be positive for the ratio to mean anything.
    if net_projected <= 0 or net_now <= 0:
        return _no_verdict(response, REASON_INVALID_PROJECTION)

    percent_gain = (net_projected - net_now) / net_now * 100

    response.update({
        "recommendation": "HOLD" if percent_gain > THRESHOLD_PERCENT_GAIN else "SELL",
        "reason": None,
        "net_now": round(net_now, MONEY_DP),
        "net_projected": round(net_projected, MONEY_DP),
        "percent_gain": round(percent_gain, PERCENT_DP),
    })
    return response
