# Phase 6 handoff — React frontend

Written 2026-08-16 against commit `428cb2d` (clean working tree, `main`).
Every figure below was queried live from Supabase or read out of the source on
that date, not recalled. Where a number drifts with time or with the next
refresh run, that is said explicitly.

Sneaker resale valuation app. FastAPI + Supabase Postgres, psycopg2 with raw
SQL, no ORM. The backend endpoints listed in §2 are built and working. Phase 6
is the React frontend.

> Naming note: `docs/roadmap.md` numbers the frontend as **Phase 4** and launch
> as Phase 5. This file uses "Phase 6" because that is what the work is being
> called in conversation. Same work, different counter — don't go looking for a
> Phase 6 section in the roadmap.

---

## 1. Data state

Live row counts, all 16 tables in `schema.sql`:

| table | rows | | table | rows |
|---|---|---|---|---|
| `sneakers` | 50 | | `market_sales` | 4137 |
| `sold_comps` | 1030 | | `goat_daily_sales` | 3215 |
| `comp_rejections` | 69 | | `sneaker_projections` | 43 |
| `platform_fees` | 4 | | `platform_multipliers` | 3 |
| `condition_multipliers` | 6 | | `refresh_runs` | 26 |
| `owned_sneakers` | 0 | | `sold_sneakers` | 0 |
| `price_history` | 0 | | `sales_velocity` | 0 |
| `lifecycle_curves` | 0 | | `projections` | 0 |

The four zero tables at bottom-left/right are declared but unused — `projections`
was superseded by `sneaker_projections`, and the other three were never built
out. `condition_multipliers` has 6 seeded rows that **no code reads** (see the
deadstock note in §4).

Portfolio tables are empty: nothing has ever been added or sold. The portfolio
endpoints work but have never run against real rows, so the frontend will be the
first thing to exercise them.

Data coverage windows:

- `sold_comps.ended_at` — 2026-05-04 to 2026-08-03
- `market_sales.purchased_at` — 2026-04-19 to 2026-08-13
- `goat_daily_sales.sale_date` — 2026-04-19 to 2026-08-13
- All 4137 `market_sales` rows are USD.
- **`image_url` is NULL on all 50 sneakers.** No image pipeline exists. The
  frontend needs a placeholder strategy, not a fallback for the odd missing one.

---

## 2. Endpoints

Every endpoint requires `Depends(get_current_user_id)` except `/health`.

```
GET    /health                              (unauthenticated)
GET    /me
GET    /sneakers/search?q=
GET    /sneakers/{id}
GET    /sneakers/{id}/valuation
GET    /sneakers/{id}/projection
GET    /sneakers/{id}/recommendation
POST   /portfolio
GET    /portfolio
GET    /portfolio/sales
DELETE /portfolio/{owned_id}
POST   /portfolio/{owned_id}/sell
```

**Auth**: Supabase JWT, `Authorization: Bearer <token>`, verified locally against
Supabase's published JWKS (ES256/RS256 only — `app/auth.py` pins asymmetric
algorithms to block the HS256 confusion attack). Get a dev token with:

```bash
TOKEN=$(.venv/bin/python scripts/get_test_token.py)   # interactive, uses getpass
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/me
```

Run the API with `.venv/bin/uvicorn app.main:app --reload`; docs at `/docs`.

Auth failure codes are deliberately distinguished and the frontend should treat
them differently:

| code | meaning | client action |
|---|---|---|
| 401 `"Token has expired"` | valid token, past `exp` | refresh and retry |
| 401 `"Invalid authentication credentials"` | forged/malformed/wrong issuer | send to login |
| 401 `"Not authenticated"` | no header at all | send to login |
| 503 | Supabase JWKS unreachable | retryable, not the user's fault |

Local verification cannot detect a token revoked before it expired. Supabase
tokens live about an hour, which bounds that.

---

## 3. Architectural decisions that constrain the frontend

- **RLS is NOT load-bearing.** `auth.uid()` is NULL over psycopg2, and the
  connection role bypasses RLS besides. Every `owned_sneakers` / `sold_sneakers`
  query carries an explicit `WHERE user_id = %s` in application code. See
  `app/db.py::get_conn`. Do not assume a refactor changed this — check there first.
- **Deadstock-only MVP.** No condition multiplier is applied anywhere;
  `condition_multipliers` is unread. There is no condition input on the add-pair
  flow by design. See `docs/scope_deadstock_only.md`.
