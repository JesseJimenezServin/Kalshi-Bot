# Build plan — Kalshi New-Market Mispricing Bot

Written 2026-10-22 from the build spec. Each phase ends in a **stop**: results
are shown and approved before the next phase starts. The bot stays in paper
mode until explicit approval; live trading is never switched on by Claude.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

---

## Phase 0 — Setup and API research (no bot code)
- [x] 0.1 Create `kalshi_bot/` with its own git repo; `.gitignore` covers `.env`, `*.pem`, `data/`, `KILL`.
- [x] 0.2 `pyproject.toml` (Python 3.11+, pinned spec libraries), venv.
- [x] 0.3 Confirm REST and WebSocket base URLs, demo and live.
- [x] 0.4 Confirm the signing method: string signed, headers, whether the query string is included; find an example signature.
- [x] 0.5 Confirm orderbook representation (bids-only for Yes/No? fixed-point fractional sizes?) and tick size (sub-cent markets?).
- [x] 0.6 Confirm order fields: side, action, type, price units, client order ID, time-in-force, post-only.
- [x] 0.7 Confirm the fee formula: maker vs taker, rounding, per-series exceptions.
- [x] 0.8 Confirm rate limits; check the account tier via the account-limits endpoint.
- [x] 0.9 Is there a lifecycle / new-market WebSocket channel? If not, which list filters (created-after, status) avoid paging every open market every 5s?
- [x] 0.10 Where "category" lives: market, event or series.
- [x] 0.11 Rules on automated trading and position limits.
- [x] 0.12 Official Python SDK: async and current? Otherwise a thin client.
- [x] 0.13 Write findings with links to `docs/API_NOTES.md`. Stop and report anything that contradicts the spec.

**Phase 0 result (2026-10-22):** see `docs/API_NOTES.md`. Ten findings change
the spec (summary at the bottom of that file). 0.7 and 0.11 rest partly on
unverified secondary sources, because kalshi.com blocks automated reads, and
are listed as owner actions. 0.8 tier measured with the owner's key: Basic.

## Phase 1 — Foundation
- [ ] 1.1 `config.yaml` + pydantic models for every section, with range validation.
- [ ] 1.2 Secrets via pydantic-settings. Mode gating: paper default; demo explicit; live needs `mode: live` AND `CONFIRM_LIVE_TRADING=yes`, else refuse to start.
- [ ] 1.3 Config validation tests: missing values, out-of-range, live without confirmation.
- [ ] 1.4 structlog JSON logging.
- [ ] 1.5 `api/auth.py`: key loading, signed headers. Test against the docs' example, or a sign-then-verify round trip.
- [ ] 1.6 `api/rest.py` core: httpx async, base URL by mode, token-bucket rate limiting, backoff on 429/5xx.
- [ ] 1.7 Pydantic response models: market, event, orderbook, balance, position, order, fill.
- [ ] 1.8 Read methods with pagination; tests on a mocked HTTP transport.
- [ ] 1.9 `place_order` / `cancel_order` accept only an `Approved` object that only the risk manager can create. Mock-tested; unused until Phase 6.
- [ ] 1.9b **Write guard.** Paper mode reads production with a key that *can* trade, so the REST client refuses every non-GET request unless mode is `demo` (demo URL) or `live` with `CONFIRM_LIVE_TRADING=yes`. Enforced in the client itself, below the risk manager, and tested. Same idea as the liquidity lab's GET-only guard.
- [ ] 1.10 `storage/models.py` (all 8 tables, Postgres-portable) + `storage/repo.py`; SQLite WAL; tests on a temp DB.
- [ ] 1.11 CLI skeleton: `run`, `kill`, `report`, plus `check` (live authenticated calls).
- [ ] 1.12 **Acceptance:** authenticated **read-only** calls to production return balance and open markets (decision A); config and write-guard tests pass. **Stop.**

## Phase 2 — Discovery and recording
- [ ] 2.1 `data/orderbook.py`: snapshot + deltas, Yes/No asks derived from bids, `best_yes_bid`, `best_yes_ask`, `depth_at_or_better`. Includes the liquidity lab's float-residue (phantom level) fix and its test.
- [ ] 2.2 `api/ws.py`: auth, dynamic tickers, sequence-gap detection → REST resync, reconnect with backoff + resubscribe. Tested against a fake WS server.
- [ ] 2.3 `discovery/scanner.py`: 5s poll (or lifecycle channel); new = unseen ticker + opened within 10 min; store; emit NewMarket.
- [ ] 2.4 Category and excluded-series filters, with cached event/series lookups.
- [ ] 2.5 Watchlist: add on NewMarket, drop after 60 min or when a position opens; WS subscriptions follow.
- [ ] 2.6 Snapshot recording: fixed cadence while watched, plus throttled change snapshots; disk-use estimate.
- [ ] 2.7 Status/result updater for recorded markets (needed for the historical model and calibration).
- [ ] 2.8 Scanner + WS tasks; clean SIGINT/SIGTERM shutdown.
- [ ] 2.9 `monitoring/alerts.py` minimum: alert on crash and errors.
- [ ] 2.10 Deploy somewhere that stays on 24h (decision B).
- [ ] 2.11 **Acceptance:** 24h without crashing; report on markets found, books recorded, resyncs, errors. **Stop.**
- [ ] 2.12 *(Optional, recommended)* Opportunity census: how often did a new market sit ≥20¢ from a simple sibling-implied price? Shows whether the edge exists before Phases 3–4.

