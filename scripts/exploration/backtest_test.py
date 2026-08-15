import os
import psycopg2
from dotenv import load_dotenv
import statistics

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

def linear_trend(rows, reference_date=None):
    """Fits on the given rows. reference_date lets us re-use day-0 from the
    FULL series when fitting on a subset, so day numbers stay comparable
    between the training fit and the real held-out dates."""
    if len(rows) < 2:
        return None
    first_date = reference_date if reference_date else rows[0][0]
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
    }

def backtest(style_code, holdout_fraction=0.25):
    rows = get_daily_series(conn, style_code)
    n = len(rows)
    split_index = int(n * (1 - holdout_fraction))

    train_rows = rows[:split_index]
    test_rows = rows[split_index:]

    print(f"{style_code}: {n} total rows -> {len(train_rows)} train, {len(test_rows)} held out")

    first_date = rows[0][0]  # reference date from the FULL series, so day
                              # numbers line up between train fit and test rows
    trend = linear_trend(train_rows, reference_date=first_date)

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
        print(f"  {sale_date}  actual=${actual:.2f}  predicted=${predicted:.2f}  error=${error:+.2f}")

    mae = statistics.mean(abs(e) for e in errors)
    mape = statistics.mean(percent_errors)
    print(f"\n  MAE (mean absolute error):  ${mae:.2f}")
    print(f"  MAPE (mean % error):        {mape:.1f}%")

backtest('CT8012-005')
conn.close()
