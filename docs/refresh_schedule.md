## Purpose

`scripts/apify_backfill.py` loaded 90 days of eBay sold comps for all 50
sneakers in one pass. `scripts/refresh_comps.py` keeps that data current
going forward. It is a recurring job, not a second backfill: it does not
skip sneakers that already have rows — refreshing existing sneakers is the
entire point. Both scripts share their filtering/parsing/write logic via
`scripts/comp_pipeline.py` (see "Shared code" below).

## Rotating-subset design

Apify's free tier is $5/month. Each sneaker costs ~$0.08 at 20 comps
(`COMPS_PER_SNEAKER`). A full 50-sneaker refresh is ~$4.00 — a single weekly
full refresh would already consume most of the monthly budget, and there's
no room for more than one per month at that volume.

Instead, each run refreshes a rotating subset:

- `SNEAKERS_PER_RUN = 13`
- 13 sneakers x $0.08/call ≈ **$1.04/run**
- Weekly cadence: ~4.33 runs/month x $1.04 ≈ **$4.16/month** — inside the $5
  free tier with headroom
- At 13/run, the 50-sneaker catalogue cycles once roughly every ~4 weeks
  (50 / 13 ≈ 3.8 runs to cover everything once)

Which 13 get picked each run is not fixed — `select_sneakers_to_refresh()`
in `refresh_comps.py` always takes the sneakers whose most recent
`sold_comps.scraped_at` is oldest (sneakers with zero comps sort first, as
maximally stale). This is self-correcting: a sneaker that fails one run
(actor error, DB error, or the stall condition below) simply stays stale and
gets picked up again next run, with no separate retry bookkeeping needed.

No `last_refreshed_at` column was added to `sneakers` for this. `scraped_at`
on `sold_comps` already gives the right behavior for free, because it only
advances when a row is actually written — a column that advanced regardless
of outcome would have to replicate "only on success" by hand and would be a
second source of truth that can drift from `sold_comps`.

## Why not daily

Daily refresh was considered and rejected. Two reasons:

1. **Cost.** Daily at the same 13-sneaker batch would be 13 x $0.08 ≈
   $1.04/day, x 30 days ≈ **$31/month** at `SNEAKERS_PER_RUN = 13` — well
   past the free tier. Fitting daily inside $5/month would mean shrinking the
   batch to ~2 sneakers/day, which takes the 50-sneaker catalogue ~25 days to
   cycle once — no faster than the current weekly rotation already achieves,
   just spread over more, smaller, costlier-per-comp actor calls.
2. **The data doesn't need it.** Sneaker resale prices don't move materially
   day to day, and the projection engine (Phase 3) models movement over a
   90-day horizon. Refreshing daily would produce finer time resolution than
   the model consumes — more Apify spend for no improvement in projection
   quality. Weekly-cadence rotation already keeps every sneaker's data within
   about a month old, which is well inside the 90-day window the projections
   reason over.

### Moving to daily later

If the calculus changes (bigger Apify budget, a projection model that wants
finer granularity), move to daily by editing constants in
`scripts/refresh_comps.py` — no logic changes:

- `SNEAKERS_PER_RUN` — lower this if staying near the free tier (e.g. 2-3/day
  to roughly match the current monthly spend), or raise it if budget allows.
- `MAX_CALLS_PER_RUN` — keep it a few above whatever `SNEAKERS_PER_RUN`
  becomes; it's a runaway guard, not a target.
- `COMPS_PER_SNEAKER` / `DAYS_TO_SCRAPE` — only touch these if the per-sneaker
  cost or lookback window itself needs to change; they're independent of
  cadence.
- `ESTIMATED_COST_PER_CALL_USD` — only touch if Apify's actual per-call price
  changes.
- The cron schedule itself (day-of-week -> daily) is set wherever the job is
  scheduled on Railway, not in this file.

## Stalled-sneaker guard

A sneaker whose actor call succeeds but yields **zero newly-inserted rows**
never advances its `scraped_at`. The most likely cause is the cross-colourway
keyword contamination already logged as BLOCKED in
`docs/comp_filtering_spec.md`: every comp the actor returns for that sneaker
this run was already written to `sold_comps` under a *different* sneaker_id
(first-write-wins on `item_id`), so `write_comps()` reports zero new rows
even though the actor call succeeded and cost money.

Because staleness drives selection, a sneaker stuck like this doesn't just
sit there quietly — it becomes one of the *stalest* sneakers in the catalogue
and gets re-selected on (or near) the very next run, repeating the same
zero-progress, non-zero-cost cycle indefinitely and starving other sneakers
of their rotation slot.

This job does not fix that (the underlying title-to-SKU matching problem is
explicitly deferred to Phase 3). It only detects and surfaces it, using the
dedicated `refresh_runs` table (see "Two audit tables, two grains" below) —
not `comp_rejections`, which stays strictly per-listing:

