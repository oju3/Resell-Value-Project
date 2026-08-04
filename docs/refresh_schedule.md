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

## Raw-count variation is a velocity signal, not degradation

`refresh_runs.raw_returned` varies run to run — sometimes the full
`COMPS_PER_SNEAKER = 20`, sometimes single digits, occasionally 0. Read in
isolation this can look like something is wrong (a thinning pool, actor
degradation, contamination eating into results). It isn't. Every Apify run
behind this data succeeded with zero actor failures — the variation is fully
explained by two things that have nothing to do with data quality:

1. **`daysToScrape` differs by design.** The backfill (`apify_backfill.py`)
   scrapes a 90-day window and consistently returns the full 20 requested.
   The refresh job scrapes only `DAYS_TO_SCRAPE = 30` days (see "Purpose"
   above — this tops up recent sales, it does not rebuild history). A
   90-day window naturally has ~3x the sold-listing pool of a 30-day window
   for the same sneaker, so a lower raw count in the refresh job than in the
   backfill is expected on that basis alone, not a regression.
2. **Genuine sold volume varies by shoe.** Within a fixed 30-day window,
   different sneakers have different real sales velocity — a tier-1 hype
   release and a tier-3 slow mover do not sell at the same pace. A raw count
   of 3 for a low-volume sneaker in a 30-day window is not a fault to
   investigate; it's the actual sales-velocity signal for that sneaker. Only
   a raw count of exactly 0 combined with the same sneaker doing so
   *repeatedly* would be worth a second look (e.g. as a query-matching
   problem), and even then the fix is a query/keyword change, not a filter
   or contamination fix.

Concretely: `refresh_runs.outcome = 'no_listings_found'` (see below) means
"nothing sold in the last 30 days for this sneaker," full stop. It does not
imply a stall, a fault, or contamination — see sneaker id 19 (Jordan 4 Red
Thunder), which logged `raw_returned = 0` with zero actor errors anywhere in
the run.

## Zero-write outcomes

A sneaker whose actor call succeeds but yields **zero newly-inserted rows**
never advances its `scraped_at`. That much is cause-agnostic: because
staleness drives selection, a sneaker stuck like this doesn't just sit there
quietly, it becomes one of the *stalest* sneakers in the catalogue and gets
re-selected on (or near) the very next run, repeating the same
zero-progress, non-zero-cost cycle indefinitely and starving other sneakers
of their rotation slot.

But the *cause* of a zero-write run is not one thing, and an earlier version
of this guard collapsed all of them into a single generic `outcome =
'stalled'` with a write-up that named cross-colourway contamination as the
"most likely cause." That was wrong more often than not: of the 4 zero-write
runs logged under the old scheme, one (`raw_returned = 0`, sneaker 19) had
nothing to be contaminated by, and the other three all matched more cleanly
to routine window overlap once their `on_conflict_skipped` rows were traced
to their owning `sneaker_id` (see `scripts/migrate_refresh_runs_outcomes.py`
for the reclassification). Naming a single ambiguous value also reproduced
the exact problem `refresh_runs` was split out of `comp_rejections` to
avoid — a reader having to cross-check other columns to know what a row
means (see "Two audit tables, two grains" below).

`outcome` now states its own cause directly, using
`comp_pipeline.find_existing_owners()` (already used by `apify_backfill.py`
for the same purpose, but previously never wired into `refresh_comps.py`)
to check whether each `ON CONFLICT`-skipped item belongs to this sneaker or
a different one:

- **`'ok'`** — `written > 0`. Normal case, logged for every attempt (not just
  failures) so spend-per-sneaker analysis is possible later.
- **`'no_listings_found'`** — `raw_returned = 0`. Nothing sold in the
  30-day window. See "Raw-count variation" above — this is a velocity
  reading, not a fault.
- **`'all_filtered'`** — `raw_returned > 0` but every returned item was
  rejected by `filter_comp` (kids listings, bundles, etc.). Filtering
  working as intended; unrelated to contamination.
- **`'no_new_sales'`** — every accepted row's `item_id` was already in
  `sold_comps` under this **same** `sneaker_id`. The 30-day window overlapped
  a previous scrape; these are the same real sales already on file, not new
  ones. Expected on a rotation whose cadence can outrun a slow seller's
  actual turnover.
- **`'cross_sneaker_conflict'`** — at least one accepted row's `item_id` was
  already in `sold_comps` under a **different** `sneaker_id`. This is the
  one outcome that's actually diagnostic of the cross-colourway keyword
  contamination logged as BLOCKED in `docs/comp_filtering_spec.md`.
- **`'actor_error'` / `'db_error'`** — unchanged from before.

`cross_sneaker_skips` (an `INT` column alongside `outcome`) records how many
of the run's `on_conflict_skipped` rows were owned by a different
`sneaker_id` — supporting detail behind the `cross_sneaker_conflict`
classification, not something a reader needs to consult to know what
`outcome` means. It's `NULL` (not `0`) for any row logged before this column
existed, since per-run item_ids weren't retained and the distinction can't
be reconstructed retroactively for those rows.

Only `'cross_sneaker_conflict'` escalates: before inserting this run's row,
`refresh_comps.py` reads that sneaker's *immediately preceding*
`refresh_runs.outcome`. If it was also `'cross_sneaker_conflict'`, this run
is flagged **consecutive** and printed as a loud, impossible-to-miss warning
block in the run summary. The other three zero-write outcomes print a
routine, non-alarming line and never escalate — repeated
`'no_listings_found'` or `'no_new_sales'` for the same sneaker is just a
persistently low-velocity shoe, not something to investigate.

