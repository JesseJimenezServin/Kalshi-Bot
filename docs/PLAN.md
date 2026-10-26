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
- [x] 1.1 `config.yaml` + pydantic models for every section, with range validation.
- [x] 1.2 Secrets via pydantic-settings. Mode gating: paper default; demo explicit; live needs `mode: live` AND `CONFIRM_LIVE_TRADING=yes`, else refuse to start.
- [x] 1.3 Config validation tests: missing values, out-of-range, live without confirmation.
- [x] 1.4 structlog JSON logging.
- [x] 1.5 `api/auth.py`: key loading, signed headers. Test against the docs' example, or a sign-then-verify round trip.
- [x] 1.6 `api/rest.py` core: httpx async, base URL by mode, token-bucket rate limiting, backoff on 429/5xx.
- [x] 1.7 Pydantic response models: market, event, orderbook, balance, position, order, fill.
- [x] 1.8 Read methods with pagination; tests on a mocked HTTP transport.
- [x] 1.9 `place_order` / `cancel_order` accept only an `Approved` object that only the risk manager can create. Mock-tested; unused until Phase 6.
- [x] 1.9b **Write guard.** Paper mode reads production with a key that *can* trade, so the REST client refuses every non-GET request unless mode is `demo` (demo URL) or `live` with `CONFIRM_LIVE_TRADING=yes`. Enforced in the client itself, below the risk manager, and tested. Same idea as the liquidity lab's GET-only guard.
- [x] 1.10 `storage/models.py` (all 8 tables, Postgres-portable) + `storage/repo.py`; SQLite WAL; tests on a temp DB.
- [x] 1.11 CLI skeleton: `run`, `kill`, `report`, plus `check` (live authenticated calls).
- [x] 1.12 **Acceptance:** authenticated **read-only** calls to production return balance and open markets (decision A); config and write-guard tests pass. **Stop.**

**Phase 1 result (2026-10-23):** 121 tests pass. `kalshi-bot check` against
production, read-only: auth ok (Ed25519), tier basic, balance returned across
4 shards, open markets returned, write guard refused a real DELETE locally.
Found and fixed during testing: the token bucket could spin forever, because
at unix-time magnitude a sub-microsecond wait doesn't move a float clock
(regression test added). Additions beyond the spec: a `storage` config
section; `bot/clock.py` (one injectable clock, needed for exact replay);
`mve_filter=exclude` on market listing (drops combo markets).

## Phase 2 — Discovery and recording
- [x] 2.1 `data/orderbook.py`: snapshot + deltas, Yes/No asks derived from bids, `best_yes_bid`, `best_yes_ask`, `depth_at_or_better`. Includes the liquidity lab's float-residue (phantom level) fix and its test.
- [x] 2.2 `api/ws.py`: auth, dynamic tickers, sequence-gap detection → REST resync, reconnect with backoff + resubscribe. Tested against a fake WS server.
- [x] 2.3 `discovery/scanner.py`: 5s poll (or lifecycle channel); new = unseen ticker + opened within 10 min; store; emit NewMarket.
- [x] 2.4 Category and excluded-series filters, with cached event/series lookups.
- [x] 2.5 Watchlist: add on NewMarket, drop after 60 min or when a position opens; WS subscriptions follow.
- [x] 2.6 Snapshot recording: fixed cadence while watched, plus throttled change snapshots; disk-use estimate.
- [x] 2.7 Status/result updater for recorded markets (needed for the historical model and calibration).
- [x] 2.8 Scanner + WS tasks; clean SIGINT/SIGTERM shutdown.
- [x] 2.9 `monitoring/alerts.py` minimum: alert on crash and errors.
- [x] 2.10 Deploy somewhere that stays on 24h (decision B).
- [~] 2.11 **Acceptance:** 24h without crashing; report on markets found, books recorded, resyncs, errors. **Stop.**
- [~] 2.12 *(Optional, recommended)* Opportunity census: how often did a new market sit ≥20¢ from a simple sibling-implied price? Shows whether the edge exists before Phases 3–4.

**Phase 2 progress (2026-10-23):** 2.1–2.9 built, 171 tests pass. Live checks
against production, read-only:
- Startup sweep of 458 weather series registered 1,293 markets; the
  lifecycle feed connected and parsed real messages; 0 errors, 0 gaps.
- A 60 s book probe on 5 live KXHIGHNY markets: every streamed book matched
  a fresh REST snapshot exactly, including one after 303 deltas.
