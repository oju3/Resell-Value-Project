-- Sneaker resale valuation app: core schema
-- 16 tables + RLS. See CLAUDE.md for project context.

-- Required extensions
-- pg_trgm powers the fuzzy fallback stage of GET /sneakers/search (see
-- app/sneakers.py). Without it the substring stage still works, but any search
-- that finds nothing raises "function word_similarity does not exist" instead
-- of recovering from the typo -- a failure that only shows up on the zero-hit
-- path, which is exactly the path least likely to be exercised by hand. It is
-- declared here so a fresh install cannot silently lose it.
--
-- Installed into the `extensions` schema, matching the convention this project
-- already uses for pgcrypto and uuid-ossp. `extensions` is on the default
-- search_path, so word_similarity() resolves without qualification.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;

-- 1. sneakers: master catalog
CREATE TABLE sneakers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    style_code TEXT NOT NULL UNIQUE,
    colorway TEXT,
    image_url TEXT,
    release_date DATE,
    hype_tier INT CHECK (hype_tier BETWEEN 1 AND 3),
    -- Nullable on purpose. Populated by scripts/fetch_goat_product_ids.py from
    -- KicksDB's unified products endpoint; a sneaker with no GOAT listing (SKU
    -- format mismatch, delisted, never carried) keeps NULL. NULL means "not
    -- looked up or not found" and must never be filled with a placeholder.
    --
    -- TEXT, not an integer, even though the observed GOAT value looks numeric
    -- ("1293064"). It is an external vendor identifier: nothing does arithmetic
    -- on it, leading zeros would be significant if they ever appeared, and the
    -- format is KicksDB's to change. The same response field carries a UUID for
    -- stockx. No UNIQUE and no index: a UNIQUE would turn a duplicate id from
    -- the vendor into a mid-backfill failure rather than something the loader
    -- can report.
    goat_product_id TEXT
);
COMMENT ON COLUMN sneakers.goat_product_id IS
    'GOAT source_product_id from KicksDB /v3/unified/products/{sku}. '
    'NULL means not looked up, or no goat entry in the response -- never a placeholder. '
    'Populated by scripts/fetch_goat_product_ids.py.';

-- 2. price_history: daily eBay sold-price feed
CREATE TABLE price_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    platform TEXT NOT NULL,
    size TEXT NOT NULL,
    price NUMERIC NOT NULL,
    condition_type TEXT NOT NULL CHECK (condition_type IN ('new', 'used')),
    date DATE NOT NULL,
    UNIQUE (sneaker_id, platform, size, condition_type, date)
);
CREATE INDEX idx_price_history_sneaker_date ON price_history (sneaker_id, date);

-- 3. sales_velocity: sales pace per sneaker/size
CREATE TABLE sales_velocity (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    size TEXT NOT NULL,
    sales_per_week NUMERIC,
    last_updated TIMESTAMPTZ,
    UNIQUE (sneaker_id, size)
);