- Every attempt writes a `refresh_runs` row: `sneaker_id`, `run_at`,
  `raw_returned`, `new_rows_inserted`, `on_conflict_skipped`, and an
  `outcome` of `'ok'`, `'stalled'`, `'actor_error'`, or `'db_error'`.
  `'ok'` rows are logged too, not just failures — that's what makes
  spend-per-sneaker analysis possible later (actor calls burned per
  sneaker over time), not just failure forensics.
- A run where `written == 0` after a successful actor call and DB write logs
  `outcome = 'stalled'`.
- Before inserting this run's row, `refresh_comps.py` reads that sneaker's
  *immediately preceding* `refresh_runs.outcome`. If it was also `'stalled'`,
  this run's stall is flagged **consecutive** and printed as a loud,
  impossible-to-miss warning block in the run summary, distinct from the
  routine per-sneaker stall line.
- The run summary always reports a plain count and list of stalled
  sneakers for the run, and a separate, louder section for any that stalled
  on consecutive runs.

This is a detection mechanism only. Seeing the same sneaker in the
consecutive-stall list repeatedly is the signal to prioritize the Phase 3
title-to-SKU matching work, not something this job resolves on its own.

## Two audit tables, two grains

`comp_rejections` and `refresh_runs` look similar (both are append-only logs
keyed on `sneaker_id`) but record different things, and that distinction is
deliberate, not incidental:

- **`comp_rejections`** — strictly **per-listing**. One row per individual
  eBay comp that `filter_comp()` rejected, per
  `docs/comp_filtering_spec.md`'s Auditing section ("log every rejected row
  with the rule that rejected it"). A count grouped by `rejection_rule` here
  answers "how often does each filter rule fire." `item_id` and `title` are
  always populated for a real rejection.
- **`refresh_runs`** — strictly **per-sneaker-per-attempt**. One row per
  sneaker per time `refresh_comps.py` attempts it, regardless of outcome. A
  count grouped by `outcome` here answers "how many refresh attempts
  succeeded/stalled/errored," a completely different question at a
  completely different grain (one row per *run*, not per *comp*). Indexed on
  `(sneaker_id, run_at DESC)`: the consecutive-stall check reads the last
  row per sneaker on every single attempt, and the table grows by
  `SNEAKERS_PER_RUN` rows every run, indefinitely — an unindexed lookup would
  degrade run over run as the table grows.

An earlier version of the stall guard logged `stalled_no_new_rows` markers
directly into `comp_rejections` with `item_id`/`title` left `NULL`. That was
reverted: a rule-frequency query against `comp_rejections`
(`SELECT rejection_rule, COUNT(*) ... GROUP BY rejection_rule`) would have
silently mixed a once-per-stalled-run marker in with real per-listing
rejection counts, at a different scale and meaning, with no documented way
to tell them apart short of checking for `item_id IS NULL`. `refresh_runs`
exists so that never has to happen — if you're counting rejected listings,
query `comp_rejections`; if you're auditing refresh attempts or spend, query
`refresh_runs`. Don't add run-level rows to `comp_rejections`, and don't add
per-listing rows to `refresh_runs`.

## Shared code

`scripts/comp_pipeline.py` holds the actor-call, parsing, filter, and
write/log functions shared by `apify_backfill.py` and `refresh_comps.py` —
`call_actor`, `fetch_run_cost`, `filter_comp` (and its parsing helpers),
`write_comps`, `find_existing_owners`, `log_rejections`, and the
`MIN_PRICE_BY_TIER` / exclusion-pattern constants from
`docs/comp_filtering_spec.md`. Both scripts import from it rather than each
defining their own copy, so a change to a filter rule only has one place to
change. `call_actor` takes `days_to_scrape` as a parameter (90 for the
backfill, `DAYS_TO_SCRAPE = 30` here) since that's the one input the two
callers disagree on.

Not shared, and deliberately so:

- Raw-response caching (`save_raw_response` / `load_cached_response` /
  `data/raw_comps/`) — backfill-only. That directory exists so the backfill's
  filter logic can be replayed offline via `--from-cache` without spending
  Apify credit again. This job has no equivalent replay use case, and saving
  every run's raw responses indefinitely would grow that directory without
  bound for a job meant to run forever. `refresh_comps.py` does not write to
  `data/raw_comps/`.
- Sneaker selection — the backfill takes all 50 sneakers in `id` order
  (optionally `--limit`); the refresh job takes the `SNEAKERS_PER_RUN`
  staleset by `scraped_at`. Different purposes, different queries.
- The backfill's `--force` / "skip if rows already exist" gate — inverted
  here by definition, since refreshing existing rows is this job's entire
  purpose.