- **Two medians, never blended.** `ebay_median` (from `sold_comps`, IQR-fenced in
  SQL) and `goat_median` (from `market_sales`, IQR-fenced in Python) are separate
  labelled fields measuring different markets. Never average them, never
  substitute one for the other, never present either as "the" value.
- **404 = the id doesn't exist. 200 = it exists but there's no data.** Never
  conflated, on any endpoint. "No data yet" is a valid answer, not an error, and
  the frontend must render it as such rather than as a failure state.
- **Absent vs null is meaningful.** Suppressed projections **omit** the price keys
  entirely (not `0`, not `null`) — a suppressed sneaker showing "bear: 0" would
  read as a prediction of worthlessness. No-verdict recommendations use `null` in
  always-present slots, because those are stable fields whose value is unknown.
- One router file per concern. **Route order is load-bearing** where paths
  collide: `/sneakers/search` must stay declared above `/sneakers/{sneaker_id}`,
  and `/portfolio/sales` above `/portfolio/{owned_id}`.
- Money is NUMERIC in SQL, converted to float only at the JSON boundary.
- Rounding is consistent across endpoints: prices 2dp, percentages 1dp, ratios
  3dp, rates 2dp.

---

## 4. Projection and recommendation state

`sneaker_projections` covers **43 of 50** sneakers. Overall average MAPE **10.4%**.

| tier | n | avg MAPE | `n_rows` range |
|---|---|---|---|
| `normal` | 36 | 7.9% | 22–100 |
| `low_confidence` | 5 | 18.2% | 43–87 |
| `suppressed` | 2 | 34.9% | 34–57 |

Tier cutoffs (`scripts/build_sneaker_projections.py`): MAPE ≤ 15 → `normal`,
≤ 25 → `low_confidence`, above → `suppressed`. A fourth tier value
`insufficient_data` exists in the table's vocabulary but no row currently holds it.

Winning half-lives across the 43: `{30d: 17, 3d: 7, 2d: 5, 5d: 5, 21d: 3, 7d: 2,
10d: 2, 14d: 2}`. Candidates swept were `[30, 21, 14, 10, 7, 5, 3, 2]`.

The 7 non-`normal` sneakers, worst first:

| style code | name | tier | MAPE |
|---|---|---|---|
| HV6674-067 | Jordan 1 High '85 Bred | suppressed | 43.7% |
| DC9533-800 | Jordan 4 Union LA Guava Ice | suppressed | 26.0% |
| FD6812-400 | Jordan 5 Midnight Navy | low_confidence | 22.9% |
| AR0715-101 | Jordan 11 Neapolitan (W) | low_confidence | 18.8% |
| DZ5485-410 | Jordan 1 High True Blue | low_confidence | 18.0% |
| CZ0790-003 | Jordan 1 Low Shadow Reimagined | low_confidence | 16.2% |
| CT8532-401 | Jordan 3 Georgetown | low_confidence | 15.3% |

**Recommendation outcomes across all 50, at the fixed 30-day horizon:**

| outcome | n |
|---|---|
| `SELL` | 26 |
| `HOLD` | 15 |
| no verdict — `no_goat_data` | 7 |
| no verdict — `insufficient_data_for_recommendation` | 2 |

A third reason, `invalid_projection` (net projected proceeds ≤ 0), exists in the
code and is unreachable on today's data at 30 days.

`low_confidence` sneakers **do** get a verdict, with the tier in the response —
flag it visually rather than hiding it. Suppressing the tier would hide exactly
what the tier exists to expose.

---

## 5. The 9 sneakers with an eBay median but no projection

This is the single most important shape for the frontend to handle, and it is
**9 sneakers, not 7**. All 50 sneakers have a non-null `ebay_median`, so valuation
always works — but 9 of them return `"horizons": []` from `/projection` and a
null `recommendation`.

Two different causes, and they produce different `reason` values:

**(a) 7 sneakers with `goat_product_id IS NULL`** → no `market_sales`, no
`goat_daily_sales`, no `sneaker_projections` row, `confidence_tier: null`,
`reason: "no_goat_data"`:

