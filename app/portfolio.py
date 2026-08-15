"""Portfolio: pairs a user owns, pairs they have sold, and the P/L on both.

AUTHORIZATION -- THE MOST IMPORTANT THING IN THIS FILE
------------------------------------------------------
Every statement here against owned_sneakers and sold_sneakers carries an
explicit `WHERE user_id = %s`. Row Level Security does NOT protect this path:
the policies are written as `auth.uid() = user_id`, auth.uid() reads JWT claims
from Postgres session settings that only Supabase's PostgREST layer populates,
and this API connects straight to Postgres with psycopg2. auth.uid() is NULL
here and the connection role bypasses RLS besides. A query missing its filter
returns every user's rows, does not error, and looks correct when tested with a
single account. See app/db.py::get_conn.

The ownership check is never a separate SELECT. It is part of the statement
that reads or changes the row, so there is no gap between checking and acting.

Realized vs unrealized P/L -- a deliberate asymmetry
-----------------------------------------------------
realized_pl   = sale_price - fee_amount - purchase_price     (NET of fees)
unrealized_pl = current_valuation - purchase_price           (GROSS, pre-fee)

Realized P/L knows which platform the sale happened on, so its fee is a fact.
Unrealized P/L does not: fees differ enough between platforms (a $100 sale
costs $13.55 on eBay, $17.40 on GOAT) that picking one would be inventing a
number. Choosing the platform is the recommendation engine's job, and that does
not exist yet -- so the unrealized figure is labelled `unrealized_pl_gross` in
the field name itself, not merely in documentation.

A net unrealized figure lands once the recommendation engine can pick a
platform. Until then this asymmetry is known and intentional, not an oversight.

Money arithmetic
----------------
Realized P/L is computed in SQL as NUMERIC -- exact decimal arithmetic, no
binary-float rounding on currency. Unrealized P/L is computed in Python as
float, because the valuation median arrives from percentile_cont as double
precision. That is acceptable precisely because it is a statistical estimate
rather than a ledger entry; the realized figure, which is a record of money
that actually moved, never touches float.

Note also that psycopg2 returns NUMERIC as Decimal and percentile_cont as
float, and Python raises TypeError on `Decimal - float`. Values are converted
at the boundary rather than mixed.
"""
from typing import Optional

from app.fees import get_fee_for_price
from app.valuation import get_values_for_sneakers

# Mirrors the CHECK constraints in schema.sql. The router's Pydantic models
# restate these as Literal types so a bad value is rejected as a 422 before
# reaching the database; the CHECK is the backstop for any other writer.
PURCHASE_SOURCES = ("snkrs", "stockx", "goat", "ebay", "in_store", "other")
SALE_PLATFORMS = ("ebay", "stockx", "goat")

MONEY_DP = 2


# The banded fee lookup now lives in app/fees.py::get_fee_for_price, imported
# above. It moved out of this file when app/recommendation.py became a second
# caller -- two copies of the half-open band logic would drift apart, and the
# $150 boundary bug it guards against only shows up at exactly one price.
# Behaviour here is unchanged: the same SQL, the same half-open interval, and
# the same raw Decimal values fed into the NUMERIC arithmetic below.

_ADD_PAIR_SQL = """
INSERT INTO owned_sneakers
    (user_id, sneaker_id, size, purchase_price, purchase_date, purchase_source)
VALUES
    (%(user_id)s, %(sneaker_id)s, %(size)s, %(purchase_price)s,
     %(purchase_date)s, %(purchase_source)s)
RETURNING id, sneaker_id, size, purchase_price, purchase_date, purchase_source;
"""

_LIST_PORTFOLIO_SQL = """
SELECT o.id, o.sneaker_id, s.name, s.style_code,
       o.size, o.purchase_price, o.purchase_date, o.purchase_source
FROM owned_sneakers o
JOIN sneakers s ON s.id = o.sneaker_id
WHERE o.user_id = %(user_id)s
ORDER BY o.purchase_date DESC NULLS LAST, o.id DESC;
"""

# The user_id filter is what makes this safe -- without it this deletes any
# user's row by id. RETURNING lets the caller tell "deleted" from "not yours
# or does not exist" without a prior SELECT.
_DELETE_PAIR_SQL = """
DELETE FROM owned_sneakers
WHERE id = %(owned_id)s AND user_id = %(user_id)s
RETURNING id;
"""

# The atomic claim in mark_sold. This single statement does three jobs:
# authorization (AND user_id), existence checking, and removal -- and hands
# back the values the sold_sneakers row needs, since this row is about to stop
# existing. It also closes the double-sell race for free: two concurrent
# requests, only one DELETE can match the row, the loser gets zero rows and a
# 404. No SELECT ... FOR UPDATE needed; the row lock the DELETE already takes
# does the work.
_CLAIM_PAIR_SQL = """
DELETE FROM owned_sneakers
WHERE id = %(owned_id)s AND user_id = %(user_id)s
RETURNING sneaker_id, size, purchase_price, purchase_date, purchase_source;
"""