## Phase 3 — Fair value
- [ ] 3.1 `FairValue` type and `estimate(market, context)` interface.
- [ ] 3.2 Sibling model, mutually exclusive: 100 − sum of other mids, clip 1–99; confirm the mutually-exclusive API field.
- [ ] 3.3 Sibling model, strike ladders: strikes from API fields, interpolation, monotonicity.
- [ ] 3.4 Sibling confidence from sibling spread and depth; missing-sibling handling.
- [ ] 3.5 Tests: sums to 100, monotonicity, clipping, missing siblings.
- [ ] 3.6 Historical model: backfill settled markets for the series; base rate by "similar parameters" (decision D); ≥20 settled; confidence from sample size.
- [ ] 3.7 Historical model tests.
- [ ] 3.8 `engine.py`: highest confidence, or confidence-weighted average when ≥2 agree within 5¢; min-confidence cutoff; log every estimate.
- [ ] 3.9 Combining tests, including three models where only some agree.
- [ ] 3.10 *(If approved, decision E)* External model: one weather adapter.
- [ ] 3.11 **Acceptance:** tests pass; report of fair estimates beside live prices. **Stop.**

## Phase 4 — Signals and paper trading
- [ ] 4.1 `fee(price, contracts)` from the confirmed formula and rounding, with tests.
- [ ] 4.2 `signals.evaluate`: Yes/No entry, depth check, one signal per market; tests at, just above and just below the threshold.
- [ ] 4.3 Sizing rule (interpretation: fees count toward the per-trade cap).
- [ ] 4.4 `risk/manager.py`: every limit, every rejection logged; one test per limit.
- [ ] 4.5 Kill switch: `KILL` file or `kill` CLI blocks every order and cancels resting ones; tested.
- [ ] 4.6 Paper executor: simulated fills against the live book at price and depth; remainder cancelled at timeout; fees recorded.
- [ ] 4.7 `positions/manager.py`: open/close, persist every change, rebuild from DB on restart.
- [ ] 4.8 The five exit rules in order; tests that each fires in the right order.
- [ ] 4.9 Exit execution: limit at bid, re-quote −1¢ per timeout to 5¢ slippage, then alert (decision C).
- [ ] 4.10 Decision loop per spec; snapshot + inputs logged on every evaluation.
- [ ] 4.11 Daily P&L rollup.
- [ ] 4.12 **Acceptance:** paper trades open and close by the rules; every decision logged. **Stop.**

## Phase 5 — Reporting and backtest
- [ ] 5.1 `report`: P&L, win rate, fill rate, average edge at entry vs captured.
- [ ] 5.2 Calibration table: fair value in 10¢ buckets vs actual Yes settlement rate.
- [ ] 5.3 `backtest/replay.py`: same signal/risk/position code over recorded snapshots on a simulated clock.
- [ ] 5.4 **Acceptance:** replay reproduces paper results exactly (requires one injected clock throughout, from Phase 1 on). **Stop.**

## Phase 6 — Demo trading
- [ ] 6.1 Real executor: limit orders with client order IDs, status tracking, stale cancels.
- [ ] 6.2 Fill channel over WS + API reconciliation on startup and every minute.
- [ ] 6.3 Restart tests: positions and resting orders rebuild from DB + API.
- [ ] 6.4 **Acceptance:** end-to-end on demo — place, partial fill, cancel, kill switch, restart mid-order. **Stop.**

## Phase 7 — Live
Not touched by Claude. Requires ≥2 weeks of paper results, owner review and
explicit approval; the owner makes the mode switch.

---

## Open decisions (needed before the phase noted)
- **A. Data source — DECIDED 2026-10-22.** Paper mode and recording read *production* market data, read-only. The owner's experience on the liquidity lab: demo "does very little" (lab notes 2026-08-31: mock funds, not reflective of real markets). Phase 1 acceptance uses production reads. Phase 6 (order mechanics) is decided when we reach it: demo for plumbing only, or tiny live orders with explicit approval.
- **B. 24h host (before 2.10).** Laptop sleeps; the lab droplet is frozen until 11-05. Recommended: a second $6/month droplet.
- **C. Thin-book exits (before 4.9).** Spec doesn't say what happens after the 5¢ slippage cap + alert; recommended: stop re-quoting, hold, alert. Also note the 15¢ stop is smaller than the 20¢ entry edge. Rules unchanged without approval; every exit logged so Phase 5 can judge them.
- **D. "Similar parameters" for the historical model (before 3.6).** Proposal for weather: same series, same strike, similar time of year — refined on real data.
- **E. External weather adapter (before 3.10).** NWS API is free. Add at the end of Phase 3, or later?
- **F. Alerts channel (before 2.9).** Telegram (needs bot token + chat ID) or reuse the lab's existing alert setup.
- **G. Credentials and repo (before 1.12).** Prod key received 2026-10-22 (Ed25519, stored at `~/.kalshi/kalshi_bot_prod.pem`; owner advised to rotate it, since it was pasted into chat). No demo key needed for Phase 1 (decision A). Still open: private GitHub repo under the same account?

## Interpretations used unless overridden
- Fees: total fee for the order size ÷ count = per-contract figure in the threshold check (Kalshi likely rounds per order).
- Combining 3+ models: average the agreeing group containing the highest-confidence model.
- `orderbook_snapshots` stores Yes bids and Yes asks, asks derived from No bids.
- Sizing: fees count toward `max_dollars_per_trade`.
