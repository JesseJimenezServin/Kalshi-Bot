# Kalshi API notes

Verified 2026-10-22 against the official docs at <https://docs.kalshi.com>
(index: <https://docs.kalshi.com/llms.txt>; OpenAPI 3.34.0). Raw pages were
read directly, not summarised. Where a fact comes from anywhere else, it is
marked **UNVERIFIED** with its source.

Every section ends with **→ Impact**: what it changes in the build spec or
plan. The short list of spec-affecting findings is at the bottom.

---

## 1. Base URLs

Source: [API Environments](https://docs.kalshi.com/getting_started/api_environments.md), [Demo](https://docs.kalshi.com/getting_started/demo_env.md)

| | REST | WebSocket |
|---|---|---|
| **Live** (recommended) | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Live (also supported) | `https://api.elections.kalshi.com/trade-api/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| **Demo** (recommended) | `https://external-api.demo.kalshi.co/trade-api/v2` | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |
| Demo (also supported) | `https://demo-api.kalshi.co/trade-api/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

- "Credentials are not shared between environments": demo keys only work on
  demo, production keys only on production. Demo needs its own account
  (<https://demo.kalshi.co/>).
- Market-data GET endpoints (`/series`, `/markets`, `/events/{t}`) answer
  without authentication (confirmed live 2026-10-22). The WebSocket
  connection always requires authentication, even for public channels.

**→ Impact:** config maps `mode` to these four URLs. Paper mode can read live
market data. Decision A: paper reads **live** data, since demo markets and
liquidity aren't real.

## 2. Signing

Source: [API Keys](https://docs.kalshi.com/getting_started/api_keys.md), [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets.md)

Headers on every authenticated request:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | Key ID |
| `KALSHI-ACCESS-TIMESTAMP` | Unix time in **milliseconds**, as a string |
| `KALSHI-ACCESS-SIGNATURE` | base64(sign(timestamp + METHOD + path)) |

- The path includes the `/trade-api/v2` prefix and **excludes the query
  string**: `/trade-api/v2/portfolio/orders?limit=5` is signed as
  `/trade-api/v2/portfolio/orders`.
- WebSocket handshake: same headers, signing `timestamp + "GET" + "/trade-api/ws/v2"`.
- **Two key types.** **Ed25519 is now Kalshi's recommended default**, and RSA
  is still supported:
  - Ed25519: sign the UTF-8 string directly (64-byte signature).
  - RSA-2048: RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length.
- The PEM header does **not** identify the type (PKCS#8 RSA and Ed25519 both
  start `-----BEGIN PRIVATE KEY-----`). Detect it from the parsed key object.
- No example signature is published. RSA-PSS is randomised anyway, so a
  fixed vector would be meaningless for RSA.

**→ Impact:** `api/auth.py` supports both key types, detected from the parsed
key. The test is sign-then-verify with the matching public key for both
types, plus exact pre-sign string tests (query stripping, WS path). Ed25519
is deterministic, so a fixed-vector test is possible there. The spec says
"RSA private key"; supporting Ed25519 too costs nothing and is the default
for new keys.

## 3. Rate limits

Source: [Rate Limits](https://docs.kalshi.com/getting_started/rate_limits.md)

- Token bucket, **per account** (not per key). Most requests cost **10
  tokens**; non-default costs come from `GET /account/endpoint_costs`.
  Batch requests bill per item.
- Two independent buckets. **Read** covers GETs; **Write** covers order
  placement, amends and cancels.

| Tier | Read tokens/s | Write tokens/s |
|---|---|---|
| Basic | 200 (= 20 req/s) | 100 (= 10 req/s) |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |

- Basic read bucket holds one second of budget. Advanced and above write
  buckets hold three seconds.
- Exceeding a limit returns `429` with `{"error": "too many requests"}`.
  **No `Retry-After` header**, no penalty or cooldown. The docs say to use
  exponential backoff.
- Tier: `GET /account/limits` → `usage_tier`. Basic comes with signup;
  Advanced is self-serve via an upgrade endpoint.
- Sharded exchanges have per-shard write budgets (§11).

**→ Impact:** the token bucket defaults to Basic (20 reads/s, 10 writes/s)
and is overridden at startup from `/account/limits` plus endpoint costs.
Because limits are **per account**, this bot and the market maker share one
budget if they use the same account. The market maker is light, but the bucket should
leave headroom. **Measured 2026-10-22 on the owner's account (`GET /account/limits`):
`usage_tier: basic`, read refill 200/s with bucket capacity 600 (a 3 s burst,
more than the docs' stated 1 s), write refill 100/s with capacity 100.** Most
endpoints cost the default 10 tokens (`/account/endpoint_costs`).

## 4. Orderbook representation

Source: [Orderbook Responses](https://docs.kalshi.com/getting_started/orderbook_responses.md), [WS Orderbook Updates](https://docs.kalshi.com/websockets/orderbook-updates.md)

- **Bids only**, for both Yes and No. REST shape:
  `{"orderbook_fp": {"yes_dollars": [[price, count], ...], "no_dollars": [...]}}`
- Prices are **dollar strings** (`"0.4200"`), and counts are **fixed-point
  strings** (`"13.00"`, fractional allowed).
- Levels are sorted **ascending**, so the best bid is the **last** element.
- Asks are derived. A Yes bid at X is a No ask at 1 − X, so
  **best Yes ask = 1 − best No bid**, and **best No ask = 1 − best Yes bid**.
- WebSocket channel `orderbook_delta`:
  - `orderbook_snapshot` (`yes_dollars_fp`, `no_dollars_fp`) first, then
    `orderbook_delta` (`price_dollars`, `delta_fp`, `side`, `ts_ms`).
  - Every message carries `sid` (subscription id) and `seq`, which allows gap
    detection.
  - `get_snapshot` command re-fetches a book without resubscribing, which is
    the resync path.
  - `update_subscription` with `add_markets`/`delete_markets` manages the
    ticker set dynamically.
  - There is a **per-subscription market limit** (error code 26), but the
    number isn't published. It has to be found empirically.
- Keep-alive: Kalshi pings every 10 s. The `websockets` library answers
  automatically.

**→ Impact:** the spec's `yes_asks` column is derived (`1 − no bid`), not
received. Fractional sizes mean the market maker's phantom-level bug applies
(a cancelled level left at `5.55e-17`). Fix: decimal arithmetic, and remove a
level when its size is ≤ 0. If the per-subscription market limit is low, the
14:00 UTC burst (§10) may need several subscriptions.

## 5. Prices: whole cents are not guaranteed

Source: [Fixed-Point Representation](https://docs.kalshi.com/getting_started/fixed_point_migration.md)

- Prices: `*_dollars` strings, up to **4 decimals**. Integer-cent fields
  "cannot represent sub-cent prices".
- Each market has its own price grid: `price_ranges` (`[{start, end, step}]`)
  is "the source of truth". Off-grid prices are rejected. Ticks range from
  $0.01 down to $0.0001 depending on `price_level_structure`. A market's grid
  can **change**, announced on the lifecycle channel
  (`price_level_structure_updated`).
- Counts: `*_fp` strings, 0.01-contract granularity, fractional fills
  possible even on whole-contract orders.
- Live check 2026-10-22: **all 1,044 weather markets created in the previous
  3 days were `linear_cent`** (whole cents).

**→ Impact:** the spec's "all prices in cents (1–99)" holds for weather
today, but not universally. Internally, prices are `Decimal` dollars (or
integer units of $0.0001) and counts are `Decimal`. Orders snap to the
market's `price_ranges` step, and the "re-quote one cent lower" exit rule
becomes "one tick lower". Thresholds in config stay in cents for
readability. **No floats for prices or sizes.**

## 6. Orders (V2)

Source: [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2.md), [Changelog 2026-06-18](https://docs.kalshi.com/changelog/index.md)

- **The legacy `/portfolio/orders` mutation endpoints are deprecated.** They
  return "Please switch to the V2 endpoints". Use:
  - `POST /portfolio/events/orders` (create)
  - cancel / amend / decrease / batch V2 variants
  - `GET /portfolio/orders` for reading
- **V2 has no `action` field and no Yes/No side.** `side` is `bid` | `ask` on
  the **YES leg only**: `bid` = buy YES, `ask` = sell YES. "Selling YES is
  economically equivalent to buying NO at 1 − price."
- Required: `ticker`, `side`, `count` (fp string), `price` (dollar string),
  `time_in_force`, `self_trade_prevention_type`.
- `time_in_force`: `fill_or_kill` | `good_till_canceled` | `immediate_or_cancel`.
  GTC plus `expiration_time` (unix **seconds**) gives an exchange-enforced
  expiry. IOC can't take `expiration_time`.
- `self_trade_prevention_type`: `taker_at_cross` | `maker`.
- Optional: `client_order_id`, `post_only`, `reduce_only` (**only with IOC**),
  `cancel_order_on_pause`, `subaccount`, `exchange_index`, `order_group_id`.
- Response: `order_id`, `client_order_id`, `fill_count`, `remaining_count`,
  `average_fill_price`, `average_fee_paid` (per contract), `ts_ms`.

**→ Impact (significant):**
- "Buy No at 100 − yes_bid" is sent as **`side: ask` at `yes_bid`**. A No
  position is a short YES position. **Positions are stored as signed YES
  contracts.**
- The spec's `orders.side` + `orders.action` collapse to `bid`/`ask`. The
  schema keeps a human-readable `intent` column (`buy_yes`/`buy_no`/`sell_yes`/`sell_no`).
- Entry timeout (30 s): GTC with `expiration_time = now + 30`, plus a cancel
  as backup, since expiry has 1-second resolution.
- Exits on a position can use `reduce_only` + IOC, so a fill can never flip
  the position.

## 7. Fees

Sources: [Fee Rounding](https://docs.kalshi.com/getting_started/fee_rounding.md), [Get Series](https://docs.kalshi.com/api-reference/market/get-series.md) (`fee_type`, `fee_multiplier`), [Changelog 2026-08-20/22](https://docs.kalshi.com/changelog/index.md)

**Confirmed from the API docs:**
- Each series has `fee_type` ∈ {`quadratic`, `quadratic_with_maker_fees`,
  `quadratic_with_combo_maker_fees`, `flat`} and a `fee_multiplier`. These
  point to the fee schedule PDF for the actual tables.
- **Maker fees went live 2026-08-20.** The standard maker multiplier is
  `0.25`, and combo series use `0.5`.
- Rounding: the trade fee is rounded **up to $0.000001**. A rounding fee then
  brings the balance to the member's precision ($0.0001 direct members, $0.01
  non-direct). An **accumulator per order** rebates overpayment so total fees
  converge to what one equivalent fill would cost. **Fees are therefore
  per-order, not per-contract.**
- Live check 2026-10-22: **all 458 Climate and Weather series are
  `quadratic`, multiplier 1.**

**UNVERIFIED (kalshi.com blocks automated fetches with HTTP 429). Source: a
search result quoting "Fee Schedule for July 2026 – 7.7.26 Update",
<https://kalshi.com/docs/kalshi-fee-schedule.pdf>:**
- Taker fee = round_up(M × **0.07** × C × P × (1 − P))
- Maker fee = round_up(M × **0.0175** × C × P × (1 − P)) (= 0.07 × 0.25,
  consistent with the changelog's 0.25 maker multiplier)
- P = price in dollars, C = contracts, M = series multiplier.

**→ Impact:** `fee(price, contracts, fee_type, multiplier, is_taker)` takes
the series' `fee_type`/`fee_multiplier` from the API rather than
hard-coding. The threshold check uses total fee ÷ count. At P = 0.50 the
taker fee is 1.75¢/contract; at P = 0.10 it's 0.63¢. Small next to a 20¢
threshold, but not zero. **Owner action: open the PDF in a browser and
confirm the 0.07 / 0.0175 coefficients and the rounding rule before Phase 4.**
Entries "at the ask" are taker fills. Whether `quadratic` series charge maker
fees on resting exits needs confirming from the PDF too.

## 8. New-market discovery

Sources: [Market & Event Lifecycle](https://docs.kalshi.com/websockets/market-and-event-lifecycle.md), [Get Markets](https://docs.kalshi.com/api-reference/market/get-markets.md)

- **A lifecycle channel exists: `market_lifecycle_v2`.** Events: `created`,
  `activated`, `deactivated`, `close_date_updated`, `determined`, `settled`,
  plus `price_level_structure_updated` and `metadata_updated`. Fields include
  `open_ts`, `close_ts`, `result`, `settlement_value`, `exchange_index`.
  There's no ticker filter (it receives everything), and it also sends event
  creation notifications.
- `GET /markets` filters: `min_created_ts`/`max_created_ts`,
  `min_close_ts`/`max_close_ts`, `min_settled_ts`/`max_settled_ts`, `status`,
  `series_ticker`, `event_ticker`, `limit` ≤ 1000, cursor pagination.
- `status` filter values are `unopened` | `open` | `closed` | `settled` (not the
  Market.status enum). Only one status at a time, and timestamp filters only
  combine with certain statuses. `mve_filter=exclude` drops combo (multivariate)
  markets.
- Live check 2026-10-22: **more than 60,000 markets were created in 3 days
  across all categories** (combo markets dominate). Paging all of them every
  5 s is infeasible.

**→ Impact (significant):**
- Discovery uses `market_lifecycle_v2` as the primary source. The fallback
  poll goes **per weather series** (`series_ticker` + `min_created_ts`),
  never a full scan.
- **"New" must key off `open_time` / `activated`, not creation.** Weather
  markets are created a median **4.4 hours before they open** (§10). The
  spec's definition ("open time within 10 min") already says open time; the
  implementation must not use `created`.
- The same channel delivers `determined`/`settled` with `result`, which
  feeds the status/result updater (plan 2.7) for free.

## 9. Category, events, strikes

Sources: [Get Event](https://docs.kalshi.com/api-reference/events/get-event.md), [Get Series](https://docs.kalshi.com/api-reference/market/get-series.md), [Get Market](https://docs.kalshi.com/api-reference/market/get-market.md)

- **Category lives on the series** (`category`, plus a `categories` list).
  `event.category` is deprecated. Markets don't carry a category.
- The live category name is **`Climate and Weather`** (458 series), not
  `weather`.
- `event.mutually_exclusive` is true when `collateral_return_type` is
  `MECNET`: at most one market in the event can resolve Yes.
- Market strike fields: `strike_type` ∈ {`greater`, `greater_or_equal`,
  `less`, `less_or_equal`, `between`, `functional`, `custom`, `structured`},
  `floor_strike`, `cap_strike`.
- Market status enum: `initialized`, `inactive`, `active`, `closed`,
  `determined`, `disputed`, `amended`, `finalized`.
- Example (live, 2026-10-22), `KXHIGHPHIL-26OCT23` ("Highest temperature in
  Philadelphia on Oct 23"), `mutually_exclusive: true`, six markets:
  - `less 59`
  - `between 59–60`, `61–62`, `63–64`, `65–66`
  - `greater 66`

  Mids summed to 100.5¢ with 1¢ spreads about 8 hours after open.
- `rules_primary` names the resolution source: "...according to **The
  Weather Company**".

**→ Impact:**
- Config's `include_categories: [weather]` becomes `["Climate and Weather"]`,
  matched against series categories, with a cached series lookup.
- **Daily temperature events are mutually exclusive bucket sets, not "above
  X" ladders.** The sibling model's ME rule (100 − sum of others) is the
  primary one for weather. The ladder/monotonicity rule applies to
  `greater`-style series without buckets. Both are still built.
- External weather adapter (decision E): resolution is by The Weather
  Company, **not NWS**. An NWS-based adapter would model a slightly different
  quantity, so it needs to be calibrated, not trusted outright.

## 10. Weather market flow (measured)

Public API, markets created 2026-10-19 → 10-22, 352 Climate and Weather
series queried:

| Metric | Value |
|---|---|
| New weather markets | **1,044 in 3 days ≈ 348/day** |
| Events | 155 (≈ 52/day) across 55 active series |
| Biggest series | `KXRAIN` (105), then the daily `KXHIGH*` / `KXLOWT*` city series (18 each = 6 buckets × 3 days) |
| Price grid | 100% `linear_cent` |
| Strike types | between 576, greater 294, less 144, greater_or_equal 30 |
| Created → open | median **15,940 s (4.4 h)**, min 280 s |
| Open time | **83% open at exactly 14:00 UTC** (10:00 ET) |

**→ Impact:** discovery is bursty. Around 290 markets (about 48 events)
become new within one minute daily, then the watchlist holds them for
`watch_duration_min`. This is also when any mispricing is most likely, and
when the WS market limit (§4) and REST read budget (§3) matter most. The
Phase 2 recording has to capture the first minutes after 14:00 UTC at high
resolution.

## 11. Exchange sharding

Source: [Exchange Sharding](https://docs.kalshi.com/getting_started/exchange_sharding.md)

- Trading is split across matching engines. Weather is on **shard 0**, the
  catch-all. Crypto/Commodities are shard 2, some Sports shard 3, combos
  shard 1.
- **Balances are per shard**, and programmatic traders must fund the shard
  before ordering. `GET /portfolio/balance` breaks balances down by
  `exchange_index`.
- Orders auto-route by ticker if `exchange_index` is omitted. Auto-routed
  writes are billed to more write buckets than explicit ones.

**→ Impact:** the risk manager's balance check reads the **shard-0** balance
for weather, not the account total. Orders set `exchange_index` from the
market's own field (avoiding auto-routing's extra billing and latency).

## 12. Fills, positions, historical data

Sources: [User Fills](https://docs.kalshi.com/websockets/user-fills.md), [Market Positions](https://docs.kalshi.com/websockets/market-positions.md), [Historical Data](https://docs.kalshi.com/getting_started/historical_data.md)

- `fill` channel fields: `trade_id`, `order_id`, `client_order_id`,
  `market_ticker`, `side`, `book_side`, `purchased_side`, `yes_price_dollars`,
  `count_fp`, `fee_cost`, `is_taker`, `post_position_fp`, `ts_ms`. The
  `market_positions` channel streams position, cost, realized P&L and fees.
- **Live vs historical tiers:** settled markets, trades, fills and orders
  older than `GET /historical/cutoff` move to the `/historical/*` endpoints
  and vanish from the live ones. Events and series always stay available.

**→ Impact:** the historical model's backfill reads **both** `/markets` and
`/historical/markets`. Startup reconciliation (Phase 6) reads live and
historical fills/positions, deduplicating as the docs describe. `fee_cost`
and `is_taker` on fills give the real fee for each fill, which is better
than recomputing it.

## 13. Automated trading rules and position limits

- API use is permitted for a member's own trading.
  **UNVERIFIED**, from a search summary quoting Kalshi Developer Agreement
  v1.1 §3: "Use of Kalshi APIs is expressly limited to facilitating a
  member's own trading on the Exchange; all other usages are disallowed..."
- Spoofing, wash trading and misuse of nonpublic information remain
  prohibited (Rulebook).
- Position limits: Rulebook 5.14(a), **$25,000 per contract** for most
  retail contracts (UNVERIFIED, search summary of the Rulebook / CFTC
  filing). Irrelevant at `max_dollars_per_trade: 25`, but the risk manager
  will enforce it as an absolute ceiling anyway.
- Self-trade prevention is a required order field (§6), so the bot can't
  accidentally wash-trade against itself.

**→ Impact:** nothing in the strategy conflicts. **Owner action: read the
Developer Agreement and Member Agreement at
<https://kalshi.com/regulatory/agreement> before Phase 6** (kalshi.com
blocks automated reads).

## 14. Official SDK

Source: [SDKs](https://docs.kalshi.com/sdks/overview.md), PyPI

- `kalshi_python_async` 3.34.0 (2026-10-13) is current and async, but built
  on **aiohttp**, not httpx, and covers REST only. `kalshi-python` is
  deprecated.
- The docs themselves: "SDKs ... may lag the API ... For production, we
  recommend generating your own client from those specs — or integrating
  directly."

**→ Decision:** a thin client on **httpx**, written against
`https://docs.kalshi.com/openapi.yaml` and `asyncapi.yaml`. This follows the
spec's fallback rule and Kalshi's own advice.

---

## Findings that change the spec

1. **V2 orders** use `bid`/`ask` on the Yes leg, and the legacy endpoints are
   gone. Buy No = `ask` at yes_bid. Positions are signed Yes contracts.
2. **"Prices are 1–99 cents" isn't guaranteed.** Each market has its own
   grid. Weather is whole-cent today. Use Decimal throughout, snap to
   `price_ranges`, and read "1 cent" as "1 tick".
3. **Discovery uses the `market_lifecycle_v2` WebSocket channel.** A full
   `/markets` scan is impossible (60k+ new markets per 3 days). "New" means
   just **opened**, not just created (created about 4.4 h earlier).
4. **The category is "Climate and Weather"**, and lives on the series.
5. **Weather events are mutually exclusive bucket sets**, so the ME sibling
   rule is the main one.
6. **Fees are per series and per order, and maker fees now exist.** The
   formula is unverified until the PDF is checked.
7. **Balances are per exchange shard.** Weather is shard 0.
8. **The daily 14:00 UTC burst**: about 290 markets open in one minute.
9. **Ed25519 keys** are now the default. Auth supports both.
10. **Rate limits are per account**, shared with the market maker if the
    same account is used.

## Owner actions

- [ ] Open <https://kalshi.com/docs/kalshi-fee-schedule.pdf> and confirm the
      fee coefficients and rounding (§7), before Phase 4.
- [ ] Read the Developer and Member Agreements (§13), before Phase 6.
- [ ] Create a demo account and API key, and a new production key, before
      Phase 1 acceptance (1.12).
