## Purpose

This document defines the rules for cleaning raw eBay sold-listing comps — scraped via the caffein.dev/ebay-sold-listings Apify actor — before they're written into `price_history`. `condition_multipliers` anchors all six condition tiers on a single deadstock baseline (multiplier = 1.00), so these rules exist to keep that baseline, and the comp stream feeding it, free of non-representative or duplicate data points.

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
  *— baseline anchors all six derived tiers; contaminating it propagates error everywhere*
- `conditionId` 3000 maps to the used tiers.

### Aggregation

- Use median, never mean, then apply 1.5×IQR fencing.
- Require ≥5 comps post-filter, else flag `low_confidence`.

### Storage

- Persist `listingType`; flag auction listings rather than excluding them.
  *— auctions clear below Buy It Now; keep separable rather than pre-excluded*

### Auditing

- Log every rejected row with the rule that rejected it. No silent discards.
  *— if a sneaker returns zero comps, that must be diagnosable*

## Known limitations

- Per-seller concentration can't be capped or detected. No seller field is returned by the actor, so the bulk-seller pattern caught by thumbnail dedup (one seller holding 12% of the 25-comp sample) can't be generalized into a systematic per-seller cap.
- Condition sub-tiers below "Pre-Owned" can't be distinguished. eBay only exposes three `conditionId` values (1000 Brand New, 1500 New Other, 3000 Pre-Owned), but `condition_multipliers` has six tiers anchored on deadstock = 1.00. Everything eBay buckets as 3000 is filtered as one undifferentiated group — vnds, worn_once, fair, and beat aren't separable from source data.

## Open questions

- **Model-variant contamination — BLOCKED.** A Mid listing was observed at $144 against an OG median of ~$205 for the same colourway. Filtering this requires distinguishing OG/High from Mid/Low, which needs a model-variant field on the `sneakers` table. Checked: no such field exists — `sneakers` has `id`, `name`, `brand`, `style_code`, `colorway`, `image_url`, `release_date`, `hype_tier`, none of which encode model variant.
- No other items marked BLOCKED. Every other rule above maps to a field returned by the Apify actor (`title`, `conditionId`, `thumbnailUrl`, `listingType`, `soldPrice`). The one hard field-level gap — no seller identifier — is a permanent data-source constraint, not an open design question, and is filed under Known limitations instead.