# Fee and P/L are computed here in NUMERIC and STORED, not derived on read.
# platform_fees is mutable -- this repo has reseeded it twice -- so a derived
# figure would retroactively change what a past sale earned.
#
# fee_amount is rounded before realized_pl subtracts it, so the stored P/L is
# consistent with the fee figure the user is shown rather than with an
# unrounded intermediate.
_INSERT_SALE_SQL = """
WITH fee AS (
    SELECT ROUND(
        %(sale_price)s::numeric * %(fee_percent)s::numeric / 100
        + %(fixed_fee)s::numeric, 2
    ) AS fee_amount
)
INSERT INTO sold_sneakers
    (user_id, sneaker_id, size, purchase_price, purchase_date, purchase_source,
     sale_price, sale_platform, sale_date,
     fee_percent_applied, fixed_fee_applied, fee_amount, realized_pl)
SELECT
    %(user_id)s, %(sneaker_id)s, %(size)s, %(purchase_price)s,
    %(purchase_date)s, %(purchase_source)s,
    %(sale_price)s, %(sale_platform)s, %(sale_date)s,
    %(fee_percent)s, %(fixed_fee)s,
    fee.fee_amount,
    ROUND(%(sale_price)s::numeric - fee.fee_amount - %(purchase_price)s::numeric, 2)
FROM fee
RETURNING id, sneaker_id, size, purchase_price, purchase_date, purchase_source,
          sale_price, sale_platform, sale_date,
          fee_percent_applied, fixed_fee_applied, fee_amount, realized_pl, sold_at;
"""

_LIST_SALES_SQL = """
SELECT sold.id, sold.sneaker_id, s.name, s.style_code, sold.size,
       sold.purchase_price, sold.purchase_date, sold.purchase_source,
       sold.sale_price, sold.sale_platform, sold.sale_date,
       sold.fee_percent_applied, sold.fixed_fee_applied,
       sold.fee_amount, sold.realized_pl, sold.sold_at
FROM sold_sneakers sold
JOIN sneakers s ON s.id = sold.sneaker_id
WHERE sold.user_id = %(user_id)s
ORDER BY sold.sale_date DESC, sold.id DESC;
"""


def _money(value) -> Optional[float]:
    """Decimal (or None) -> float for the JSON response. Money is kept as
    NUMERIC for every calculation and only converted here, at the edge."""
    return None if value is None else float(value)


def add_pair(conn, user_id, sneaker_id, size, purchase_price,
             purchase_date, purchase_source) -> dict:
    """Records a pair the user owns.

    No uniqueness check: owning several of the same sneaker in different sizes
    or at different prices is normal, so each add is its own row.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(_ADD_PAIR_SQL, {
                "user_id": user_id,
                "sneaker_id": sneaker_id,
                "size": size,
                "purchase_price": purchase_price,
                "purchase_date": purchase_date,
                "purchase_source": purchase_source,
            })
            row = cur.fetchone()

    return {
        "id": row[0],
        "sneaker_id": row[1],
        "size": row[2],
        "purchase_price": _money(row[3]),
        "purchase_date": row[4],
        "purchase_source": row[5],
    }


def list_portfolio(conn, user_id) -> dict:
    """The user's unsold pairs, each with its current valuation and gross P/L.

    Two queries total regardless of how many pairs there are: one for the rows,
    one batched valuation for their distinct sneakers. Never N x 3.
    """
    with conn.cursor() as cur:
        cur.execute(_LIST_PORTFOLIO_SQL, {"user_id": user_id})
        rows = cur.fetchall()

    values = get_values_for_sneakers(conn, {r[1] for r in rows})

    items = []
    for (owned_id, sneaker_id, name, style_code, size,
         purchase_price, purchase_date, purchase_source) in rows:
        # Absent from `values` means the sneaker has no comps at all. That is
        # "no valuation available", never zero -- so the P/L stays null rather
        # than reporting the purchase price back as a loss.
        valuation = values.get(sneaker_id)
        current_value = valuation["value"] if valuation else None

        unrealized = None
        if current_value is not None and purchase_price is not None:
            unrealized = round(current_value - float(purchase_price), MONEY_DP)

        items.append({
            "id": owned_id,
            "sneaker_id": sneaker_id,
            "name": name,
            "style_code": style_code,
            "size": size,
            "purchase_price": _money(purchase_price),
            "purchase_date": purchase_date,
            "purchase_source": purchase_source,
            "current_value": current_value,
            # Named gross in the field, not just the docs, so a frontend cannot
            # present a pre-fee number as a net one.
            "unrealized_pl_gross": unrealized,
            "valuation_low_confidence": valuation["low_confidence"] if valuation else None,
        })

    return {"count": len(items), "items": items}


def delete_pair(conn, user_id, owned_id) -> bool:
    """Hard-deletes one of the user's pairs. True if a row was removed.

    Hard rather than soft: sold pairs are already preserved in sold_sneakers, so
    deleting an OWNED pair means "I entered this wrong", not "I disposed of it".
    A soft-delete flag would reintroduce exactly the status-column pattern this
    design avoids, and would then require every query to remember
    `AND deleted_at IS NULL` -- one forgotten filter and deleted pairs reappear.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(_DELETE_PAIR_SQL, {"owned_id": owned_id, "user_id": user_id})
            return cur.fetchone() is not None