```
CT8012-116  Jordan 11 Cherry
CV9388-100  Jordan 4 Off-White Sail (W)
DM9014-003  Jordan 5 Green Bean
DN3707-100  Jordan 3 White Cement Reimagined
DZ5485-106  Jordan 1 High Black Toe Reimagined
DZ5485-612  Jordan 1 High Chicago Lost & Found
IF4396-104  Jordan 3 True Blue
```

Cause: KicksDB's `/v3/unified/products/{sku}` returned no entry with
`shop_name == "goat"` for these SKUs. `scripts/fetch_goat_product_ids.py` has no
fallback lookup and does not investigate why — a deliberate scope boundary.

**(b) 2 suppressed sneakers** (HV6674-067, DC9533-800) → these DO have GOAT data
and a real `goat_median`, but `reason: "insufficient_data_for_recommendation"`.

So the frontend needs a sneaker page that renders a valuation, no projection
chart, and a "no call" recommendation panel — and the panel's copy differs by
reason. Don't key off `goat_median == null` alone; that only catches case (a).

---

## 6. Known limitations — documented, do not "discover" and fix blind

- **The Monte Carlo band is constant across horizons.** Uncertainty is applied
  once at the target date, not compounded, so `bear`/`bull` sit at
  `expected ± 1.2816 × residual_stdev` at 30, 60 and 90 days alike. A 90-day
  projection is **not** shown as more uncertain than a 30-day one. Do not render
  the band as a widening cone — the data does not support that shape. (Compounding
  it was a real bug during development; it produced negative prices.)
- **`THRESHOLD_PERCENT_GAIN = 5.0` is unvalidated.** It is the only number in the
  pipeline with no evidence behind it. This project has never observed a HOLD/SELL
  outcome, so there is nothing to backtest it against. The half-lives, the
  confidence tiers, and the fuzzy-search threshold were all measured against real
  data. Do not present the HOLD/SELL verdict with the same authority as the
  valuation or MAPE figures.
