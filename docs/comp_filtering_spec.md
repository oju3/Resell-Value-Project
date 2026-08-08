## Purpose

This document defines the rules for cleaning raw eBay sold-listing comps — scraped via the caffein.dev/ebay-sold-listings Apify actor — before they're written into `price_history`. The deadstock baseline (`conditionId` 1000) is what the product values, so these rules exist to keep that baseline, and the comp stream feeding it, free of non-representative or duplicate data points. The baseline was previously described here as the anchor for all six `condition_multipliers` tiers; the MVP values deadstock only and those derived tiers have no consumer — see `docs/scope_deadstock_only.md`. The filtering rules below are unaffected either way.

## Data source

Comps come from the Apify actor `caffein.dev/ebay-sold-listings`, not eBay's official API — the Marketplace Insights API is limited-release and we couldn't get access to it.

All data is from eBay.com (US), priced in USD.

Available fields: `keyword`, `itemId`, `title`, `condition`, `conditionId`, `endedAt`, `soldPrice`, `soldCurrency`, `listingType`, `isBestOfferAccepted`, `shippingPrice`, `shippingType`, `totalPrice`, `url`, `thumbnailUrl`, `fullResThumbnailUrl`, `buyingFormat`.

Known gaps:
- Size is not a returned field. It appears inconsistently inside `title` and is absent from ~70% of listings.
- No seller identifier is returned.
- `conditionId` only exposes three values (1000, 1500, 3000), not the six-tier granularity `condition_multipliers` uses.

Every rule below was derived by manually inspecting a 25-comp sample for the Jordan 4 Bred Reimagined (FV5029-006).

## Scrape parameters

`min_price` passed to the Apify actor is scaled by `hype_tier`, not constant:

- Tier 1 → 150
- Tier 2 → 120
- Tier 3 → 80

