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

def linear_trend(rows):
    if len(rows) < 2:
        return None

    first_date = rows[0][0]
    xs = [(row[0] - first_date).days for row in rows]
    ys = [float(row[1]) for row in rows]
    weights = [row[2] for row in rows]

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
        "n": n,
        "slope_per_day": slope,
        "intercept": intercept,
        "residual_stdev": statistics.stdev(residuals) if n > 2 else None,
        "days_span": max(xs) - min(xs),
        "total_orders": sum(weights),
    }

rows = get_daily_series(conn, 'CT8012-005')
print(f"Total daily rows: {len(rows)}")
print(f"Date range: {rows[0][0]} to {rows[-1][0]}")

result = linear_trend(rows)
print("\nTrend fit:")
for key, value in result.items():
    print(f"  {key}: {value}")

for days_ahead in [30, 60, 90]:
    projected = result["intercept"] + result["slope_per_day"] * (result["days_span"] + days_ahead)
    print(f"\nProjected avg_amount, {days_ahead} days from last observed date: ${projected:.2f}")

conn.close()
