import os
import psycopg2
from dotenv import load_dotenv
import statistics
import random

load_dotenv('.env')
conn = psycopg2.connect(os.environ['DATABASE_URL'])

def get_daily_series(conn, style_code):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT gds.sale_date, gds.avg_amount, gds.orders
            FROM goat_daily_sales gds
            JOIN sneakers s ON s.id = gds.sneaker_id
            WHERE s.style_code = %s
            ORDER BY gds.sale_date;
        """, (style_code,))
        return cur.fetchall()

def linear_trend(rows):
    if len(rows) < 2:
        return None
    first_date = rows[0][0]
    xs = [(row[0] - first_date).days for row in rows]
    ys = [float(row[1]) for row in rows]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    return {
        "slope_per_day": slope,
        "intercept": intercept,
        "residual_stdev": statistics.stdev(residuals) if len(residuals) > 2 else None,
        "days_span": max(xs) - min(xs),
    }

def run_monte_carlo(trend, days_ahead, n_simulations=10000, seed=42):
    """Simulates n_simulations future prices at a single target date.

    The fitted line gives the expected (deterministic) price at days_ahead.
    Uncertainty around that expected price is modeled ONCE per simulation,
    not accumulated day by day -- the residual_stdev already represents the
    real day-to-day scatter observed in the data; re-adding it every single
    simulated day would compound variance far past what the data supports
    (variance of independent daily noise grows with sqrt(days), producing
    an unrealistically wide spread -- this was the bug in the first version).
    """
    random.seed(seed)
    expected_price = trend["intercept"] + trend["slope_per_day"] * (trend["days_span"] + days_ahead)

    final_prices = [
        max(0, expected_price + random.gauss(0, trend["residual_stdev"]))
        for _ in range(n_simulations)
    ]
    final_prices.sort()

    return {
        "expected_price": expected_price,
        "bear_10th": final_prices[int(0.10 * n_simulations)],
        "base_50th": final_prices[int(0.50 * n_simulations)],
        "bull_90th": final_prices[int(0.90 * n_simulations)],
        "min": final_prices[0],
        "max": final_prices[-1],
    }

rows = get_daily_series(conn, 'CT8012-005')
trend = linear_trend(rows)

print(f"Current fitted price: ${trend['intercept'] + trend['slope_per_day'] * trend['days_span']:.2f}")
print(f"Slope: ${trend['slope_per_day']:.4f}/day, Residual stdev: ${trend['residual_stdev']:.2f}\n")

for days_ahead in [30, 60, 90]:
    result = run_monte_carlo(trend, days_ahead)
    print(f"--- {days_ahead}-day projection ---")
    print(f"  Expected (trend only): ${result['expected_price']:.2f}")
    print(f"  Bear (10th pct):  ${result['bear_10th']:.2f}")
    print(f"  Base (50th pct):  ${result['base_50th']:.2f}")
    print(f"  Bull (90th pct):  ${result['bull_90th']:.2f}")
    print(f"  Full range:       ${result['min']:.2f} - ${result['max']:.2f}\n")

conn.close()
