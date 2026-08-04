# Roadmap

Phase boundaries below (other than the Phase 5 launch date) were self-imposed
pacing targets, not commitments, and are recorded here as original targets
rather than deadlines. August 31 is the only firm date.

## Phase 1 — Setup (complete)

Supabase account, tables with RLS, `.env` and gitignore, GitHub repo, seed
`platform_fees` and `condition_multipliers`, seed 50 Jordans with
style_code/release_date/hype_tier, eBay developer keys.

## Phase 2 — Data pipeline (complete)

eBay puller, platform estimate fallback logic, cron refresh job, sanity
check against real listings. The StockX scraper was cut to v1.1. The data
source ended up being an Apify actor rather than eBay's Marketplace Insights
API. See `docs/comp_filtering_spec.md`, `docs/platform_multipliers.md`,
`docs/refresh_schedule.md`, and `docs/sanity_check.md`.

## Phase 3 — Backend core (next)

Auth via Supabase, search and sneaker detail endpoints, portfolio CRUD with
mark-as-sold and realized P/L, valuation engine (condition multiplier plus
uncertainty), recommendation engine (eligibility → fees → net payout → three
picks plus time-to-sell), per-size profitability and stats endpoints.

## Phase 3.5 — Projection engine

Technical model (trend and moving averages), Monte Carlo, blend into
bear/base/bull and cache to `projections`, HOLD/SELL logic with reasoning,
comps model using `lifecycle_curves`.

## Phase 4 — Frontend

Login/signup with protected routes, search and sneaker page (projection
cone chart, size grid, best size), add-pair flow (size, condition,
box/receipt, price, date), portfolio dashboard (P/L, charts, HOLD/SELL
summary), Where to Sell panel (three picks, table, greyed-out ineligible),
mark-as-sold and sales history.

## Phase 5 — Launch — target August 31

Deploy frontend to Vercel, deploy backend and cron to Railway or Render,
mobile testing, disclaimer + launch post.

## Descope candidates

If the schedule tightens, these are cut before the launch date moves, in
this order: per-size profitability endpoints, the comps model and
`lifecycle_curves`, and the technical model (leaving Monte Carlo alone as
the projection basis). Rationale: each is additive to the core valuation and
recommendation loop rather than load-bearing for it.