-- 4. owned_sneakers: pairs a user currently owns (UNSOLD only)
-- Sold pairs are MOVED to sold_sneakers (table 13) and deleted from here, so
-- "what I own" and "what I sold" stay two clean queries with no status filter.
--
-- A user may own several of the same sneaker in different sizes or at
-- different prices, so there is deliberately no UNIQUE (user_id, sneaker_id).
--
-- Authorization note: RLS below is defence-in-depth only. The API reaches this
-- table over psycopg2, where auth.uid() is NULL -- every query must carry an
-- explicit WHERE user_id = %s. See app/db.py::get_conn.
CREATE TABLE owned_sneakers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id),
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    size TEXT,
    -- UNUSED in v1: the MVP is deadstock-only, so every pair is deadstock by
    -- definition. Kept, not dropped -- see docs/scope_deadstock_only.md.
    condition TEXT,
    -- UNUSED in v1: not part of what the add-pair flow records.
    has_box BOOLEAN,
    has_receipt BOOLEAN,
    defects TEXT,
    -- CHECK, not just API validation: a negative purchase price produces
    -- nonsense P/L rather than an obvious error, and scripts/psql write here
    -- too. NULL still passes -- a CHECK is satisfied when it evaluates to
    -- NULL -- so this constrains the value without making the column required.
    purchase_price NUMERIC CHECK (purchase_price > 0),
    purchase_date DATE,
    -- Fixed vocabulary, lowercase slugs. Lowercase matches
    -- platform_fees.platform, which sold_sneakers.sale_platform must equal
    -- exactly for the banded fee lookup to return a row; one casing
    -- convention across adjacent columns avoids a silent zero-row lookup.
    -- CHECK rather than an enum: widening the list is a one-line swap.
    purchase_source TEXT CHECK (purchase_source IN
        ('snkrs', 'stockx', 'goat', 'ebay', 'in_store', 'other')),
    -- UNUSED BELOW THIS LINE. Superseded by sold_sneakers (table 13).
    -- NOTHING READS `status`, including this default: a sold pair is removed
    -- from this table entirely rather than flipped to 'sold', so no query
    -- filters on it. A default that is never checked is easy to mistake for
    -- load-bearing state -- it is not. Retained rather than dropped because
    -- dropping is destructive for no functional gain.
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'sold')),
    sale_platform TEXT,
    sale_price NUMERIC,
    fees_paid NUMERIC,
    sale_date DATE,
    realized_profit NUMERIC
);
CREATE INDEX idx_owned_sneakers_user_id ON owned_sneakers (user_id);

-- 5. platform_fees: commission schedules per marketplace
-- A platform can have multiple price-banded rows (e.g. eBay's $0-150 / $150+
-- split), so uniqueness is enforced on (platform, min_price) rather than
-- platform alone.
CREATE TABLE platform_fees (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform TEXT NOT NULL,
    fee_percent NUMERIC,
    fixed_fee NUMERIC,
    min_condition TEXT,
    default_days_to_sell INT,
    min_price NUMERIC,
    max_price NUMERIC,
    UNIQUE (platform, min_price)
);

-- 6. condition_multipliers: value retention by wear level
-- UNUSED IN v1, AND THE SEEDED VALUES ARE ASSUMED RATHER THAN MEASURED.
-- The MVP values deadstock/new sneakers only (docs/scope_deadstock_only.md),
-- so nothing reads this table; deadstock is the 1.00 identity row. The six
-- seeded multipliers have no documented derivation and cannot be validated
-- from the current feed -- eBay exposes three conditionId values, so five of
-- the six tiers collapse into 3000, and sold_comps holds zero rows there.
-- Kept, not dropped: used valuation returns in v1.1, and it needs measured
-- multipliers plus a data source that actually reports used sold prices.
CREATE TABLE condition_multipliers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    condition TEXT NOT NULL UNIQUE,
    multiplier NUMERIC,
    uncertainty_percent NUMERIC
);

-- 7. lifecycle_curves: comparables price trajectory by model family/hype tier
CREATE TABLE lifecycle_curves (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_family TEXT NOT NULL,
    hype_tier INT CHECK (hype_tier BETWEEN 1 AND 3),
    month_since_release INT NOT NULL,
    avg_price_index NUMERIC,
    UNIQUE (model_family, hype_tier, month_since_release)
);

-- 8. projections: cached bear/base/bull scenarios per sneaker/size/horizon
CREATE TABLE projections (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    size TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('1m', '3m', '6m')),
    bear NUMERIC,
    base NUMERIC,
    bull NUMERIC,
    agreement_score NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (sneaker_id, size, horizon)
);

