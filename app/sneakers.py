"""Sneaker catalogue: two-stage search, and single-sneaker lookup.

Search runs in two stages, in this order:

  1. Substring  -- case-insensitive ILIKE '%q%' across name, style_code and
                   colorway. Answers a deliberate search literally.
  2. Fuzzy      -- trigram similarity, run ONLY when stage 1 returned zero
                   rows. Recovers from typos.

Why the fallback rather than fuzzy-always
-----------------------------------------
This catalogue is dense in shared name fragments: six sneakers contain
"Reimagined", three contain "Union LA", and "True Blue" appears on both a
Jordan 1 and a Jordan 3. Ranking every search by similarity would routinely
float a near-miss above the exact colourway the user typed. Falling back only
on zero results means fuzziness can never degrade a search that already
worked -- the cheap precise answer wins whenever it exists.

word_similarity(), not similarity()
-----------------------------------
similarity() is a Jaccard ratio over the WHOLE string, so it penalises length
difference: a short query against a long name scores badly just because the
name contributes trigrams the query cannot match. Measured on this catalogue,
'travis scot' vs 'Jordan 1 Low Travis Scott Reverse Mocha' scores 0.268 by
similarity() -- below pg_trgm's own 0.3 default, so a reasonable typo would
return NOTHING. word_similarity() scores the query against the best-matching
extent inside the text and gives 0.917 for the same pair. Using similarity()
here would ship a fallback that does not fall back.

Threshold provenance -- READ BEFORE CHANGING
--------------------------------------------
FUZZY_SIMILARITY_THRESHOLD = 0.4 was CALIBRATED, not assumed: it was measured
against real word_similarity() over this specific 50-Jordan catalogue, using
realistic typos ('jordn 4 bred', 'travis scot', 'Jordan 4 Bred Reimagened').

That calibration is only valid for this catalogue, which has heavy shared
vocabulary -- every name begins "Jordan", and colourway words repeat across
models. A broader catalogue would change the score distribution and the
threshold would need rechecking against real queries rather than carried over.
Treat 0.4 as provisional and measured, in the same spirit as the sample_size /
method / measured_date columns on platform_multipliers.

Known ceiling: no threshold rescues a single-word typo. 'jordn' matches all 50
rows at 0.4, 0.5 and 0.6, and returns zero at 0.7 -- but 0.7 also breaks
'jordn 4 bred'. There is no cutoff that separates them, so the result cap is
what keeps that case usable, not the threshold.

Style codes DO fuzzy-match, contrary to an earlier estimate
-----------------------------------------------------------
A pre-install Python simulation suggested one-digit style-code typos scored
~0.036 and were effectively unmatchable. Real pg_trgm disagrees: 'FV5029-007'
scores 0.818 against FV5029-006 (Jordan 4 Bred Reimagined), with the runner-up
far back at 0.333, and transposed digits ('DZ5485-160' -> DZ5485-106) score
0.727. word_similarity() finds the matching extent within the code rather than
being defeated by the hyphen split. The fuzzy stage is a usable SKU-typo
matcher as well as a name matcher. Recorded because the simulated figure was
wrong and would otherwise be rediscovered as a surprise.

Depends on the pg_trgm extension -- see the note at the top of schema.sql.
"""
from typing import Optional

# Max results returned per search. With 50 rows this is a UX guard rather than
# a performance one: a vague typo legitimately matches the whole catalogue
# (every shoe is a Jordan), and a wall of results is worse than the best 20.
SEARCH_RESULT_LIMIT = 20

# Minimum word_similarity a row must reach to appear in the fuzzy stage.
# See "Threshold provenance" above before changing this.
FUZZY_SIMILARITY_THRESHOLD = 0.4

# Matches RATIO_DP in app/valuation.py.
SCORE_DP = 3

# Selected by both stages and by the detail lookup, so the three can never
# drift apart in what they return.
_SNEAKER_COLUMNS = """
    id, name, brand, style_code, colorway, image_url, release_date, hype_tier
"""

# Ordering, most significant first:
#   1. exact style_code match, then name-starts-with, then anything else --
#      a searched SKU should outrank a shoe that merely mentions the text
#   2. hype_tier ascending (1 = most hyped) -- the likelier target first
#   3. name, then id -- alphabetical, and id makes ties fully deterministic
#      so results never reorder between identical requests
_SUBSTRING_SQL = f"""
SELECT {_SNEAKER_COLUMNS}
FROM sneakers
WHERE name       ILIKE %(pattern)s
   OR style_code ILIKE %(pattern)s
   OR colorway   ILIKE %(pattern)s
ORDER BY
    CASE
        WHEN style_code ILIKE %(exact)s  THEN 0
        WHEN name       ILIKE %(prefix)s THEN 1
        ELSE 2
    END,
    hype_tier,
    name,
    id
LIMIT %(limit)s;
"""

