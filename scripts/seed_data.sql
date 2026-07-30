-- Seed data for platform_fees and condition_multipliers

INSERT INTO platform_fees (platform, fee_percent, fixed_fee, min_condition, default_days_to_sell) VALUES
    ('stockx', 12.0, 0,    'deadstock', 3),
    ('goat',   12.4, 5.00, 'good',      5),
    ('ebay',   13.6, 0.30, 'any',       7);

INSERT INTO condition_multipliers (condition, multiplier, uncertainty_percent) VALUES
    ('deadstock', 1.00, 4),
    ('vnds',      0.87, 8),
    ('worn_once', 0.78, 12),
    ('good',      0.65, 15),
    ('fair',      0.50, 20),
    ('beat',      0.35, 25);
