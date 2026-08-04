## Purpose

The Phase 2 backfill (`scripts/apify_backfill.py`) loaded ~1000 eBay sold comps
across the 50-sneaker catalogue. Everything up to this point has validated
that the pipeline runs without error and that its filtering rules behave as
designed (`docs/comp_filtering_spec.md`). None of that checks whether the
resulting numbers resemble the real market. This document is that check: a
manual spot-check comparing database output against live eBay sold listings,
performed as the final Phase 2 deliverable.

## Method

10 sneakers were sampled from `sold_comps` (via `sneakers` join), covering
all three hype tiers. For each, the count of `conditionId = 1000`
(deadstock) comps, the deadstock median, min, max, and `ended_at` date range
were pulled from the database.

Of those 10, 3 were checked by hand on 2026-08-04 against live eBay listings:
filtered to Sold Items + New with box, all sizes, on `ebay.co.uk`, with GBP
converted to USD at an assumed rate of ~1.31 USD/GBP.

## Sampled data

| Sneaker | Tier | n (cond 1000) | Deadstock median | Min | Max | ended_at range |
|---|---|---|---|---|---|---|
| J4 Bred Reimagined | 2 | 16 | $203.13 | $130.00 | $265.49 | 2026-07-27 → 08-02 |
| J1 High Chicago Lost & Found | 1 | 21 | $269.00 | $176.77 | $350.00 | 2026-07-08 → 08-02 |
| J4 Black Cat | 1 | 19 | $250.00 | $150.00 | $599.99 | 2026-07-10 → 08-02 |
| J11 Cherry | 2 | 16 | $229.00 | $169.99 | $400.00 | 2026-07-10 → 07-31 |
| J3 White Cement Reimagined | 1 | 15 | $270.00 | $200.00 | $320.00 | 2026-06-14 → 08-01 |
| J5 Racer Blue | 1 | 16 | $234.00 | $155.00 | $294.95 | 2026-05-24 → 07-28 |
| J1 High UNC Reimagined | 3 | 22 | $109.99 | $89.99 | $219.00 | 2026-07-09 → 08-02 |
| J1 High True Blue | 3 | 23 | $110.00 | $80.00 | $230.00 | 2026-06-05 → 08-03 |
| J11 Neapolitan (W) | 3 | 18 | $164.98 | $109.99 | $249.99 | 2026-05-06 → 07-30 |
| J1 High Palomino | 3 | 16 | $135.00 | $95.00 | $199.99 | 2026-05-09 → 07-31 |

## Manual verification

Checked on `ebay.co.uk`, filtered to Sold Items + New with box, all sizes.
GBP converted at ~1.31 USD/GBP.

| Sneaker | Tier | DB median | Observed market median | Gap |
|---|---|---|---|---|
| J1 High Chicago Lost & Found | 1 | $269 | ~$249 | +8% |
| J4 Black Cat | 1 | $250 | ~$296 | −16% |
| J1 High UNC Reimagined | 3 | $110 | ~$135 | −19% |

## Findings

- **Pass on magnitude and ordering.** Every checked sneaker landed in the
  right price band, and the tier ordering holds: tier-1 grails in the
  $250–270 range, tier-3 general releases at $110–135. The pipeline
  distinguishes tiers correctly by price, which is the property that
  matters most downstream.
- **Two of three came in low, one high — no consistent directional bias.**
  That pattern is more consistent with sampling noise than with a
  systematic error in the filtering or aggregation.
- **Validation was performed against UK data, which is a real limitation.**
  The pipeline sources US comps in USD; verification used `ebay.co.uk` in
  GBP at an assumed 1.31 rate. UK and US sneaker markets price differently
  and the conversion rate is approximate, so the observed gaps of 8–19% may
  be partly artefact rather than error. A US-only re-check is a v1.1 item.
- **Date windows are not aligned.** Database medians cover specific
  `ended_at` ranges per sneaker (some four weeks, some two months); the
  eBay pages show whatever sold recently. Not like-for-like.
- **Two contamination types were observed live that the current filters
  would handle differently.** A Chicago Lost & Found (TD) toddler listing
  appeared and is covered by the existing exclusion regex. A "Jordan 4 RM
  Black Cat" listing at roughly a third of market also appeared — RM is a
  distinct lifestyle model, and no current exclusion rule covers it. Logged
  as a new contamination type under Open questions in
  `docs/comp_filtering_spec.md`, related to but distinct from the existing
  model-variant BLOCKED item.
- **The Black Cat $599.99 maximum was investigated and downgraded in
  concern.** Against an observed market of ~$296 rather than the
  database's $250, that sale is roughly 2x market rather than 2.4x — high
  but not obviously erroneous.

## Limitations

- Only 3 of 50 sneakers were manually verified.
- Verification market (UK, GBP) differs from the pipeline's source market
  (US, USD); the conversion rate used is approximate.
- Database `ended_at` windows and the live eBay "recently sold" view are not
  the same time window.
- Manual checks were single-point-in-time (2026-08-04) and were not
  repeated.

## v1.1 improvements

- Re-run manual verification against US-only eBay listings to remove the
  UK/US market and currency-conversion confound.
- Expand the RM lifestyle-model observation into a confirmed exclusion rule
  (or model-variant BLOCKED item) once a larger sample is inspected, per the
  new Open questions entry in `docs/comp_filtering_spec.md`.
- Align manual spot-checks to the same `ended_at` window as the database
  medians being checked, rather than "whatever sold recently."
