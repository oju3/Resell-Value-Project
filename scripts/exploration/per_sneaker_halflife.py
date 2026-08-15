import os
import psycopg2
from dotenv import load_dotenv
import statistics
import math

load_dotenv('.env')
conn = psycopg2.connect(os.environ['DATABASE_URL'])

CANDIDATE_HALF_LIVES = [30, 21, 14, 10, 7, 5, 3, 2]

def get_daily_series(conn, sneaker_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sale_date, avg_amount, orders
            FROM goat_daily_sales
            WHERE sneaker_id = %s
            ORDER BY sale_date;
        """, (sneaker_id,))
        return cur.fetchall()

def weighted_linear_trend(rows, reference_date, half_life_days):
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

def backtest_at_halflife(rows, half_life_days, holdout_fraction=0.25):
    n = len(rows)
    if n < 8:
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

with conn.cursor() as cur:
    cur.execute("""
        SELECT s.id, s.style_code, s.name
        FROM sneakers s
        WHERE s.goat_product_id IS NOT NULL
        ORDER BY s.style_code;
    """)
    sneakers = cur.fetchall()

print(f"Sweeping {len(sneakers)} sneakers...\n")
results_summary = []
for sneaker_id, style_code, name in sneakers:
    rows = get_daily_series(conn, sneaker_id)
    best_hl, best_mape = find_best_halflife(rows)
    if best_hl is None:
        print(f"  [{style_code}] insufficient data for backtest ({len(rows)} rows) — skipped")
        continue
    print(f"  [{style_code}] n={len(rows):<4} best_half_life={best_hl:<3}d  MAPE={best_mape:.1f}%   {name}")
    results_summary.append((style_code, best_hl, best_mape))

print(f"\nCompleted: {len(results_summary)}/{len(sneakers)} sneakers got a tuned half-life")
if results_summary:
    avg_mape = statistics.mean(r[2] for r in results_summary)
    print(f"Average best-fit MAPE across all sneakers: {avg_mape:.1f}%")

conn.close()
