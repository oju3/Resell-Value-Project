import os
import psycopg2
from dotenv import load_dotenv
import statistics
import math

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

def weighted_linear_trend(rows, reference_date=None, half_life_days=21):
    """Weighted least squares. Each row's weight decays exponentially with
    its distance from the MOST RECENT training row -- so a row half_life_days
    old counts half as much as the most recent row, a row 2*half_life_days
    old counts a quarter as much, etc.

    half_life_days=21 is a starting guess (3 weeks), not measured -- worth
    tuning against the backtest MAPE if you want to push this further, but
    it's a defensible default and clearly labeled as a knob, not a fact.

    This directly targets the bias found in the unweighted backtest: recent
    rows dominate the fit, so a recent flattening/reversal shows up in the
    slope instead of being averaged away by older, more numerous training
    days.
    """
    if len(rows) < 2:
        return None

    first_date = reference_date if reference_date else rows[0][0]
    xs = [(row[0] - first_date).days for row in rows]
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
        "half_life_days": half_life_days,
    }

def backtest_weighted(style_code, holdout_fraction=0.25, half_life_days=21):
    rows = get_daily_series(conn, style_code)
    n = len(rows)
    split_index = int(n * (1 - holdout_fraction))
    train_rows = rows[:split_index]
    test_rows = rows[split_index:]

    print(f"{style_code}: {n} total rows -> {len(train_rows)} train, {len(test_rows)} held out, half_life={half_life_days}d")

    first_date = rows[0][0]
    trend = weighted_linear_trend(train_rows, reference_date=first_date, half_life_days=half_life_days)

    if trend is None:
        print("  Not enough training data to fit.")
        return

    errors = []
    percent_errors = []
    for sale_date, avg_amount, orders in test_rows:
        days_from_reference = (sale_date - first_date).days
        predicted = trend["intercept"] + trend["slope_per_day"] * days_from_reference
        actual = float(avg_amount)
        error = predicted - actual
        errors.append(error)
        percent_errors.append(abs(error) / actual * 100)

    mae = statistics.mean(abs(e) for e in errors)
    mape = statistics.mean(percent_errors)
    bias = statistics.mean(errors)  # signed mean -- shows systematic over/under prediction
    print(f"  MAE:  ${mae:.2f}")
    print(f"  MAPE: {mape:.1f}%")
    print(f"  Mean signed error (bias): ${bias:+.2f}  (negative = model under-predicts, like the unweighted version)")

print("=== UNWEIGHTED (previous result for comparison) ===")
print("  MAE: $34.63  MAPE: 10.9%  (all but 2 of 25 errors were negative -- systematic under-prediction)\n")

print("=== WEIGHTED, half_life=21 days ===")
backtest_weighted('CT8012-005', half_life_days=21)

print("\n=== WEIGHTED, half_life=10 days (more aggressive recency) ===")
backtest_weighted('CT8012-005', half_life_days=10)

conn.close()
