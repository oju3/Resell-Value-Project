"""Builds sneaker_projections: tuned trend fit per sneaker over goat_daily_sales.

Ported from scripts/exploration/per_sneaker_halflife.py, which was validated
across all 43 sneakers. The maths here is that script's, unchanged -- the port was
checked to reproduce its output exactly (43/43 swept, average MAPE 10.4%,
HV6674-067 at 43.7%) before this file was written. Do not "improve" the
arithmetic without re-running that comparison.

PIPELINE PER SNEAKER
--------------------
  1. Pull the daily series from goat_daily_sales, oldest first.
  2. Sweep CANDIDATE_HALF_LIVES. For each, fit on the first 75% of rows and
     measure MAPE against the held-out last 25%.
  3. Keep the half-life with the lowest MAPE.
  4. REFIT on 100% of rows at that half-life -- this is what gets stored.
  5. Assign a confidence tier from the backtest MAPE.
  6. Upsert one row.

WHY STEP 4 IS NOT STEP 2'S FIT
------------------------------
The backtest exists only to choose a half-life. Once chosen, throwing away the
most recent 25% of real observations forever would make every stored projection
permanently staler than the data supports. So the half-life comes from the
backtest and the slope/intercept/residual_stdev come from the full series.
mape therefore describes the CHOICE, not the error of the stored fit.

WEIGHTING
---------
Each row is weighted by exp(-ln(2)/half_life * days_before_most_recent), so a
row one half-life old counts half as much as the newest row. The half-life is
tuned per sneaker because the right amount of recency bias is not a global
constant -- on this catalogue the winning value ranges across the whole
candidate list.

CONFIDENCE TIERS
----------------
Set from the real MAPE distribution measured across all 43 sneakers:
  normal            MAPE <= 15
  low_confidence    15 < MAPE <= 25
  suppressed        MAPE > 25
  insufficient_data fewer than 8 daily rows -- no backtest possible
Every sneaker gets a row, including suppressed ones: "evaluated and found
unreliable" is real information, and an absent row would be indistinguishable
from "never evaluated".

insufficient_data is deliberately distinct from suppressed. It means the
sneaker could not be evaluated at all, not that it was evaluated badly. The
five fit columns are NULL for it -- never 0, never a guess. n_rows is still
populated, since it is the reason for the tier.

KNOWN LIMITATIONS (all deliberate, all deferred)
------------------------------------------------
- Linear trend only. Non-linear/curve fitting was considered and deferred:
  real overfitting risk on 22-100 data points, and the expected improvement is
  small relative to the cost, against what was attempted tonight.
- No StockX data, no order-count weighting on top of recency, no platform fee
  data. All out of scope for this script.
- Monte Carlo is not run here. This stores the trend-fit inputs Monte Carlo
  will consume later. When it does run, its uncertainty band is applied as a
  constant at each horizon rather than widening with projection distance --
  a documented limitation, not fixed here.

IDEMPOTENT. UNIQUE (sneaker_id) with ON CONFLICT DO UPDATE, so re-running
refreshes each sneaker in place. The whole rebuild runs in ONE transaction --
unlike the fetch scripts, which commit per sneaker because they are network
loops. Here everything is local arithmetic over already-stored rows, so an
all-or-nothing refresh is the cleaner semantic.

Never prints or logs DATABASE_URL.
"""
import argparse
import math
import os
import statistics
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Swept in this order; ties go to the first (longest) on min(). Recorded into
# half_life_candidates on every row so an old result stays auditable if this
# list ever changes.
CANDIDATE_HALF_LIVES = [30, 21, 14, 10, 7, 5, 3, 2]

# Last 25% of each sneaker's rows are held out to score a candidate.
HOLDOUT_FRACTION = 0.25

# Below this many daily rows there is no meaningful train/test split, so no
# tuning is possible and nothing is guessed.
MIN_ROWS_FOR_BACKTEST = 8

# From the measured MAPE distribution across all 43 sneakers.
TIER_NORMAL_MAX_MAPE = 15
TIER_LOW_CONFIDENCE_MAX_MAPE = 25


def get_daily_series(conn, sneaker_id):
    """Daily rows for one sneaker, oldest first. (sale_date, avg_amount, orders).

    orders is selected but unused by the fit -- carried so the tuple shape
    matches exploration/per_sneaker_halflife.py and so order-count weighting can be added
    later without changing this query.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sale_date, avg_amount, orders
            FROM goat_daily_sales
            WHERE sneaker_id = %s
            ORDER BY sale_date;
        """, (sneaker_id,))
        return cur.fetchall()