- Found by the live runs and fixed: WebSocket TLS failed on this Mac (no
  system CA bundle; now pinned to certifi, as REST already was), and the
  startup sweep ran twice.
- Found in review and fixed before running: the WS reader could deadlock
  awaiting its own command replies (handlers now go through a queue), and
  "already emitted" lived only in memory (now `markets.emitted_at`).

**2.10 done 2026-10-23:** deployed commit `2e64aa2` to the lab droplet as systemd
`kalshi-bot` (see deploy/README.md), below the lab in CPU/IO/memory priority,
at 75% of the account rate limit. Startup: 458 series swept in 49 s, 1,293
markets registered, lifecycle feed connected, 65 MB RSS, lab services active.

**2.11 run started 2026-10-23 17:38 UTC (restarted 18:16 UTC, see Phase 3 notes)**, which
covers the 2026-10-24 14:00 UTC open burst. Check with `kalshi-bot stats`.
**2.11a (2026-10-23 17:42 UTC):** healthy, 0 restarts, 0 warnings, lifecycle
flowing; 923 of the registered markets already carry results (useful for the
Phase 3 historical model).

**2.12 tool built (2026-10-23):** `kalshi-bot census` (bot/analysis/census.py),
15 tests with hand-computed values, deployed. It needs recorded new-market
books, so the first real run is after the 2026-10-24 14:00 UTC open. Note:
the sibling model sees an event whose mids sum to less than 100 as *every*
bucket being cheap, so gaps cluster by event. The report counts markets, so
read it with that in mind.