This job does not fix cross-sneaker conflicts (the underlying title-to-SKU
matching problem is explicitly deferred to Phase 3). It only detects and
surfaces them. Seeing the same sneaker in the consecutive-conflict list
repeatedly is the signal to prioritize that work, not something this job
resolves on its own.

## Should `raw_returned` feed `sales_velocity`?

No — `sales_velocity.sales_per_week` should be derived from `sold_comps.
ended_at` directly, not from `refresh_runs.raw_returned`. Three reasons
`raw_returned` is the wrong input:

1. **It's capped.** `COMPS_PER_SNEAKER = 20` means a sneaker actually selling
   40 pairs in 30 days and one selling 20 both report `raw_returned` at or
   near 20 — the cap hides real velocity differences above it.
2. **It includes pre-filter noise.** `raw_returned` counts everything the
   actor returned before `filter_comp` runs — kids listings, bundles, etc.
   included. `sold_comps` holds only what survived filtering, which is the
   more accurate count of real, attributable sales.
3. **It's a point-sample from an irregular, overlapping window.** Runs land
   on a rotating ~4-week cadence with a 30-day lookback, so consecutive
   `raw_returned` values for the same sneaker overlap in what they're
   counting rather than forming a clean non-overlapping rate.

`sold_comps.ended_at` doesn't have these problems: it's the filtered ground
truth, unbounded by the per-call cap, and a query like `COUNT(*) WHERE
ended_at > now() - interval '90 days'` divided by `90/7.0` gives a real rate
over a clean, explicit window. That should be its own small scheduled
aggregation step — consistent with this repo's existing pattern of one
table per concern (`comp_rejections` vs `refresh_runs`) — not folded into
`refresh_comps.py` itself. `raw_returned` may still be useful as a cheap
secondary signal for *scheduling* (e.g. deprioritizing sneakers with a
history of low raw counts to save budget), but that's a rotation
optimization, not the source of truth for `sales_velocity`.

## Known false positive: mid-run credit exhaustion

The zero-write outcomes above assume every logged `raw_returned` reflects a
normal, uninterrupted actor call. That assumption breaks if the Apify
account runs out of monthly credit partway through a run: an
aborted/truncated call can return far fewer items than `COMPS_PER_SNEAKER`
requested, and if that truncated set happens to already be in `sold_comps`,
`write_comps()` still reports `written == 0` — indistinguishable, from
`raw_returned` alone, from a genuine thin sold-listing pool (see "Raw-count
variation" above, which explains why a low `raw_returned` is normally *not*
a problem worth flagging on its own).

**Confirmed instance — 2026-08-03 run.** `refresh_runs.raw_returned` across
that run's 13 calls was `20, 20, 6, 20, 16, 17, 14, 20, 9, 20, 20, 11, 19` —
dipping well below the requested 20 on several calls rather than holding
steady, as it does in a normal run. Apify's account notification confirmed
the $5/month free-tier cap was exceeded partway through this run and
in-flight actor executions were aborted. Sneaker id 3 (`Jordan 1 High Royal
Reimagined`) landed on one of the truncated calls (`raw_returned = 6`, all 6
already in `sold_comps` under sneaker id 3 itself) — under the current
scheme this logs `outcome = 'no_new_sales'`, not `'cross_sneaker_conflict'`,
which is the correct classification: investigated and ruled out as
cross-colourway contamination or a thin sold-listing pool — credit
exhaustion mid-call is the confirmed cause for this occurrence, independent
of any per-sneaker data property.

**Implication.** Truncated calls from mid-run credit exhaustion land in the
same non-escalating buckets (`'no_new_sales'` / `'all_filtered'` /
`'no_listings_found'`) as genuine low-velocity sneakers, which is the
correct behavior now that only `'cross_sneaker_conflict'` escalates — a
truncated call has no reason to produce a cross-sneaker match, so this
failure mode can no longer trigger a false consecutive-conflict warning the
way it could under the old single-`'stalled'` scheme. The pre-run credit
check below remains the primary defense against the truncation itself.

**Fix.** `refresh_comps.py::main()` now checks remaining Apify budget via
`comp_pipeline.fetch_remaining_budget_usd()`
(`GET /v2/users/me/limits`) before selecting sneakers or making any actor
calls. It aborts (exit 1, nothing written to `refresh_runs`) if remaining
budget is below `SNEAKERS_PER_RUN * ESTIMATED_COST_PER_CALL_USD *
CREDIT_CHECK_BUFFER` (a 1.2x buffer over the worst-case full-run cost, since
the per-call estimate is itself approximate). If the limits call fails —
this account API has no stronger reliability guarantee than the existing
`fetch_run_cost`, already documented above as unreliable in practice — the
job fails open: it prints a warning and proceeds without the check, rather
than blocking the run on a flaky secondary API call.

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
  succeeded, found nothing, got filtered out, overlapped a prior scrape,
  hit a cross-sneaker conflict, or errored," a completely different question
  at a completely different grain (one row per *run*, not per *comp*).
  Indexed on `(sneaker_id, run_at DESC)`: the consecutive-cross_sneaker_conflict
  check reads the last row per sneaker on every single attempt, and the
  table grows by
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