def weighted_linear_trend(rows, reference_date, half_life_days):
    """Exponentially-weighted least-squares line through (day_offset, avg_amount).

    x is days since reference_date, so intercept is only meaningful alongside
    that date -- which is why reference_date is stored on the row.

    Weight decays with distance from the MOST RECENT row, not from today: a row
    one half-life older than the newest counts half as much.

    Returns None when every row shares an x value, which makes the slope
    undefined. The caller must handle that rather than assume a value.
    """
    xs = [(row[0] - reference_date).days for row in rows]
    ys = [float(row[1]) for row in rows]
    most_recent_x = max(xs)
    decay_rate = math.log(2) / half_life_days
    weights = [math.exp(-decay_rate * (most_recent_x - x)) for x in xs]
    sum_w = sum(weights)
    mean_x = sum(w * x for w, x in zip(weights, xs)) / sum_w
    mean_y = sum(w * y for w, y in zip(weights, ys)) / sum_w
    numerator = sum(w * (x - mean_x) * (y - mean_y) for w, x, y in zip(weights, xs, ys))
    denominator = sum(w * (x - mean_x) ** 2 for w, x in zip(weights, xs))
    if denominator == 0:
        return None
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    return {
        "slope_per_day": slope,
        "intercept": intercept,
        "residual_stdev": statistics.stdev(residuals) if len(residuals) > 2 else None,
    }


def backtest_at_halflife(rows, half_life_days, holdout_fraction=HOLDOUT_FRACTION):
    """MAPE of one candidate half-life against the held-out tail, or None.

    Fits on the first (1 - holdout_fraction) of rows and predicts the rest.
    reference_date is the FULL series' first date, so train and test x values
    live on the same axis.

    None means "cannot be scored": too few rows, an empty holdout, or an
    undefined slope.
    """
    n = len(rows)
    if n < MIN_ROWS_FOR_BACKTEST:
        return None
    split_index = int(n * (1 - holdout_fraction))
    train_rows, test_rows = rows[:split_index], rows[split_index:]
    if len(test_rows) == 0:
        return None
    first_date = rows[0][0]
    trend = weighted_linear_trend(train_rows, first_date, half_life_days)
    if trend is None:
        return None
    percent_errors = []
    for sale_date, avg_amount, orders in test_rows:
        days_from_reference = (sale_date - first_date).days
        predicted = trend["intercept"] + trend["slope_per_day"] * days_from_reference
        actual = float(avg_amount)
        percent_errors.append(abs(predicted - actual) / actual * 100)
    return statistics.mean(percent_errors)


def find_best_halflife(rows):
    """Sweeps candidates, returns (best_half_life, best_mape) or (None, None)
    if there isn't enough data to backtest at all -- caller must handle that
    case rather than assume a value."""
    results = []
    for hl in CANDIDATE_HALF_LIVES:
        mape = backtest_at_halflife(rows, hl)
        if mape is not None:
            results.append((hl, mape))
    if not results:
        return None, None
    return min(results, key=lambda r: r[1])


def confidence_tier(mape):
    """Tier from backtest MAPE. None mape -> could not be evaluated at all.

    'suppressed' and 'insufficient_data' are not interchangeable: the first was
    measured and found unreliable, the second could not be measured. Both get a
    stored row.
    """
    if mape is None:
        return "insufficient_data"
    if mape <= TIER_NORMAL_MAX_MAPE:
        return "normal"
    if mape <= TIER_LOW_CONFIDENCE_MAX_MAPE:
        return "low_confidence"
    return "suppressed"


_UPSERT_SQL = """
INSERT INTO sneaker_projections
    (sneaker_id, half_life_days, slope_per_day, intercept, residual_stdev,
     mape, n_rows, reference_date, confidence_tier, half_life_candidates)
VALUES
    (%(sneaker_id)s, %(half_life_days)s, %(slope_per_day)s, %(intercept)s,
     %(residual_stdev)s, %(mape)s, %(n_rows)s, %(reference_date)s,
     %(confidence_tier)s, %(half_life_candidates)s)
ON CONFLICT (sneaker_id) DO UPDATE SET
    half_life_days       = EXCLUDED.half_life_days,
    slope_per_day        = EXCLUDED.slope_per_day,
    intercept            = EXCLUDED.intercept,
    residual_stdev       = EXCLUDED.residual_stdev,
    mape                 = EXCLUDED.mape,
    n_rows               = EXCLUDED.n_rows,
    reference_date       = EXCLUDED.reference_date,
    confidence_tier      = EXCLUDED.confidence_tier,
    half_life_candidates = EXCLUDED.half_life_candidates,
    computed_at          = now();
"""


