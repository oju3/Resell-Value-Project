import os
import psycopg2
from dotenv import load_dotenv
import random
from datetime import date

load_dotenv('.env')
conn = psycopg2.connect(os.environ['DATABASE_URL'])

def get_projection(conn, style_code):
    """Pulls the stored trend fit for one sneaker. Returns None if the
    sneaker has no projection row, or if it's insufficient_data (no fit
    exists to project from)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sp.slope_per_day, sp.intercept, sp.residual_stdev,
                   sp.reference_date, sp.confidence_tier, sp.mape, sp.n_rows,
                   s.name
            FROM sneaker_projections sp
            JOIN sneakers s ON s.id = sp.sneaker_id
            WHERE s.style_code = %s;
        """, (style_code,))
        row = cur.fetchone()
        if row is None:
            return None
        slope, intercept, residual_stdev, reference_date, tier, mape, n_rows, name = row
        return {
            "slope_per_day": float(slope) if slope is not None else None,
            "intercept": float(intercept) if intercept is not None else None,
            "residual_stdev": float(residual_stdev) if residual_stdev is not None else None,
            "reference_date": reference_date,
            "confidence_tier": tier,
            "mape": float(mape) if mape is not None else None,
            "n_rows": n_rows,
            "name": name,
        }

def run_monte_carlo(projection, days_ahead, n_simulations=10000, seed=42):
    """Projects forward from TODAY, not from the fit's last observed date --
    this matters: reference_date is the sneaker's earliest daily row, and the
    fit's x-axis runs in days from there, so 'today' has to be expressed on
    that same axis before projecting days_ahead further."""
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

def show_projection(style_code):
    projection = get_projection(conn, style_code)
    if projection is None:
        print(f"[{style_code}] no projection row found")
        return

    print(f"\n=== {style_code}: {projection['name']} ===")
    print(f"Confidence: {projection['confidence_tier']}  (backtest MAPE {projection['mape']}%, n={projection['n_rows']})")

    if projection["confidence_tier"] in ("suppressed", "insufficient_data"):
        print("Projection SUPPRESSED -- not enough reliable signal to show bear/base/bull.")
        return

    for days_ahead in [30, 60, 90]:
        result = run_monte_carlo(projection, days_ahead)
        print(f"  {days_ahead}d -> Bear ${result['bear_10th']:.2f}  Base ${result['base_50th']:.2f}  Bull ${result['bull_90th']:.2f}")

# One from each real tier you already measured tonight
show_projection('CT8012-005')   # normal, 6.3%
show_projection('AR0715-101')   # low_confidence, 18.8%
show_projection('HV6674-067')   # suppressed, 43.7%

conn.close()