-- 9. sold_comps: individual filtered eBay sold listings (raw comps, not aggregated)
-- See docs/comp_filtering_spec.md. price_history aggregation is a separate
-- downstream step that reads from this table; the actor backfill writes here.
CREATE TABLE sold_comps (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    item_id TEXT NOT NULL UNIQUE,
    title TEXT,
    sold_price NUMERIC NOT NULL,
    shipping_price NUMERIC,
    total_price NUMERIC,
    currency TEXT NOT NULL,
    size TEXT,
    condition_id INT NOT NULL,
    listing_type TEXT,
    thumbnail_url TEXT,
    ended_at DATE NOT NULL,
    source TEXT NOT NULL DEFAULT 'apify_ebay',
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sold_comps_sneaker ON sold_comps (sneaker_id);
CREATE INDEX idx_sold_comps_ended_at ON sold_comps (ended_at);

-- 10. comp_rejections: audit log of comps dropped during filtering, and why.
-- Strictly per-listing: one row per rejected comp, per docs/comp_filtering_spec.md's
-- Auditing section. Per-sneaker-per-refresh-attempt bookkeeping (including the
-- refresh job's stall detection) lives in refresh_runs instead, not here --
-- see docs/refresh_schedule.md. A rejection_rule in this table always maps to
-- a real rejected listing with a real item_id; do not add run-level markers.
CREATE TABLE comp_rejections (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    item_id TEXT,
    title TEXT,
    rejection_rule TEXT NOT NULL,
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_comp_rejections_sneaker ON comp_rejections (sneaker_id);

-- 11. platform_multipliers: measured eBay -> platform price ratios (separate
-- from platform_fees: this converts an eBay deadstock median into an
-- estimated sale price on the target platform; platform_fees then converts
-- that sale price into a net payout. See docs/platform_multipliers.md.
CREATE TABLE platform_multipliers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform TEXT NOT NULL,
    multiplier NUMERIC NOT NULL,
    band_low NUMERIC,
    band_high NUMERIC,
    sample_size INT,
    method TEXT,
    confidence TEXT CHECK (confidence IN ('none', 'low', 'medium', 'high')),
    is_proxy BOOLEAN NOT NULL DEFAULT false,
    proxy_source TEXT,
    measured_date DATE NOT NULL,
    notes TEXT,
    UNIQUE (platform, measured_date)
);
COMMENT ON TABLE platform_multipliers IS
    'platform_price = ebay_deadstock_median * multiplier; '
    'net_payout = platform_price * (1 - fee_percent) - fixed_fee (fee_percent/fixed_fee from platform_fees). '
    'eBay is the numeraire (multiplier = 1.00, confidence = high, is_proxy = false, a definitional '
    'reference rather than a measurement) -- the multiplier converts an eBay price into an estimated '
    'price on the target platform, never the reverse. Multiplier and fee are separate stages and must '
    'not be collapsed into one number.';

-- 12. refresh_runs: per-sneaker-per-attempt log written by scripts/refresh_comps.py,
-- one row every time the recurring refresh job attempts a sneaker (not just on
-- failure) -- 'ok' rows are what make spend-per-sneaker analysis possible later,
-- not just failure forensics. Distinct from comp_rejections (per-listing); this
-- is per-attempt bookkeeping. See docs/refresh_schedule.md.
--
-- outcome carries its own cause directly rather than a generic 'stalled' that
-- would require cross-checking raw_returned/on_conflict_skipped to interpret:
-- no_listings_found (raw_returned=0), all_filtered (every raw item rejected by
-- filter_comp), no_new_sales (all accepted rows already in sold_comps under
-- this SAME sneaker_id -- window overlap, expected), cross_sneaker_conflict
-- (at least one accepted row already in sold_comps under a DIFFERENT
-- sneaker_id -- the real contamination signal). Only cross_sneaker_conflict
-- escalates to the loud consecutive-run warning in refresh_comps.py.
-- cross_sneaker_skips is supporting detail behind that classification, not
-- itself the field a reader needs to consult.
CREATE TABLE refresh_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_returned INT,
    new_rows_inserted INT,
    on_conflict_skipped INT,
    cross_sneaker_skips INT,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'ok', 'no_listings_found', 'all_filtered', 'no_new_sales',
        'cross_sneaker_conflict', 'actor_error', 'db_error'
    ))
);
CREATE INDEX idx_refresh_runs_sneaker_run_at ON refresh_runs (sneaker_id, run_at DESC);