def main():
    parser = argparse.ArgumentParser(
        description="Build sneaker_projections from goat_daily_sales."
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N sneakers, by style_code.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not found in .env", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)

    # Same selection as exploration/per_sneaker_halflife.py. Today this is the set
    # with goat_daily_sales rows (all 43 with a goat_product_id have data). A
    # sneaker with an id but no daily rows falls through to insufficient_data
    # rather than being skipped.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.style_code, s.name
            FROM sneakers s
            WHERE s.goat_product_id IS NOT NULL
            ORDER BY s.style_code
            LIMIT %s;
        """, (args.limit,))
        sneakers = cur.fetchall()

    print(f"Building projections for {len(sneakers)} sneaker(s)...\n")

    tier_counts = {}
    mapes = []

    # One transaction for the whole rebuild: this is a derived table and a
    # half-refreshed projection set is a worse state than an untouched one.
    with conn:
        with conn.cursor() as cur:
            for sneaker_id, style_code, name in sneakers:
                rows = get_daily_series(conn, sneaker_id)
                best_hl, best_mape = find_best_halflife(rows)
                tier = confidence_tier(best_mape)

                if best_hl is None:
                    # Could not evaluate. Every fit column stays NULL -- not 0,
                    # not a guess. n_rows is still recorded: it is the reason.
                    params = {
                        "sneaker_id": sneaker_id,
                        "half_life_days": None,
                        "slope_per_day": None,
                        "intercept": None,
                        "residual_stdev": None,
                        "mape": None,
                        "n_rows": len(rows),
                        "reference_date": None,
                        "confidence_tier": tier,
                        "half_life_candidates": Json(CANDIDATE_HALF_LIVES),
                    }
                    print(f"  [{style_code}] n={len(rows):<4} insufficient_data "
                          f"(needs >= {MIN_ROWS_FOR_BACKTEST} daily rows)   {name}")
                else:
                    # THE FINAL FIT: full dataset, chosen half-life. Not the
                    # 75% training fit the backtest used.
                    reference_date = rows[0][0]
                    final = weighted_linear_trend(rows, reference_date, best_hl)
                    if final is None:
                        # Unreachable if the backtest succeeded (the full series
                        # is a superset of the training rows). Stopping rather
                        # than storing a row whose tier claims a fit that does
                        # not exist.
                        print(f"\n[{style_code}] backtest succeeded but the full-dataset fit "
                              f"returned no slope. This should be impossible; stopping rather "
                              f"than writing an inconsistent row.", file=sys.stderr)
                        sys.exit(1)

                    params = {
                        "sneaker_id": sneaker_id,
                        "half_life_days": best_hl,
                        "slope_per_day": final["slope_per_day"],
                        "intercept": final["intercept"],
                        "residual_stdev": final["residual_stdev"],
                        "mape": best_mape,
                        "n_rows": len(rows),
                        "reference_date": reference_date,
                        "confidence_tier": tier,
                        "half_life_candidates": Json(CANDIDATE_HALF_LIVES),
                    }
                    mapes.append(best_mape)
                    print(f"  [{style_code}] n={len(rows):<4} half_life={best_hl:<3}d "
                          f"MAPE={best_mape:5.1f}%  slope={final['slope_per_day']:+8.3f}/day "
                          f"{tier:<16} {name}")

                cur.execute(_UPSERT_SQL, params)
                tier_counts[tier] = tier_counts.get(tier, 0) + 1

    print(f"\nDone. Upserted {len(sneakers)} projection(s).")
    print(f"Tiers: {tier_counts}")
    if mapes:
        print(f"Average MAPE across tuned sneakers: {statistics.mean(mapes):.1f}%")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT confidence_tier, COUNT(*), ROUND(AVG(mape), 1), MIN(n_rows), MAX(n_rows)
            FROM sneaker_projections GROUP BY 1 ORDER BY 2 DESC;
        """)
        print("\nsneaker_projections by tier (tier, rows, avg mape, n_rows min/max):")
        for row in cur.fetchall():
            print(f"  {row[0]:<18}{row[1]:<5}{str(row[2]):<8}{row[3]}-{row[4]}")

    conn.close()


if __name__ == "__main__":
    main()
