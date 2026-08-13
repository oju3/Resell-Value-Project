"""GOAT median from market_sales, reported alongside eBay's median.

Deliberately self-contained. The eBay engine in app/valuation.py fences in SQL
(_FENCE_CTE), bound to sold_comps/sold_price and not parameterisable onto
another table. Rather than refactor a working, tested query, this module
DUPLICATES the fencing arithmetic in plain Python, scoped to GOAT only.

That duplication is the deliberate trade, and it has a cost worth stating: the
1.5 IQR factor now exists in two implementations. Change one and the other must
change with it, or the two medians stop being comparable -- which is the whole
point of reporting them side by side.

Verified, not assumed: statistics.quantiles(method="inclusive") uses the same
linear interpolation as Postgres percentile_cont. Checked against percentile_cont
on real market_sales data, 5 sneakers at n=100-200, max difference 0.0000000000.

NOT a blended number. goat_median is its own labelled figure. It is never
averaged with, nor substituted for, the eBay median.
"""
import statistics
from typing import Optional

# Same factor as the eBay fence in app/valuation.py. Restated here rather than
# imported: this module must not depend on that module, and a shared import
# would imply a shared implementation that does not exist.
IQR_FENCE_FACTOR = 1.5

# Matches PRICE_DP in app/valuation.py so both medians round identically.
PRICE_DP = 2

# Only USD rows feed the median. All 4,137 rows are USD today, so this changes
# nothing now; it exists so a future non-USD amount cannot be silently averaged
# into a USD median and produce a meaningless number. Excluded rows simply do
# not count toward goat_sample_size.
MEDIAN_CURRENCY = "USD"

_GOAT_AMOUNTS_SQL = """
SELECT amount
FROM market_sales
WHERE sneaker_id = %(sneaker_id)s
  AND currency = %(currency)s
  AND amount IS NOT NULL;
"""


def _fenced_median(amounts):
    """Median after 1.5*IQR fencing. Returns (median, kept_count).

    Same two-stage order as the eBay engine: quartiles from the UNFILTERED
    values, fence built from those, median over the survivors only.

    (None, 0) for an empty list. A single value is returned as-is -- quartiles
    are undefined at n=1 and statistics.quantiles raises rather than returning
    anything usable.
    """
    if not amounts:
        return None, 0
    if len(amounts) == 1:
        return amounts[0], 1

    q1, _, q3 = statistics.quantiles(amounts, n=4, method="inclusive")
    iqr = q3 - q1
    lower = q1 - IQR_FENCE_FACTOR * iqr
    upper = q3 + IQR_FENCE_FACTOR * iqr

    kept = [a for a in amounts if lower <= a <= upper]
    if not kept:
        return None, 0
    return statistics.median(kept), len(kept)


def get_goat_median(conn, sneaker_id: int) -> dict:
    """Fenced GOAT median for one sneaker. {"goat_median", "goat_sample_size"}.

    goat_median is None -- never 0, never omitted -- when there is nothing to
    measure. Two causes collapse into that None: the sneaker has no
    goat_product_id (7 of 50), or it has one and market_sales holds no rows for
    it. Both mean "unmeasured", matching how goat_product_id stores absence.

    goat_sample_size is the POST-fence count, matching what comp_count_used
    means on the eBay side, so a thin sample (33) is distinguishable from a
    thick one (200) without inventing a confidence label.
    """
    with conn.cursor() as cur:
        cur.execute(_GOAT_AMOUNTS_SQL,
                    {"sneaker_id": sneaker_id, "currency": MEDIAN_CURRENCY})
        amounts = [float(row[0]) for row in cur.fetchall()]

    median, kept_count = _fenced_median(amounts)
    return {
        "goat_median": None if median is None else round(median, PRICE_DP),
        "goat_sample_size": kept_count,
    }