**2.13 backups (2026-10-23), added to the plan:** a daily 04:30 UTC off-box
backup of the bot's DB to the lab's B2 bucket under `kalshi-bot/`, kept
separate from the lab's files, staging and success marker (the lab's
healthcheck reads its own marker). First run uploaded; **restore verified**
(integrity ok, row counts match live, lab's 26 files untouched).

**Remaining:** 2.11b (24 h report, after 2026-10-24 17:38 UTC) and the first
census on real data (after 2026-10-24 ~15:00 UTC).

## Phase 3 — Fair value
- [x] 3.1 `FairValue` type and `estimate(market, context)` interface.
- [x] 3.2 Sibling model, mutually exclusive: 100 − sum of other mids, clip 1–99; confirm the mutually-exclusive API field.
- [x] 3.3 Sibling model, strike ladders: strikes from API fields, interpolation, monotonicity.
- [x] 3.4 Sibling confidence from sibling spread and depth; missing-sibling handling.
- [x] 3.5 Tests: sums to 100, monotonicity, clipping, missing siblings.
- [x] 3.6 Historical model: backfill settled markets for the series; base rate by "similar parameters" (decision D); ≥20 settled; confidence from sample size.
- [x] 3.7 Historical model tests.
- [x] 3.8 `engine.py`: highest confidence, or confidence-weighted average when ≥2 agree within 5¢; min-confidence cutoff; log every estimate.
- [x] 3.9 Combining tests, including three models where only some agree.
- [ ] 3.10 *(SKIPPED pending decision E)* External model: one weather adapter.
- [x] 3.11 **Acceptance:** tests pass; report of fair estimates beside live prices. **Stop.**

**Phase 3 result (2026-10-23):** 231 tests pass. `kalshi-bot fair-report` against
live production data (read-only) shows every model's estimate beside the live
bid/ask.
- Daily-high events (exclusive buckets): the sibling model tracks the market
  at confidence 0.6-0.8. Those events summed to 97.5-103.5c, with 13/18
  markets estimated.
- Hourly ladders: the ladder model does not commit across a 90c cliff (0.11
  confidence), and only 1/40 markets was estimated.
- No executable gap of 5c or more on these established markets.
- History: 292,619 settled markets / 38,085 events / 209 series backfilled
  (2021-07 to 2026-10). 34,114 complete events are indexed.
- **Decision D, as implemented (owner to confirm):** "similar" means the same
  series, the same number of markets, the same position on the strike axis,
  preferring +/-45 days of the same date. Historical confidence is capped at
  0.5 < min_confidence. The live report shows why: e.g. Austin's B87.5 at 67c
  vs a 22.9c base rate. Uncapped, that would have looked like a 44c
  "mispricing".

Bugs found on the live run and fixed during Phase 3 (all affect Phase 2
recording):
- False WS sequence gaps: Kalshi's `ok` replies carry a seq. There were 41
  false gaps in ~30 min; 0 in 11,898 messages since the fix.
- Interrupted watches were not resumed on restart. They are now, with the
  original expiry (40 resumed on the redeploy).
- A redundant re-stale in resync (exposed by a flaky test).
- History overlap between the live and historical tiers broke the first
  backfill.

**The 24 h clock restarted 2026-10-23 18:16 UTC** (a restart was needed for
the gap fix). It now ends 2026-10-24 18:16 UTC and still covers the 14:00 UTC
burst.

**Category question (2026-10-23).** Do markets open far from fair? Public
trades for ~140 markets across 13 categories (first 10 min vs the 45-75 min
consensus): the largest case anywhere was 40 contracts traded 24c off,
about $8. Weather's first trade typically lands within ~1c of the +1h
consensus. Trades understate opportunity, since a mispriced quote nobody
hit shows no trade, so the owner chose to widen book recording to measure
quotes directly.

**Recording widened 2026-10-23 22:41 UTC** (commit at that deploy) to Climate
and Weather, Sports (25% of events, sampled deterministically), Politics,
Economics, Financials and Mentions: 9,204 series. Safeguards: max 600 markets
watched at once, a disk guard at 4 GB free, and a global min_created_ts
sweep instead of per-series queries. At startup: 99,551 markets created in
24 h scanned, 1,280 queued to open; after 6 min, 87 watched, 0 gaps, 0
errors, 119 MB RSS, ~7.7 snapshot rows/s. Projected ~1 GB/day at peak;
~7 days of disk before the guard. **Run 3-7 days, then census per
category.** The census scores mutually exclusive events only; single-market
events need an opening-move measure (to add).

Also fixed 2026-10-23: new events 404 over REST right after creation, which
dropped 31/40 markets until the sweep (8.7 min late). Fixed by caching
event_lifecycle plus throttled retries. **The 24 h clock restarted again,
22:41 UTC; it ends 2026-10-24 22:41 UTC.**

**Next-steps 1-3 done (2026-10-23):**
1. `bot/analysis/opening.py`: an opening-move measure for every recorded
   market, against its own 45-75 min consensus. It covers single-market
   events the census cannot.
2. `kalshi-bot category-study`: the trade-based cross-category study, made
   reproducible. Unsigned public requests, so no account rate budget.
3. The census reports per category, and `kalshi-bot census` prints both.
252 tests pass, here and on the droplet (5/5 runs, after a test race the
slower droplet exposed was fixed).

**Lab and droplet items done (2026-10-23):**
- Lab switch-in crash fixed (lab commit c12f620). Its window restarted
  22:55 UTC and ends 2026-11-06.
- needrestart never auto-restarts `kalshi-*` services
  (/etc/needrestart/conf.d/90-kalshi.conf).
- SSH is key-only (/etc/ssh/sshd_config.d/00-kalshi-hardening.conf).
  Verified: a new key login works, and a password attempt is refused.
- Not done: a kernel reboot is pending (no auto-reboot). Do it after the
  lab window ends.

**Droplet and data, 2026-10-26.**
- 72 h of widened recording: 24,104 markets declared new, ~5M book snapshots, 231k trades, 0 WS gaps.
- Problems found: the lab's daily backups made ~6 GB temporary copies, which paused recording twice (13 + 4 min) and
  stalled the 09:04 scan every day; the 600-market watch cap overflowed at the 12:00/14:00 UTC bursts (3,122 new
  markets skipped); the 72 h census was OOM-killed.
- Fixed: lab put dormant at the owner's request (units disabled, cron lines commented; DB archived with a verified
  sha256 to B2 `final/` and the owner's Mac, then deleted from the droplet: 17 GB free). Watch cap raised to 1200
  (735 watched right after the deploy). Census folds events in one at a time (~175 MB RSS; ran under a 350 MB
  systemd cap). `deploy/healthcheck.sh` alerts for the bot through the lab's ntfy topic, cron every 5 min.
- Step 4 (opening burst): median first book 4-12 s after open per category; 0 stale books. Capacity skips fixed as above.

**72 h census (2026-10-26, provisional fees, >= 50 contracts at the price):**
- Opening move vs each market's own 45-75 min consensus: **0 markets with a >= 20c gap**, in any category
  (462 measurable; most recorded markets resolve within 6 h or had not reached 75 min).
- Sibling census (mutually exclusive events only): 54 markets with a >= 20c gap at depth, 0.5% of
  observed market-time. Politics 26/74 markets (but its events sum to 100 +/- 38c at open, median), Sports 26/1,413,
  Weather 2/476. Sibling "fair" is 100 minus the other markets' MIDS, so wide or one-sided sibling books inflate it;
  these are candidates to inspect, not confirmed opportunities.

## Phase 4 — Signals and paper trading

**Proposed breakdown (2026-10-26, not started; needs the owner's go-ahead).** The original items 4.1-4.12 are
kept below; this splits them into small steps and adds a gate first.

*4.0 Gate (read-only, before any trading code)*
- 4.0a Inspect the top ~20 sibling-census candidates by hand (books for every sibling at the moment of the
  gap): were they executable, or artifacts of wide/one-sided sibling books? Reported per category.
- 4.0b Re-run the census with the engine's confidence filter (what Phase 4 would actually act on), so the
  count matches what the signal step would see.
- 4.0c Owner decisions: fee schedule verified (4.1 needs it), C (thin-book exits), D confirmed, and whether
  to also LOG (never trade) shadow signals at 5c/10c so a quiet two weeks still says something. Entry stays 20c.

*4.1 Fees*: 4.1a `fee(price, count, series)` from the confirmed schedule (per-series fee_type/multiplier, rounding
per order); 4.1b tests against the schedule's worked examples; 4.1c analysis code switches to it.

*4.2 Signals*: 4.2a pure `evaluate(book, fair, cfg) -> Signal | None` (YES and NO sides); 4.2b depth walk at
the executable price; 4.2c one signal per market; 4.2d tests at, just above, just below 20c; 4.2e shadow log (if approved).

*4.3 Sizing*: contracts = floor(max_dollars_per_trade / (price + fee per contract)), capped by depth; tests.

*4.4 Risk manager* (`bot/risk/manager.py`, the only issuer of `Approved`): one check per limit (per-trade $,
total exposure, per event, open positions, daily loss, kill switch, mode); every rejection persisted; one test per limit.

*4.5 Kill switch*: the `KILL` file blocks approval and cancels resting paper orders next cycle; tested.

*4.6 Paper executor*: fill against the live recorded book at the limit price, walking depth; partial fills;
remainder cancelled at `order_timeout_sec`; fees recorded in orders/fills.

*4.7 Positions*: open/close/persist every change; rebuild from the DB on restart; tests.

*4.8 Exits*: the five spec rules in spec order, one test each plus an ordering test. *4.9* exit execution
per decision C.

*4.10 Decision loop*: on each book update of a watched market, fair value -> signal -> risk -> executor,
throttled per market for the 1 vCPU; book + inputs logged for every evaluation (this is what Phase 5 replays).

*4.11* Daily P&L rollup. *4.12 Acceptance*: an end-to-end test with a scripted book that enters and hits each
exit; deploy in paper mode; this starts the >= 2-week paper record Phase 7 requires. **Stop.**

Original items:
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
- **B. 24h host — DECIDED 2026-10-23:** the lab droplet, alongside the lab, at lower priority (owner's choice over a second droplet).
- **C. Thin-book exits (before 4.9).** Spec doesn't say what happens after the 5¢ slippage cap + alert; recommended: stop re-quoting, hold, alert. Also note the 15¢ stop is smaller than the 20¢ entry edge. Rules unchanged without approval; every exit logged so Phase 5 can judge them.
- **D. "Similar parameters" — IMPLEMENTED AS PROPOSAL 2026-10-23, owner to confirm:** same series, same event size, same strike-axis position, +/-45-day seasonal preference; confidence capped at 0.5 (below min_confidence) so it corroborates but never triggers.
- **E. External weather adapter (before 3.10).** NWS API is free. Add at the end of Phase 3, or later?
- **F. Alerts channel — default 2026-10-23:** `none` (alerts go to the structured log) until the owner picks Telegram or another channel.
- **G. Credentials and repo (before 1.12).** Prod key received 2026-10-22 (Ed25519, stored at `~/.kalshi/kalshi_bot_prod.pem`; owner advised to rotate it, since it was pasted into chat; 2026-10-23 owner decided NOT to rotate). No demo key needed for Phase 1 (decision A). Still open: private GitHub repo under the same account?

## Interpretations used unless overridden
- Fees: total fee for the order size ÷ count = per-contract figure in the threshold check (Kalshi likely rounds per order).
- Combining 3+ models: average the agreeing group containing the highest-confidence model.
- `orderbook_snapshots` stores Yes bids and Yes asks, asks derived from No bids.
- Sizing: fees count toward `max_dollars_per_trade`.
