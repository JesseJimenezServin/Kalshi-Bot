# Kalshi-Bot

A research bot that asked one question about [Kalshi](https://kalshi.com)
prediction markets: **do new markets open mispriced, by enough to trade?**

The plan was to spot a market at the moment it opens, estimate its fair
value from related markets and history, and buy when the price sat at least
20¢ away. Before writing any trading code, I measured whether that edge
existed. It mostly didn't, so the project stopped at the research stage.
**It never placed an order.** The REST client refuses to send anything but
reads unless live trading is explicitly confirmed.

**Status: concluded (2026-10-28).** Phases 0–3 are complete: API research,
foundation, discovery and recording, and fair-value models. Phase 4
(trading) was deliberately not built. The full build log, with every result,
is in [`docs/PLAN.md`](docs/PLAN.md).

> **Sister project:** [kalshi-market-maker](https://github.com/JesseJimenezServin/kalshi-market-maker),
> an earlier market-making experiment (called "the liquidity lab" in this
> code). Its failures shaped how this one was built.

---

## What the data said

**Recording live markets (72 hours, 24,104 new markets, about 5M book
snapshots, 0 WebSocket gaps).**
- *Opening move against the market's own price 45–75 minutes later:*
  **0 markets** had a gap of 20¢ or more at tradeable depth, in any
  category.
- *Sibling-implied fair value* (for mutually exclusive outcomes, the prices
  should sum to 100): 54 markets flagged. I inspected the top 30 by hand.
  **None converged**: no flagged market's price moved halfway to the
  "fair" price within 15 minutes. 23 were artifacts of wide or one-sided
  books, and 5 came from recording only part of an event. **One was real**:
  a 9-bucket ladder whose asks summed to 88¢, worth about **$4.75** after
  fees.

**Opening price against the last price before the event (555 settled
markets, public price history).**
- *Sports games:* the opening price is where the game ends up before
  kickoff. Median move 2¢; a 10¢+ hindsight edge in about 1% of markets.
- *Politics and economics:* large moves (median 10–43¢). But these are
  "will X happen by a date" markets, so the movement is mostly new
  information arriving, not a wrong opening price.

**Kalshi against Polymarket (13 leagues plus 2027 futures, every pre-game hour).**
- Gaps of 10¢ or more after fees occurred in **0–0.2%** of hours in every
  league except college football (4.4%, where Polymarket's small-school
  games trade thinly). None reached 20¢ in the NFL.
- When they did diverge, **Polymarket usually moved to Kalshi's price**:
  NFL 19 to 3, MLB 31 to 0. Kalshi leads in US sports.
- The exception was **La Liga's opening weeks**, where Kalshi moved to
  Polymarket 7 times. At most about **$560** over 60 days, and only from
  prints at that price on either side, with no competition for them.

**Conclusion:** across about 25,000 newly opened markets there was no evidence of the
single-market ≥20¢ opening mispricing that the strategy needed. The real
opportunities I found were rare and worth single or low-triple-digit
dollars. Building trading code on top of that would have been building on
nothing.

---

## What I learned

**1. Check whether the edge exists before building the machine.** The
earlier project spent weeks building a trading system before measuring its
core assumption. Here, a read-only gate came first: a census, then
hand-inspection, then cross-venue checks. The gate said no, so I avoided
building, testing and operating a trading engine.

**2. Most "opportunities" are measurement artifacts.** The first census
flagged dozens of large gaps. Hand-inspection showed placeholder asks at
5¢, events with only 2 of their markets recorded, and sets that weren't
truly exclusive. The census fixes that followed (require every market of
the event, check exhaustiveness) came from looking at real cases, not from
theory.

**3. Hindsight makes everything look like an edge.** Ranked against the
settlement price, every market is a "big mover": a prop bought at 14¢ that
paid 100 looks brilliant afterwards. Only prices that were knowable at the
time count as evidence.

**4. A second venue isn't automatically a fair value.** Polymarket looked
like a natural reference price. But in US sports it was the venue that was
wrong, and its own opening prices weren't usable at all (one game opened at
54/46 and moved to 81.5 within 3 hours). The fair value had to be earned
per league, by checking which venue moved to the other.

**5. Make the dangerous action impossible, not merely unlikely.** Paper mode
reads production with a key that *can* trade. So the REST client itself
refuses every non-GET request unless the mode is demo, or live with
`CONFIRM_LIVE_TRADING=yes`. Order methods accept only an `Approved` object
that only the risk manager can create. Both rules sit below the strategy
code and are tested.

**6. Bring the last project's bugs forward as tests.** Order-book sizes use
`Decimal`, because floats once created phantom price levels. SQLite runs in
WAL mode with a busy timeout, because the earlier project lost data to
locks. Raw trades are recorded alongside derived values. A singleton lock
prevents duplicate processes. Each of these traces to a specific incident
in the sister project.

---

## How it works

```
lifecycle WebSocket + REST sweep ──► discovery/scanner ──► watchlist (first 60 min of each market)
                                                               │
                         api/ws.py (books, trades, gap → resync) ──► data/recorder ──► SQLite (WAL)
                                                               │
               fair_value/  sibling (exclusive sets, strike ladders) + historical (292k settled markets)
                                                               │
               analysis/  census · candidates · opening · movers · total-move · crossvenue · fair-report
```

- **API layer**: async `httpx` and `websockets` with Ed25519 or RSA request
  signing. A token-bucket rate limiter holds the bot to 75% of the account
  limit, since another program shared the account. Typed pydantic models.
- **Discovery**: Kalshi's market-lifecycle channel, with a periodic REST
  sweep as a fallback. Category filters and deterministic per-event
  sampling.
- **Fair value**: a sibling model for exclusive outcomes and strike ladders,
  and a historical base-rate model over 292,619 settled markets
  (2021–2026). An engine combines the models' estimates by confidence.
- **One injectable clock** throughout, so a recorded session can be
  replayed exactly.
- **Operations**: a systemd service under CPU, IO and memory limits, a disk
  guard, a healthcheck with push alerts, and a daily off-box backup with a
  verified restore. See [`deploy/`](deploy/).
- **Tests**: 282 (pytest, fake REST and WebSocket servers, no network).
  About 6,600 lines of Python.

Results from the studies are in [`docs/results/`](docs/results/). API
findings, each with a link to the official docs, are in
[`docs/API_NOTES.md`](docs/API_NOTES.md).

## Running it

```bash
python3 -m venv venv && source venv/bin/activate   # Python 3.11+
pip install -e ".[dev]"
pytest                                  # 282 tests, offline
cp .env.example .env                    # Kalshi key ID + key path
kalshi-bot check                        # authenticated read-only calls
kalshi-bot run                          # discover + record new markets
kalshi-bot census                       # mispricing census over recordings
kalshi-bot crossvenue --league nfl      # Kalshi vs Polymarket, public data
```

`mode: paper` in `config.yaml` is the default, and nothing in this
repository can place an order in that mode.

---

*Built with [Claude Code](https://claude.com/claude-code) as a pair
programmer. I set the direction, made the decisions and reviewed the
results.*