def mark_sold(conn, user_id, owned_id, sale_price, sale_platform, sale_date):
    """Moves a pair from owned_sneakers to sold_sneakers, computing realized P/L.

    Returns the sale dict, or None if the pair does not exist or is not this
    user's, or the string "no_fee_band" if platform_fees has no band covering
    this platform and price.

    ATOMICITY
    ---------
    Everything runs inside one `with conn:` block. psycopg2's connection
    context manager is a TRANSACTION boundary, not a connection one -- it
    commits on clean exit and rolls back on any exception, and does not close
    the connection, which is what makes it safe with the pool in app/db.py.
    So the delete and the insert either both land or neither does: a partial
    failure cannot leave the pair in both tables or in neither.

    The fee band is looked up BEFORE the delete on purpose. Doing it after --
    or folding the whole thing into one statement with a CTE -- would let an
    unmatched band delete the pair and insert nothing, since a DELETE inside a
    CTE still executes when its output is joined away.

    (app/db.py's get_conn calls conn.rollback() in its finally block after the
    endpoint returns. That runs after this has already committed, so it acts on
    a fresh empty transaction and is a no-op. It is not undoing this write.)
    """
    with conn:
        with conn.cursor() as cur:
            band = get_fee_for_price(conn, sale_price, sale_platform)
            if band is None:
                return "no_fee_band"
            fee_percent, fixed_fee = band

            cur.execute(_CLAIM_PAIR_SQL, {"owned_id": owned_id, "user_id": user_id})
            claimed = cur.fetchone()
            if claimed is None:
                # Nothing was deleted, so committing this transaction commits
                # no changes. The caller turns this into a 404.
                return None
            sneaker_id, size, purchase_price, purchase_date, purchase_source = claimed

            cur.execute(_INSERT_SALE_SQL, {
                "user_id": user_id,
                "sneaker_id": sneaker_id,
                "size": size,
                "purchase_price": purchase_price,
                "purchase_date": purchase_date,
                "purchase_source": purchase_source,
                "sale_price": sale_price,
                "sale_platform": sale_platform,
                "sale_date": sale_date,
                "fee_percent": fee_percent,
                "fixed_fee": fixed_fee,
            })
            row = cur.fetchone()

    return _sale_row_to_dict(row)


def list_sales(conn, user_id) -> dict:
    """The user's sales history with realized P/L.

    Every figure here was frozen at sale time, so this endpoint reads history
    rather than recomputing it -- changing platform_fees tomorrow will not move
    a number on this page.
    """
    with conn.cursor() as cur:
        cur.execute(_LIST_SALES_SQL, {"user_id": user_id})
        rows = cur.fetchall()

    items = [{
        "id": r[0],
        "sneaker_id": r[1],
        "name": r[2],
        "style_code": r[3],
        "size": r[4],
        "purchase_price": _money(r[5]),
        "purchase_date": r[6],
        "purchase_source": r[7],
        "sale_price": _money(r[8]),
        "sale_platform": r[9],
        "sale_date": r[10],
        "fee_percent_applied": _money(r[11]),
        "fixed_fee_applied": _money(r[12]),
        "fee_amount": _money(r[13]),
        "realized_pl": _money(r[14]),
        "sold_at": r[15],
    } for r in rows]

    return {"count": len(items), "items": items}


def _sale_row_to_dict(row) -> dict:
    """Shapes the RETURNING row from _INSERT_SALE_SQL. Column order must match."""
    return {
        "id": row[0],
        "sneaker_id": row[1],
        "size": row[2],
        "purchase_price": _money(row[3]),
        "purchase_date": row[4],
        "purchase_source": row[5],
        "sale_price": _money(row[6]),
        "sale_platform": row[7],
        "sale_date": row[8],
        "fee_percent_applied": _money(row[9]),
        "fixed_fee_applied": _money(row[10]),
        "fee_amount": _money(row[11]),
        "realized_pl": _money(row[12]),
        "sold_at": row[13],
    }