`min_price` is a cost-saving pre-filter to avoid paying to scrape kids/youth listings — it is **not** the kids filter (that's the title regex under Exclusions). A flat $150 floor was tested against tier-3 inventory and found to exclude most of the real market: UK sold/active data for Jordan 1 UNC Reimagined showed brand-new pairs clustering at £83–£115 (~$108–150 USD), meaning a $150 floor would capture only the expensive tail and inflate the median by an estimated 40–50% while still appearing valid. Tier-scaled floors keep the cost saving without truncating the distribution.

## Rules

### Exclusions

Exclusion matching uses word-boundary regex, not substring — matching by substring produces false positives (`\bGS\b` as a substring matches "leggings" and "wings").

- Exclude titles matching `\bGS\b`, `\(PS\)`, `\(TD\)`, `\bYouth\b`, `\bToddler\b`, `Big Kids`, `Little Kids`, `Preschool`.
  *— 2.6x price gap observed between PS and men's on identical colourway ($89 vs $233 equivalent). Largest single source of contamination.*
- Exclude titles matching `lot of`, `2 pairs`, `bundle`.
  *— aggregate price, not unit price*

### Size

- Extract US men's size from `title`. Accepted patterns: `Size 10`, `Men's Size 11.5`, `Sz 9`, `Size 10.5`.
- For dual-sized listings like `Size 12.5M/14W`, take the M (men's) value and discard the W (women's) value.
  *— the model is built on US men's sizing*
- Ignore any UK or EU sizes in titles (`UK 8`, `EU 42.5`).
  *— this dataset is eBay.com US, so US sizing is the only valid scale*
- Drop any size below US men's 7. A Y-suffixed size (e.g. `7Y`) is treated as youth regardless of its numeric value.
  *— 7Y overlaps men's 7*
- Accept nulls; never drop a row for missing size.
  *— ~70% null rate; dropping nulls discards most of the sample*

### Deduplication

- Dedupe on `thumbnailUrl`.
  *— 3 of 25 comps shared thumbnail `g/AJkAA`, identical $32.32 shipping, sequential item IDs — one bulk seller holding 12% of the sample*

### Condition

- `conditionId` 1000 maps to the deadstock baseline.
- `conditionId` 1500 is persisted but excluded from the baseline.
  *— the baseline is the valuation figure the product actually reports; contaminating it with New Other listings biases every number downstream*
- `conditionId` 3000 is excluded from valuation entirely. It previously mapped to the used tiers, which the MVP no longer values (`docs/scope_deadstock_only.md`). It is a non-issue in practice regardless: `apify_backfill.py` sends `itemCondition: "new"`, so `sold_comps` holds zero rows at 3000. Any that slip through are persisted and ignored, not dropped — keeping the raw `conditionId` is what lets used valuation return in v1.1 without re-scraping.

### Aggregation

- Use median, never mean, then apply 1.5×IQR fencing.
- Require ≥5 comps post-filter, else flag `low_confidence`.

### Storage

- Filtered comps are written to `sold_comps`, one row per surviving comp — not to `price_history`. `price_history`'s `UNIQUE (sneaker_id, platform, size, condition_type, date)` is an aggregate time series; individual sold comps don't belong there. Aggregating `sold_comps` into `price_history` (median, IQR fencing) is a separate downstream step, not part of this filtering pass.
- Persist `listingType`; flag auction listings rather than excluding them.
  *— auctions clear below Buy It Now; keep separable rather than pre-excluded*

### Auditing

- Log every rejected row with the rule that rejected it. No silent discards.
  *— if a sneaker returns zero comps, that must be diagnosable*
- `comp_rejections` is strictly per-listing: one row per individual comp that failed a filter rule above, with a real `item_id`/`title`. It does not hold per-sneaker or per-run bookkeeping — e.g. the recurring refresh job's (`scripts/refresh_comps.py`) stall detection logs to a separate `refresh_runs` table instead (see `docs/refresh_schedule.md`), specifically so a `rejection_rule` count grouped from this table always means "count of rejected listings," never a mix of listing-level and run-level events.

## Known limitations

- Per-seller concentration can't be capped or detected. The actor does return a `sellerUsername` field, but it was null in 20/20 observed rows (DM7866-162 cache), so it carries no usable signal — the bulk-seller pattern caught by thumbnail dedup (one seller holding 12% of the 25-comp sample) can't be generalized into a systematic per-seller cap. Deduplication falls back to `thumbnailUrl` instead.
- Condition sub-tiers below "Pre-Owned" can't be distinguished. eBay only exposes three `conditionId` values (1000 Brand New, 1500 New Other, 3000 Pre-Owned), but `condition_multipliers` has six tiers anchored on deadstock = 1.00. Everything eBay buckets as 3000 is filtered as one undifferentiated group — vnds, worn_once, fair, and beat aren't separable from source data.

## Open questions

- **Model-variant contamination — BLOCKED.** A Mid listing was observed at $144 against an OG median of ~$205 for the same colourway. Filtering this requires distinguishing OG/High from Mid/Low, which needs a model-variant field on the `sneakers` table. Checked: no such field exists — `sneakers` has `id`, `name`, `brand`, `style_code`, `colorway`, `image_url`, `release_date`, `hype_tier`, none of which encode model variant.
- **RM lifestyle-model contamination — related to the model-variant item above, but distinct.** Observed live during the 2026-08-04 manual verification for `docs/sanity_check.md`: a "Jordan 4 RM Black Cat" listing appeared in results for the Jordan 4 Black Cat query, priced at roughly a third of the observed market median. RM is a separate Jordan lifestyle model, not a colourway of the OG Jordan 4 — unlike the Mid/OG case above, this may be distinguishable by title keyword alone rather than needing a model-variant field, but that's unconfirmed against only one observed listing, and no current exclusion rule covers it. Not yet triaged as BLOCKED or as a simple addition to the word-boundary Exclusions list above; needs the same manual-sample inspection given to the existing GS/PS/Youth patterns before a regex is written, to avoid a false-positive risk symmetrical to the `\bGS\b`-as-substring lesson already documented in this file.
- **Cross-colourway keyword contamination — BLOCKED.** eBay keyword search matches loosely — a listing titled "Jordan 4 Union LA mid Off Noir" was returned by the Guava Ice query and attributed to Guava Ice under first-write-wins on item_id. Shoes sharing name fragments (Union LA, Reimagined, Fire Red) are exposed. The full backfill confirms this is more severe than a single row: Jordan 5 A Ma Maniere Violet Ore had 6 of its 20 comps (30%) already claimed by A Ma Maniere Dusk, processed one call earlier — both share the "Jordan 5 A Ma Maniere" prefix. First-write-wins means the earlier-processed sneaker absorbs the later one's comps, contaminating both medians, not just the later one's. Affected pairs observed in the backfill: the two A Ma Manieres, the two Union LA 4s, Jordan 3 vs Jordan 1 True Blue, and Fire Red across models 3 and 4. Requires a title-to-SKU matching step, not just a keyword query. Deferred to Phase 3.
- No other items marked BLOCKED. Every other rule above maps to a field returned by the Apify actor (`title`, `conditionId`, `thumbnailUrl`, `listingType`, `soldPrice`). The one hard field-level gap — no seller identifier — is a permanent data-source constraint, not an open design question, and is filed under Known limitations instead.
