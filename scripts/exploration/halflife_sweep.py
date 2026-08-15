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

def weighted_linear_trend(rows, reference_date, half_life_days):
    first_date = reference_date
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
    return {"slope_per_day": slope, "intercept": intercept}

def backtest_at_halflife(style_code, half_life_days, holdout_fraction=0.25):
    rows = get_daily_series(conn, style_code)
    n = len(rows)
    split_index = int(n * (1 - holdout_fraction))
    train_rows = rows[:split_index]
    test_rows = rows[split_index:]
    first_date = rows[0][0]

    trend = weighted_linear_trend(train_rows, first_date, half_life_days)
    if trend is None:
        return None

    errors = []
    percent_errors = []
    for sale_date, avg_amount, orders in test_rows:
        days_from_reference = (sale_date - first_date).days
        predicted = trend["intercept"] + trend["slope_per_day"] * days_from_reference
        actual = float(avg_amount)
        error = predicted - actual
        errors.append(error)
        percent_errors.append(abs(error) / actual * 100)

    return {
        "mae": statistics.mean(abs(e) for e in errors),
        "mape": statistics.mean(percent_errors),
        "bias": statistics.mean(errors),
    }

for hl in [21, 10, 7, 5, 3]:
    result = backtest_at_halflife('CT8012-005', hl)
    print(f"half_life={hl:>2}d   MAE=${result['mae']:.2f}   MAPE={result['mape']:.1f}%   bias=${result['bias']:+.2f}")

conn.close()
