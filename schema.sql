-- Sneaker resale valuation app: core schema
-- 8 tables + RLS. See CLAUDE.md for project context.

-- 1. sneakers: master catalog
CREATE TABLE sneakers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    style_code TEXT NOT NULL UNIQUE,
    colorway TEXT,
    image_url TEXT,
    release_date DATE,
    hype_tier INT CHECK (hype_tier BETWEEN 1 AND 3)
);

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

-- 4. owned_sneakers: user collections / brokerage account
CREATE TABLE owned_sneakers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id),
    sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
    size TEXT,
    condition TEXT,
    has_box BOOLEAN,
    has_receipt BOOLEAN,
    defects TEXT,
    purchase_price NUMERIC,
    purchase_date DATE,
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

-- Row Level Security
ALTER TABLE sneakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_velocity ENABLE ROW LEVEL SECURITY;
ALTER TABLE owned_sneakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_fees ENABLE ROW LEVEL SECURITY;
ALTER TABLE condition_multipliers ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifecycle_curves ENABLE ROW LEVEL SECURITY;
ALTER TABLE projections ENABLE ROW LEVEL SECURITY;

-- Public read on all market/reference data; writes only via service role (bypasses RLS)
CREATE POLICY "public read" ON sneakers FOR SELECT USING (true);
CREATE POLICY "public read" ON price_history FOR SELECT USING (true);
CREATE POLICY "public read" ON sales_velocity FOR SELECT USING (true);
CREATE POLICY "public read" ON platform_fees FOR SELECT USING (true);
CREATE POLICY "public read" ON condition_multipliers FOR SELECT USING (true);
CREATE POLICY "public read" ON lifecycle_curves FOR SELECT USING (true);
CREATE POLICY "public read" ON projections FOR SELECT USING (true);

-- owned_sneakers: users can only touch their own rows
CREATE POLICY "select own" ON owned_sneakers FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "insert own" ON owned_sneakers FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "update own" ON owned_sneakers FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "delete own" ON owned_sneakers FOR DELETE USING (auth.uid() = user_id);