# The score is computed once in a CTE and then filtered and sorted on, rather
# than repeating the GREATEST(...) expression three times.
#
# GREATEST ignores NULLs, so a sneaker with a null colorway simply does not
# score on that column instead of poisoning the whole comparison.
#
# The threshold is passed as a bound parameter rather than using pg_trgm's
# `<%` operator, which reads its cutoff from a session GUC. app/db.py explains
# that DATABASE_URL points at Supabase's TRANSACTION pooler, where server-side
# session state does not survive between transactions -- so a session-level
# SET would be unreliable here by design. A parameter also keeps the cutoff
# visible in this file instead of hidden in connection state.
_FUZZY_SQL = f"""
WITH scored AS (
    SELECT {_SNEAKER_COLUMNS},
           GREATEST(
               word_similarity(%(q)s, name),
               word_similarity(%(q)s, style_code),
               word_similarity(%(q)s, colorway)
           ) AS match_score
    FROM sneakers
)
SELECT {_SNEAKER_COLUMNS}, match_score
FROM scored
WHERE match_score >= %(threshold)s
ORDER BY match_score DESC, hype_tier, name, id
LIMIT %(limit)s;
"""

_DETAIL_SQL = f"""
SELECT {_SNEAKER_COLUMNS}
FROM sneakers
WHERE id = %(sneaker_id)s;
"""


def _escape_like(value: str) -> str:
    """Escapes the characters that are WILDCARDS inside ILIKE.

    Parameterising the query stops SQL injection but does NOT stop this: `%`
    and `_` are meaningful to the pattern matcher, not to the SQL parser, so a
    user searching "a_b" would otherwise match "aXb". Backslash is escaped
    first, otherwise it would double-escape the backslashes added afterwards.

    Backslash is ILIKE's default escape character, so no ESCAPE clause is
    needed alongside this.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_sneaker(row) -> dict:
    """Maps one selected row onto the response shape. Column order here must
    match _SNEAKER_COLUMNS."""
    return {
        "id": row[0],
        "name": row[1],
        "brand": row[2],
        "style_code": row[3],
        "colorway": row[4],
        "image_url": row[5],
        "release_date": row[6],
        "hype_tier": row[7],
    }


def search_sneakers(conn, query: str, limit: int = SEARCH_RESULT_LIMIT) -> dict:
    """Runs the two-stage search. `query` is expected already stripped and
    non-empty -- the router rejects blank input before calling this.

    Fetches limit + 1 rows and returns at most `limit`. If the extra row comes
    back, there was more to show, which is what sets `truncated` -- one query,
    no separate COUNT.
    """
    escaped = _escape_like(query)
    fetch = limit + 1

    with conn.cursor() as cur:
        cur.execute(
            _SUBSTRING_SQL,
            {
                "pattern": f"%{escaped}%",
                "exact": escaped,
                "prefix": f"{escaped}%",
                "limit": fetch,
            },
        )
        rows = cur.fetchall()
        stage = "substring"
        scores = [None] * len(rows)

        # Stage 2 runs only on a completely empty stage 1.
        if not rows:
            cur.execute(
                _FUZZY_SQL,
                {
                    "q": query,
                    "threshold": FUZZY_SIMILARITY_THRESHOLD,
                    "limit": fetch,
                },
            )
            fuzzy_rows = cur.fetchall()
            stage = "fuzzy"
            rows = [r[:-1] for r in fuzzy_rows]
            scores = [round(r[-1], SCORE_DP) for r in fuzzy_rows]

    truncated = len(rows) > limit
    rows = rows[:limit]
    scores = scores[:limit]

    results = []
    for row, score in zip(rows, scores):
        sneaker = _row_to_sneaker(row)
        # Null on substring hits: those matched literally, so there is no
        # score to report. Present on fuzzy hits so a "did you mean" result
        # can be interrogated rather than trusted blind -- the same
        # auditability habit as returning q1/q3 alongside risk_pct.
        sneaker["match_score"] = score
        results.append(sneaker)

    return {
        "query": query,
        # "substring" when stage 1 found anything, otherwise "fuzzy" -- which
        # includes the case where both stages found nothing. The frontend
        # reads it as: stage == "fuzzy" and count > 0 -> "did you mean...";
        # count == 0 -> "no results".
        "stage": stage,
        "count": len(results),
        "truncated": truncated,
        "results": results,
    }


def get_sneaker(conn, sneaker_id: int) -> Optional[dict]:
    """Returns one sneaker's own fields, or None if the id does not exist.

    Catalogue data only -- no valuation. See the note in
    app/routers/sneakers.py on why the two are separate endpoints.
    """
    with conn.cursor() as cur:
        cur.execute(_DETAIL_SQL, {"sneaker_id": sneaker_id})
        row = cur.fetchone()

    return None if row is None else _row_to_sneaker(row)
