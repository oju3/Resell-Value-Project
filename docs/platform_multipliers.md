## Purpose

`sold_comps` holds eBay sold data only. To recommend where a user should sell,
the engine needs to estimate what a shoe would fetch on StockX and GOAT from
the eBay figure. That estimate is a **price multiplier**, stored in
`platform_multipliers`, and it is a separate concern from `platform_fees`:
fees affect what a seller nets, the multiplier affects what the item sells
for. Conflating the two into one number would be wrong — this document and
the schema keep them as two stages.

This document covers storage and measurement only. No projection or
recommendation logic — that's Phase 3.

## The measurement (2026-08-03)

eBay deadstock medians (`condition_id = 1000`, from `sold_comps`) compared
against StockX size-10 last-sale prices, for 12 sneakers:

| Sneaker | Tier | eBay | StockX | Ratio |
|---|---|---|---|---|
| J1 Low Travis Scott Reverse Mocha | 1 | 1450.00 | 2350 | 1.62 |
| J4 Union LA Off Noir | 1 | 599.50 | 763 | 1.27 |
| J4 Union LA Guava Ice | 1 | 450.00 | 793 | 1.76 |
| J4 Nike SB Pine Green | 1 | 440.00 | 603 | 1.37 |
| J1 High '85 Bred | 2 | 377.50 | 565 | 1.50 |
| J4 Military Black | 2 | 295.00 | 596 | 2.02 |
| J4 Fire Red | 2 | 232.50 | 456 | 1.96 |
| J4 Bred Reimagined | 2 | 201.25 | 408 | 2.03 |
| J3 True Blue | 3 | 209.99 | 310 | 1.48 |
| J1 High Palomino | 3 | 139.99 | 267 | 1.91 |
| J1 High UNC Reimagined | 3 | 109.99 | 154 | 1.40 |
| J1 High Satin Bred (W) | 3 | 100.00 | 101 | 1.01 |

Median across all 12: **1.50**. Interquartile range: **1.40–1.91**.

## What's stored (`platform_multipliers`)

| platform | multiplier | band | n | method | confidence | is_proxy | proxy_source |
|---|---|---|---|---|---|---|---|
| ebay | 1.00 | — | — | — | high | false | — |
| stockx | 1.50 | 1.40–1.90 | 12 | `ebay_deadstock_median_vs_stockx_size10_last_sale` | low | false | — |
| goat | 1.50 | — | — | `proxied_from_stockx` | none | true | stockx |

- **eBay** is stored as a row (multiplier 1.00, confidence high) rather than
  only asserted in prose, so `SELECT multiplier WHERE platform = 'ebay'`
  returns a row instead of nothing. It's a definitional reference, not a
  measurement.
- **StockX** is the measured row above.
- **GOAT** copies StockX's 1.50 value as a placeholder. GOAT was never
  sampled. `confidence = 'none'` and `is_proxy = true` make this explicit and
  queryable rather than a fact that only lives in a comment.

## How the multiplier is applied

```
platform_price = ebay_deadstock_median × multiplier
net_payout     = platform_price × (1 − fee_percent) − fixed_fee
```

eBay is the numeraire. The multiplier converts an eBay price into an
estimated price on the target platform, never the reverse. `fee_percent` and
`fixed_fee` come from `platform_fees` — a separate table for a separate
stage. Multiplier and fee must not be collapsed into one number.

## Band derivation

The 1.40–1.90 band is the interquartile range of the 12 observed ratios
(1.40–1.91), rounded. Stating the rule here makes the band reproducible from
the raw ratios above rather than an arbitrary-looking pair of numbers.

## Limitations

- **n=1 per shoe on the StockX side.** Each ratio rests on a single StockX
  transaction. The eBay side is a median of 13–20 comps with IQR fencing; the
  ratio inherits the weaker side's noise.
- **The order book is thin.** Jordan 4 SB Pine Green showed consecutive
  size-10 sales of $352 and $603 — a 71% swing on the same shoe, same size,
  back to back. That single observation demonstrates why n=1 can't support
  tier-level conclusions.
- **Tier differentiation was tested and rejected.** Three-tier medians came
  out T1 1.50 / T2 1.99 / T3 1.44; a two-tier split (T1+T2 1.69 vs T3 1.44)
  was also tested. In both cases variance within groups exceeded the
  difference between groups — the signature of no detectable effect at this
  sample size. A single global multiplier is used instead.
- **Theory predicts a tier effect the data can't yet confirm.** Satin Bred
  (W), the cheapest shoe sampled at $100, showed essentially no premium
  (1.01). If the premium tracks counterfeit risk it should scale with value —
  plausible, unproven here.
- **Not like-for-like on size.** eBay medians span all sizes (size is null on
  ~70% of `sold_comps` rows); StockX figures are size 10 specifically. An
  approximation, not a clean comparison.
- **The spread is a composite, not one mechanism.** Contributing factors from
  industry sources and seller reports: StockX aggregates international
  demand (roughly half its business is non-US) that eBay US sellers don't
  reach; authentication trust; no-negotiation fixed pricing; concentrated
  sneaker-specific traffic versus eBay's general marketplace. The multiplier
  is empirical and is not attributed to any single cause.
- **It may compress over time.** eBay acquired Sneaker Con's authentication
  business and expanded its Authenticity Guarantee programme, narrowing the
  trust gap. A multiplier measured in August 2026 should not be assumed
  permanent.

## v1.1 improvements

- Sample StockX sales history properly and compute medians with the same IQR
  fencing used for eBay comps, rather than taking single last-sale points.
- Measure GOAT directly instead of proxying from StockX.
- Hold size constant on both sides.