-- 13. sold_sneakers: historical record of pairs a user has sold.
-- A row here is a HISTORICAL FACT and must stay correct forever, so it does
-- not depend on any value that can change later:
--
--   * purchase_price/date/source are COPIED from owned_sneakers, because the
--     source row is deleted by the same transaction that writes this one.
--   * fee_percent_applied, fixed_fee_applied, fee_amount and realized_pl are
--     FROZEN at sale time rather than derived on read. platform_fees is
--     mutable and this repo has already reseeded it twice (d8370e7, b356de4);
--     recomputing would retroactively change what a past sale earned.
--   * sneaker_id stays a FOREIGN KEY rather than a copied name. Catalogue
--     identity is stable and it is the same shoe. Tradeoff, stated plainly:
--     renaming a sneaker re-renders past sales under the new name. Accepted
--     as correct; copy the name instead if history must be literally frozen.
--
-- realized_pl = sale_price - fee_amount - purchase_price. See app/portfolio.py
-- for the banded fee lookup, including the $150 boundary.
CREATE TABLE sold_sneakers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id),
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    size TEXT,
    purchase_price NUMERIC NOT NULL CHECK (purchase_price > 0),
    purchase_date DATE,
    purchase_source TEXT,
    sale_price NUMERIC NOT NULL CHECK (sale_price > 0),
    sale_platform TEXT NOT NULL CHECK (sale_platform IN ('ebay', 'stockx', 'goat')),
    sale_date DATE NOT NULL,
    fee_percent_applied NUMERIC NOT NULL,
    fixed_fee_applied NUMERIC NOT NULL,
    fee_amount NUMERIC NOT NULL,
    realized_pl NUMERIC NOT NULL,
    sold_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sold_sneakers_user_id ON sold_sneakers (user_id);