- **Table-grain mismatch inside the recommendation.** `net_now` derives from
  `goat_median` (`market_sales`, individual transactions); `net_projected` derives
  from a fit on `goat_daily_sales` (GOAT's own daily averages). Evaluating the fit
  at day 0 against `goat_median` gives a residual ratio of 0.85–1.14 (median 1.02,
  32 of 41 within 10%). At 30 days, where the median gain is only ~1.7%, a few
  percent of `percent_gain` may be grain mismatch rather than real trend.
- **The KicksDB endpoints are row-capped.** Verified live: `market_sales` tops out
  at exactly 200 rows for a sneaker (min 33, avg 96.2) and `goat_daily_sales` at
  exactly 100 (min 22, avg 74.8). `MIN(date)` therefore means "as far back as the
  API went", not "first sale ever". Don't label it as first-ever anywhere in the UI.
- **`platform_multipliers` is weak.** StockX 1.50 is n=12, one observation per
  shoe, `confidence='low'`. GOAT 1.50 is `is_proxy=true`, copied from the StockX
  row, never measured. eBay 1.00 is definitional. Nothing in the API reads this
  table currently.
- **Only eBay is fee-banded.** eBay: 13.25% + $0.30 below $150, 8% + $0 at or
  above. GOAT: 12.4% + $5.00, single band. StockX: 12.0% + $0, single band. The
  boundary is half-open (`>= min AND < max`), so at exactly $150.00 the 8% band
  wins.
- **`trend_ratio` is null for 12 of 50 sneakers today, and `size_breakdown` is
  empty for 16.** Note that `app/valuation.py`'s docstring still says "21 of 50"
  for the trend figure — that was true when written and the refresh runs have
  since improved it. **The docstring is stale, the live number is 12.** Both
  should keep improving as `refresh_comps.py` accumulates history.
- **Time-dependent responses.** `/projection` and `/recommendation` project from
  `date.today()`, so their numbers move day to day even though the Monte Carlo
  seed is fixed at 42. Two calls on the same day are identical; the same call
  tomorrow is not. Valuation's liquidity and trend windows are likewise anchored
  to `CURRENT_DATE` and degrade toward null/zero if the refresh job stops.

---

## 7. Response shapes to build against

All payloads below are **real responses captured live on 2026-08-16**, not
paraphrases.

### `GET /sneakers/{id}/valuation`

> ⚠️ **The headline key is `ebay_median`, not `value`.** It was renamed when the
> endpoint started returning two medians. Anything written against the old `value`
> key reads `undefined` silently. This is the one breaking change most likely to
> be missed.

```json
{
  "sneaker_id": 1,
  "name": "Jordan 1 Low Travis Scott Reverse Mocha",
  "style_code": "DM7866-162",
  "ebay_median": 1450.0,
  "q1": 1069.5,
  "q3": 1592.49,
  "risk_pct": 36.1,
  "sales_per_week": 1.63,
  "trend_ratio": 0.883,
  "trend_recent_count": 6,
  "trend_older_count": 7,
  "comp_count_raw": 14,
  "comp_count_used": 13,
  "low_confidence": false,
  "size_breakdown": [{"size": "13", "median_price": 1200.0, "comp_count": 3}],
  "listing_type_breakdown": {"buy_it_now": 9, "best_offer_accepted": 3, "auction": 2},
  "goat_median": 1382.5,
  "goat_sample_size": 188
}
```

- `trend_ratio` is null unless **both** windows hold ≥3 surviving comps (12/50
  today). `trend_recent_count` and `trend_older_count` are always present, so a
  null ratio explains itself.
- `size_breakdown` omits sizes with fewer than 3 surviving comps; it is `[]` for
  16 of 50.
- `listing_type_breakdown` holds three keys, not two — `buy_it_now`,
  `best_offer_accepted`, `auction`. Don't hardcode two.
- `low_confidence` is `comp_count_used < 5`.
- `risk_pct` is `(q3 - q1) / ebay_median * 100`, deliberately mixing pre-fence
  quartiles with the post-fence median — see `app/valuation.py` for why that is
  correct and not a bug.

### `GET /sneakers/{id}/projection`

`normal` / `low_confidence`:

```json
{
  "sneaker_id": 1,
  "name": "Jordan 1 Low Travis Scott Reverse Mocha",
  "style_code": "DM7866-162",
  "confidence_tier": "normal",
  "mape": 9.3,
  "n_rows": 91,
  "half_life_days": 21,
  "horizons": [
    {"days_ahead": 30, "expected_price": 1391.35, "bear": 1098.54, "base": 1388.73, "bull": 1676.27},
    {"days_ahead": 60, "expected_price": 1378.99, "bear": 1086.18, "base": 1376.37, "bull": 1663.91},
    {"days_ahead": 90, "expected_price": 1366.63, "bear": 1073.82, "base": 1364.01, "bull": 1651.55}
  ]
}
```

`suppressed` (evaluated, found unreliable):

```json
{"sneaker_id": 17, "name": "Jordan 4 Union LA Guava Ice", "style_code": "DC9533-800",
 "confidence_tier": "suppressed", "mape": 26.0, "n_rows": 57, "half_life_days": 30,
 "horizons": []}
```

never processed (no `sneaker_projections` row at all):

```json
{"sneaker_id": 2, "name": "Jordan 1 High Chicago Lost & Found", "style_code": "DZ5485-612",
 "confidence_tier": null, "mape": null, "n_rows": null, "half_life_days": null,
 "horizons": []}
```

unknown id → `404 {"detail": "Sneaker not found"}`

`horizons` is **always present**, so it can be read unconditionally. Horizons are
fixed at 30/60/90 and are not a query parameter. `confidence_tier: null` means
never processed; `"suppressed"` means evaluated and rejected — distinct states.

### `GET /sneakers/{id}/recommendation`

With a verdict:

```json
{
  "sneaker_id": 1,
  "name": "Jordan 1 Low Travis Scott Reverse Mocha",
  "style_code": "DM7866-162",
  "confidence_tier": "normal",
  "horizon_days": 30,
  "goat_median": 1382.5,
  "ebay_median": 1450.0,
  "recommendation": "SELL",
  "reason": null,
  "net_now": 1206.07,
  "net_projected": 1211.53,
  "percent_gain": 0.5
}
```

`no_goat_data` (note `ebay_median` is still populated):

```json
{"sneaker_id": 2, "name": "Jordan 1 High Chicago Lost & Found", "style_code": "DZ5485-612",
 "confidence_tier": null, "horizon_days": 30, "goat_median": null, "ebay_median": 269.0,
 "recommendation": null, "reason": "no_goat_data",
 "net_now": null, "net_projected": null, "percent_gain": null}
```

`insufficient_data_for_recommendation` (same shape, but `goat_median` **is**
present — this is the suppressed case):

```json
{"sneaker_id": 17, "name": "Jordan 4 Union LA Guava Ice", "style_code": "DC9533-800",
 "confidence_tier": "suppressed", "horizon_days": 30, "goat_median": 514.0, "ebay_median": 449.0,
 "recommendation": null, "reason": "insufficient_data_for_recommendation",
 "net_now": null, "net_projected": null, "percent_gain": null}
```

- `recommendation` ∈ `"HOLD"` | `"SELL"` | `null`. `reason` is null exactly when
  `recommendation` is not.
- **Both sides of the comparison are GOAT**, netted of GOAT fees. `ebay_median` is
  a labelled **reference figure only** and is not in the arithmetic. An earlier
  design compared eBay-now against GOAT-projected and most of the resulting "gain"
  turned out to be the cross-platform gap, not time.
- `horizon_days` is always 30 — the shortest horizon, chosen because the trend is
  linear and the uncertainty band doesn't widen.

### `GET /sneakers/search?q=`

Returns `{query, stage, count, truncated, results[]}`.

- `stage` is `"substring"` when stage 1 found anything, `"fuzzy"` otherwise —
  including when both stages found nothing.
- Frontend rule: `stage == "fuzzy" && count > 0` → render "no exact matches, did
  you mean…". `count == 0` → "no results".
- `match_score` is null on substring hits, a 3dp float on fuzzy hits.
- Cap is 20 results; `truncated` says there were more.
- Blank/whitespace `q` → **400** `"Query parameter 'q' must not be empty"`. A
  missing `q` → FastAPI's 422.

### `GET /sneakers/{id}`

Catalogue fields only, no valuation: `id, name, brand, style_code, colorway,
image_url, release_date, hype_tier`. Deliberately separate from `/valuation` so
the page can render immediately and fill numbers in as they arrive. `image_url` is
null on all 50.

### Portfolio

- `GET /portfolio` → `{count, items[]}`. Each item has `current_value`,
  **`unrealized_pl_gross`** (pre-fee — netting requires choosing a platform, which
  is not decided at list time), and `valuation_low_confidence`. All three are null
  when the sneaker has no comps. Null means "no valuation", never zero.
- `POST /portfolio` → 201. `purchase_source` ∈ `snkrs | stockx | goat | ebay |
  in_store | other`. `purchase_price` must be > 0 (422 otherwise).
- `POST /portfolio/{owned_id}/sell` → 201 with frozen `fee_percent_applied`,
  `fixed_fee_applied`, `fee_amount`, `realized_pl`. `sale_platform` ∈ `ebay |
  stockx | goat`. `sale_price` > 0. Atomic — the pair cannot land in both tables
  or neither.
- `DELETE /portfolio/{owned_id}` → 204, or 404 if not the user's. **404 rather
  than 403 on someone else's pair is deliberate** — a 403 would confirm the id
  exists and leak another user's portfolio size to id enumeration.
- `GET /portfolio/sales` → `{count, items[]}`, reading frozen history. Changing a
  fee rate will not retroactively alter a past sale.

Realized P/L is net of fees and computed in NUMERIC; unrealized is gross and
computed in float. That asymmetry is intentional — see `app/portfolio.py`.

---

## 8. Deferred — do not propose these as new work

- Non-linear / curve trend fitting (overfitting risk on this sample size)
- Monte Carlo band widening with horizon
- StockX data ingestion; order-count weighting
- Recency-weighted valuation (v1.1 — needs 6+ months of history to backtest)
- Used-condition valuation (v1.1 — needs a source for used comps plus measured
  multipliers)
- Per-size projection breakdown
- Measuring GOAT's multiplier directly; re-measuring StockX's
- Fallback lookup for the 7 unmatched SKUs
- Dedup on `market_sales` re-run (deliberate gap)
- Pagination for the KicksDB endpoints (no cursor observed; `meta` always null)
- Cross-colourway contamination in `sold_comps`
- `lifecycle_curves` / comps model; per-size profitability endpoints
- Validating the eBay fee rates and the 1.5 IQR fence factor

**Open history issue (cosmetic).** Commit `0e17fda`'s message describes pointing
`build_sneaker_projections.py` at the archived exploration path — but its diff
adds `scripts/price_series_test.py` and a `.pyc`, and the docstring change it
describes actually landed in `10648f4`. The `.pyc` was removed later in `a817a2f`.
Fixing the message needs a force-push; not worth it.
