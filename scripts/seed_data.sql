-- Seed data for platform_fees and condition_multipliers

-- GOAT's 12.40% + $5.00 = 9.5% commission + $5 US seller fee + 2.9% cash-out fee.
-- StockX and GOAT rates assume a standard (Level 1) seller tier.
INSERT INTO platform_fees (platform, fee_percent, fixed_fee, min_condition, default_days_to_sell, min_price, max_price) VALUES
    ('ebay',   13.25, 0.30, 'any',       7, 0,   150),
    ('ebay',    8.00, 0,    'any',       7, 150, NULL),
    ('stockx', 12.00, 0,    'deadstock', 3, 0,   NULL),
    ('goat',   12.40, 5.00, 'good',      5, 0,   NULL);

-- ASSUMED VALUES, NOT MEASURED. Unlike the fee rates above, none of these six
-- multipliers has a documented derivation -- they were seeded as "starting"
-- values in 2bd5743 (2026-07-30) to unblock downstream work and were never
-- replaced. Do not treat them as measured until they are.
--
-- This table is currently UNREAD: nothing outside this file and run_seed.py's
-- print-back queries it. The MVP values deadstock only, where the multiplier
-- is the 1.00 identity row. The rows stay seeded so used valuation can be
-- picked up later. See docs/scope_deadstock_only.md.
INSERT INTO condition_multipliers (condition, multiplier, uncertainty_percent) VALUES
    ('deadstock', 1.00, 4),
    ('vnds',      0.87, 8),
    ('worn_once', 0.78, 12),
    ('good',      0.65, 15),
    ('fair',      0.50, 20),
    ('beat',      0.35, 25);
