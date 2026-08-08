# Scope: deadstock-only valuation for v1

Decided 2026-08-07.

## The decision

The MVP values deadstock/new sneakers only. Used-condition valuation is
deferred to v1.1.

Nothing is removed from the schema. `condition_multipliers`,
`owned_sneakers.condition`, `platform_fees.min_condition`, and
`sold_comps.condition_id` all stay exactly as they are — unused for now, and
documented as such here and at each definition site.

## Why

### The multipliers are assumptions, not measurements

The six-tier ladder — deadstock 1.00, vnds 0.87, worn_once 0.78, good 0.65,
fair 0.50, beat 0.35 — was seeded on 2026-07-30 in commit `2bd5743`, whose
message describes them as "starting" values needed to unblock downstream
work. A repo-wide search found no documented derivation anywhere: no comment,
no docstring, no doc states where any of the six numbers came from.

That is the whole of their provenance. They were placeholders that were never
replaced, and shipping a valuation engine on top of them would mean shipping
a number the product cannot explain.

### They can't be validated from the current data source

eBay exposes three `conditionId` values — 1000 (Brand New), 1500 (New Other),
3000 (Pre-Owned). Five of the six tiers collapse into 3000 and are
permanently indistinguishable from one another. vnds, worn_once, good, fair,
and beat are not separable from source data, so there is no way to measure
what any of those five multipliers should be.

It is worse than that in practice: `sold_comps` contains **zero rows** at
`conditionId` 3000, because `scripts/apify_backfill.py` sends
`itemCondition: "new"` on every actor call. The used data was never
collected. Validating the used tiers is not a matter of analysing what we
have — there is nothing to analyse.

### Restricting to deadstock costs nothing

Filtering to `conditionId` 1000:

- 870 of 1030 `sold_comps` rows survive.
- All 50 sneakers retain 5 or more deadstock comps (min 8, max 26, mean 17.4).
- Zero sneakers fall below the low-confidence threshold.

The 160 excluded rows are `conditionId` 1500, which
`docs/comp_filtering_spec.md` already excludes from the baseline. So the
deadstock baseline the aggregation step consumes is unchanged by this
decision — those 870 rows were already the baseline.

### The separation of concerns

What a sneaker is worth is a market question — supply, hype, restock
frequency — and it is model-able from real data. How much wear reduces that
value is a per-pair, subjective question that can't be verified remotely and
that the data source can't answer.

The product models the first. The second was a fudge factor sitting on top of
it.

## What stays in the schema, unused

| Object | Status under deadstock-only |
| --- | --- |
| `condition_multipliers` (table) | Retained, unread. Deadstock is the 1.00 identity row, so a deadstock-only valuation multiplies by 1.00 or skips the lookup entirely. |
| `owned_sneakers.condition` | Retained, nullable, unconstrained. An added pair is deadstock by definition in v1. |
| `platform_fees.min_condition` | Retained. See the consequence below — it becomes a no-op, not a no-longer-needed column. |
| `sold_comps.condition_id` | Retained and still written on every scrape. Keeping the raw `conditionId` means used tiers could be re-enabled later without re-scraping anything already collected. |

## Consequence: platform eligibility has nothing left to gate

`platform_fees.min_condition` is the platform eligibility gate. StockX
requires `deadstock`, GOAT requires `good`, eBay accepts `any`.

Under deadstock-only, every pair in the product clears every gate. That means
Phase 4's "greyed-out ineligible platforms" panel has nothing to grey out —
all three platforms are always eligible, and the Where to Sell panel reduces
to ranking by net payout alone.

The gate stays in the schema. It becomes live again the moment used valuation
returns, without a migration.

## What adding used valuation later would require

Two things, both currently missing:

1. **A data source that exposes used sold prices.** Either a different actor
   or scrape configuration that actually returns `conditionId` 3000 listings,
   or a source with finer condition granularity than eBay's three values.
   Without this there is no measurement to make.
2. **Measured multipliers rather than assumed ones.** The six current numbers
   would need to be replaced by values derived from real used-vs-deadstock
   price ratios, with a recorded sample size and measurement date.

`platform_multipliers` is the model to follow here. It carries `sample_size`,
`method`, `confidence`, `is_proxy`, `proxy_source`, `measured_date`, and
`notes` — and it records honestly where a value is a placeholder rather than
a measurement. `condition_multipliers` has none of that structure today. Any
future used-valuation work should add it before the numbers are trusted.

Note that eBay's three-value `conditionId` will still not distinguish vnds
from worn_once from good from fair from beat. A source-level fix is required
first; a finer ladder cannot be recovered from the existing feed no matter
how the data is analysed.