-- 14. market_sales: real GOAT sold transactions, one row per sale.
-- Pulled from KicksDB /v3/goat/products/{goat_product_id}/sales by
-- scripts/fetch_goat_sales.py, for every sneaker with a populated
-- sneakers.goat_product_id.
--
-- No `source` column by design: every row here came from GOAT by
-- construction, so the table is its own provenance. A second platform means a
-- new table, or a source column added then with a backfill for these rows --
-- not a column with exactly one value in it now.
--
-- goat_product_id is TEXT to match sneakers.goat_product_id exactly. The sales
-- response returns product_id as a JSON NUMBER (1293064, not "1293064"), so
-- the loader stringifies before comparing or inserting; a number on one side
-- and text on the other is how a join silently returns zero rows later.
--
-- KNOWN GAP -- NO DEDUPLICATION. Deliberately no unique constraint on
-- (goat_product_id, purchased_at, size_us, amount) or anything else, and the
-- loader has no ON CONFLICT clause, so re-running fetch_goat_sales.py WILL
-- insert duplicate rows. Accepted for the first pass, recorded here so it is
-- visible rather than discovered later.
--
-- amount and currency are stored exactly as returned, never converted.
CREATE TABLE market_sales (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    goat_product_id TEXT NOT NULL,
    -- Payload columns are nullable: a response row missing one field should
    -- still land with the rest intact rather than failing the whole batch.
    -- raw_response preserves whatever was actually returned.
    size_us TEXT,
    currency TEXT,
    amount NUMERIC,
    purchase_type TEXT,
    location TEXT,
    purchased_at TIMESTAMPTZ,
    raw_response JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_market_sales_sneaker ON market_sales (sneaker_id);
COMMENT ON TABLE market_sales IS
    'Real GOAT sold transactions from KicksDB /v3/goat/products/{id}/sales, one row per sale. '
    'No source column: every row is GOAT by construction. '
    'NO DEDUPLICATION -- re-running scripts/fetch_goat_sales.py inserts duplicates. '
    'amount/currency are stored exactly as returned, never converted.';
COMMENT ON COLUMN market_sales.raw_response IS
    'The individual sale object as returned, not the full response envelope. '
    'The complete envelope is cached to cache/kicksdb_goat_sales/{goat_product_id}.json. '
    'Keeps any field not mapped to a column above from being lost.';

-- 15. goat_daily_sales: GOAT's PRE-AGGREGATED daily feed, one row per DAY.
-- From KicksDB /v3/goat/products/{id}/sales/daily, written by
-- scripts/fetch_goat_daily_sales.py.
--
-- DISTINCT FROM market_sales (14). That table holds individual transactions
-- from the /sales endpoint; this holds GOAT's own daily rollup from
-- /sales/daily. Different endpoint, different response shape, different grain.
-- avg_amount here is GOAT's average, NOT something recomputed from
-- market_sales rows -- do not join or compare the two casually.
--
-- `orders` is the count of sales that day's avg_amount was computed from, and
-- is the per-day liquidity/confidence signal. Stored as returned, never
-- recomputed.
--
-- SPARSE BY DESIGN. The endpoint returns a row only for days on which at least
-- one sale happened, so a low-volume sneaker produces a gapped series. An
-- absent date means no sale occurred that day. Nothing fills, interpolates, or
-- backfills those gaps -- the gaps ARE the trading-activity signal.
--
-- Observed on the first batch: the endpoint appears to cap at 100 rows (two of
-- three sneakers returned exactly 100, one returned 91). If so the series is
-- truncated at the OLD end, so MIN(sale_date) is "as far back as the API went",
-- not "first sale ever". meta was null on every response, so there is no cursor
-- to page with.
--
-- UNIQUE (goat_product_id, sale_date), and the loader uses ON CONFLICT DO
-- UPDATE. Deliberately the opposite of market_sales' documented no-dedup gap: a
-- daily aggregate for a date is one fact a fresher pull should overwrite, so
-- re-running the loader is idempotent rather than additive. ingested_at is
-- reset on update, so it reads as "last refreshed", not "first landed".
--
-- No currency column: every sale in the underlying /sales feed was verified
-- USD, and this aggregates those same sales. The loader hard-stops if a
-- currency field ever appears with a non-USD value.
CREATE TABLE goat_daily_sales (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    goat_product_id TEXT NOT NULL,
    sale_date DATE NOT NULL,
    -- GOAT's own average for that day, and the order count it was computed
    -- from. Both stored as returned, never recomputed.
    avg_amount NUMERIC,
    orders INT,
    raw_response JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The dedup key. One daily aggregate per product per date; a re-run
    -- refreshes it in place rather than appending.
    UNIQUE (goat_product_id, sale_date)
);
CREATE INDEX idx_goat_daily_sales_sneaker ON goat_daily_sales (sneaker_id);
COMMENT ON TABLE goat_daily_sales IS
    'GOAT pre-aggregated daily sales from KicksDB /v3/goat/products/{id}/sales/daily. '
    'One row per DAY, and only for days with at least one sale -- gaps are real and are '
    'never filled or interpolated. Distinct from market_sales, which holds individual '
    'transactions from the /sales endpoint. avg_amount and orders come straight from '
    'GOAT and are never recomputed. UNIQUE (goat_product_id, sale_date): re-running the '
    'loader refreshes a day in place, it does not duplicate.';
COMMENT ON COLUMN goat_daily_sales.orders IS
    'Count of sales the day''s avg_amount was computed from. This is the per-day '
    'liquidity/confidence signal -- use as-is, do not recompute from market_sales.';
COMMENT ON COLUMN goat_daily_sales.raw_response IS
    'The individual daily object as returned, not the full response envelope. '
    'The complete envelope is cached to cache/kicksdb_goat_daily/{goat_product_id}.json.';

-- 16. sneaker_projections: tuned weighted-linear trend per sneaker.
-- One row per sneaker, fitted over goat_daily_sales (15) by
-- scripts/build_sneaker_projections.py. Derived from
-- scripts/exploration/per_sneaker_halflife.py, which is the archived validation
-- work behind the model.
--
-- DERIVED AND REBUILDABLE. UNIQUE (sneaker_id) with ON CONFLICT DO UPDATE, so
-- re-running the builder as goat_daily_sales accumulates refreshes each row in
-- place. Same reasoning as goat_daily_sales, deliberately NOT market_sales --
-- there is no meaningful "second projection" for a sneaker, only a fresher one.
--
-- HOW THE HALF-LIFE IS CHOSEN vs HOW THE STORED FIT IS PRODUCED -- these differ,
-- and conflating them misreads every row. The half-life is selected by backtest:
-- train on the first 75% of a sneaker's daily rows, score MAPE against the
-- held-out last 25%, keep the lowest. slope_per_day/intercept/residual_stdev are
-- then REFIT on 100% of the rows at that half-life. The backtest's only job is
-- choosing the half-life; the production fit does not throw away the most recent
-- 25% of real data forever. So mape describes the CHOICE, not the error of the
-- stored fit.
--
-- Each row is weighted by exp(-ln(2)/half_life * days_before_most_recent), so a
-- row one half-life old counts half as much as the newest. The half-life is
-- tuned per sneaker because the right amount of recency bias is not a global
-- constant -- on this catalogue the winner ranges across the whole candidate
-- list, 30d winning 17 of 43 but 2d winning for the fastest-moving shoes.
--
-- Linear trend only. Non-linear/curve fitting was deliberately deferred: real
-- overfitting risk on 22-100 data points against a small expected gain. This
-- table stores the inputs Monte Carlo will consume later; it does not run Monte
-- Carlo, and that band is applied as a constant per horizon rather than widening
-- with distance -- a documented limitation, not fixed here.
CREATE TABLE sneaker_projections (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- UNIQUE, not just FK: one current projection per sneaker, and the
    -- ON CONFLICT target. No separate index needed -- UNIQUE creates one.
    sneaker_id BIGINT NOT NULL UNIQUE REFERENCES sneakers(id),
    -- All five NULL together when confidence_tier is 'insufficient_data'.
    -- Never partially populated.
    half_life_days INT,
    slope_per_day NUMERIC,
    intercept NUMERIC,
    residual_stdev NUMERIC,
    mape NUMERIC,
    -- Always known, including for insufficient_data rows, where it is the
    -- reason for the tier -- hence NOT NULL.
    n_rows INT NOT NULL,
    reference_date DATE,
    confidence_tier TEXT NOT NULL CHECK (confidence_tier IN
        ('normal', 'low_confidence', 'suppressed', 'insufficient_data')),
    -- The whole swept list, not just the winner.
    half_life_candidates JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE sneaker_projections IS
    'Tuned weighted-linear trend per sneaker over goat_daily_sales, with the backtest '
    'evidence behind it. Derived and rebuildable: UNIQUE (sneaker_id) with ON CONFLICT '
    'DO UPDATE, so re-running the builder refreshes rather than duplicates. The '
    'half-life is CHOSEN by backtest on the first 75% of rows, but the stored fit is '
    'REFIT on 100% of rows at that half-life. Linear trend only -- non-linear fitting '
    'was deliberately deferred (overfitting risk on 22-100 points). Stores the inputs '
    'Monte Carlo will consume later; does not run Monte Carlo.';
COMMENT ON COLUMN sneaker_projections.reference_date IS
    'x = 0 for the fitted line: predicted = intercept + slope_per_day * '
    '(target_date - reference_date in days). Equals the sneaker''s earliest '
    'goat_daily_sales date at build time. Stored rather than re-derived because '
    'MIN(sale_date) shifts if older daily rows ever arrive, which would silently '
    'invalidate every stored intercept.';
COMMENT ON COLUMN sneaker_projections.n_rows IS
    'Count of goat_daily_sales rows the fit was built from. Lets a stored MAPE be read '
    'in context -- 10% off 22 points is not 10% off 100 points. Populated even for '
    'insufficient_data rows, where it is the reason for that tier.';
COMMENT ON COLUMN sneaker_projections.confidence_tier IS
    'normal (MAPE<=15), low_confidence (15<MAPE<=25), suppressed (MAPE>25, evaluated '
    'and found unreliable), insufficient_data (<8 daily rows, could not be evaluated). '
    'Suppressed rows are still inserted -- absence would look like never-evaluated.';
COMMENT ON COLUMN sneaker_projections.mape IS
    'Mean absolute percentage error of the winning half-life against the held-out last '
    '25% of rows. Backtest evidence for the choice, NOT the error of the stored fit '
    '(which was refit on 100% of rows).';

-- Row Level Security
ALTER TABLE sneakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_velocity ENABLE ROW LEVEL SECURITY;
ALTER TABLE owned_sneakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_fees ENABLE ROW LEVEL SECURITY;
ALTER TABLE condition_multipliers ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifecycle_curves ENABLE ROW LEVEL SECURITY;
ALTER TABLE projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE sold_comps ENABLE ROW LEVEL SECURITY;
ALTER TABLE comp_rejections ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_multipliers ENABLE ROW LEVEL SECURITY;
ALTER TABLE refresh_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE sold_sneakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE market_sales ENABLE ROW LEVEL SECURITY;
ALTER TABLE goat_daily_sales ENABLE ROW LEVEL SECURITY;
ALTER TABLE sneaker_projections ENABLE ROW LEVEL SECURITY;

-- Public read on all market/reference data; writes only via service role (bypasses RLS)
CREATE POLICY "public read" ON sneakers FOR SELECT USING (true);
CREATE POLICY "public read" ON price_history FOR SELECT USING (true);
CREATE POLICY "public read" ON sales_velocity FOR SELECT USING (true);
CREATE POLICY "public read" ON platform_fees FOR SELECT USING (true);
CREATE POLICY "public read" ON condition_multipliers FOR SELECT USING (true);
CREATE POLICY "public read" ON lifecycle_curves FOR SELECT USING (true);
CREATE POLICY "public read" ON projections FOR SELECT USING (true);
CREATE POLICY "public read" ON sold_comps FOR SELECT USING (true);
CREATE POLICY "public read" ON comp_rejections FOR SELECT USING (true);
CREATE POLICY "public read" ON platform_multipliers FOR SELECT USING (true);
CREATE POLICY "public read" ON refresh_runs FOR SELECT USING (true);
CREATE POLICY "public read" ON market_sales FOR SELECT USING (true);
CREATE POLICY "public read" ON goat_daily_sales FOR SELECT USING (true);
CREATE POLICY "public read" ON sneaker_projections FOR SELECT USING (true);

-- owned_sneakers: users can only touch their own rows
CREATE POLICY "select own" ON owned_sneakers FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "insert own" ON owned_sneakers FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "update own" ON owned_sneakers FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "delete own" ON owned_sneakers FOR DELETE USING (auth.uid() = user_id);

-- sold_sneakers: same per-user scoping as owned_sneakers
CREATE POLICY "select own" ON sold_sneakers FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "insert own" ON sold_sneakers FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "update own" ON sold_sneakers FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "delete own" ON sold_sneakers FOR DELETE USING (auth.uid() = user_id);
