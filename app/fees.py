"""Banded platform fee lookup. One implementation, several callers.

platform_fees stores a fee schedule per (platform, price band) -- eBay charges
13.25% + $0.30 below $150 and 8% + $0 at or above it -- so the fee depends on
the SALE PRICE, not on the platform alone.

Extracted from app/portfolio.py, where this SQL previously sat inline inside
mark_sold(). It moved here the moment a second caller needed it: two copies of
band logic drift apart, and this project has already hit that failure mode
(the fence arithmetic duplicated between app/valuation.py's single-sneaker and
batch queries, which now has to be kept in sync by hand).

THE BOUNDARY IS HALF-OPEN ON PURPOSE: >= min_price AND < max_price.
--------------------------------------------------------------------
The bands as seeded are [0, 150] and [150, NULL] -- they TOUCH at 150. The
intuitive rule `price >= min_price AND price <= max_price` therefore matches
BOTH eBay rows at exactly $150.00 (verified against live data: 2 rows, 13.25%
and 8.00%), and which one wins is whatever the planner returns first. That is
a real bug that appears at exactly one price point and reads as flakiness.

Half-open intervals fix it the standard way: every price falls in exactly one
band, bands tile the number line with no gap and no overlap. At $150 the 8%
band wins. Confirmed at $0, $149.99, $150.00, $150.01 and $5000.

max_price IS NULL means unbounded -- how StockX and GOAT have a single band.
Only eBay is actually banded today: goat and stockx each have one row spanning
[0, infinity), so a price can never cross a band boundary on those platforms.
The banded lookup is still the right call for them -- it costs nothing and
keeps working if they ever gain bands.
"""

_FEE_BAND_SQL = """
SELECT fee_percent, fixed_fee
FROM platform_fees
WHERE platform = %(platform)s
  AND %(price)s >= min_price
  AND (max_price IS NULL OR %(price)s < max_price);
"""


def get_fee_for_price(conn, price, platform):
    """Returns (fee_percent, fixed_fee) for this platform at this price.

    Returns None when no band covers the price -- a server-side data gap, not
    a caller error. Every caller must handle that rather than assuming a fee.

    Values are returned EXACTLY as the database supplies them (Decimal for
    NUMERIC columns), not coerced to float. app/portfolio.py feeds them
    straight back into NUMERIC arithmetic when recording a sale, and rounding
    them through binary float on the way would put representation error into
    money that actually moved. Callers that want floats convert at their own
    boundary.
    """
    with conn.cursor() as cur:
        cur.execute(_FEE_BAND_SQL, {"platform": platform, "price": price})
        return cur.fetchone()
