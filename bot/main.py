"""
Entry point and CLI.

  kalshi-bot check     validate config, then make authenticated read-only calls
  kalshi-bot run       start the bot (Phase 2: discovery + recording, no trading)
  kalshi-bot stats     what has been recorded (Phase 2 run report)
  kalshi-bot census    how often new markets were mispriced vs siblings (plan 2.12)
  kalshi-bot backfill-history   load settled markets for the historical model (plan 3.6)
  kalshi-bot fair-report        fair estimates beside live prices (plan 3.11)
  kalshi-bot total-move   open-to-pre-event move of settled markets (public candles; nothing in-play)
  kalshi-bot movers       biggest tradeable opening moves, ranked by dollars (no threshold)
  kalshi-bot candidates   inspect the top sibling-census gaps: arbitrage? did the price converge?
  kalshi-bot category-study     which categories traded far from fair at open (public data)
  kalshi-bot kill      create the KILL file: stop new orders, cancel resting ones
  kalshi-bot report    performance report (Phase 5)

Every command validates config first and exits non-zero if it is invalid.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from bot.api.auth import Signer
from bot.api.rest import KalshiRestClient, WriteForbidden
from bot.app import Bot, lock_path_for, resolve_db_url
from bot.config import AppConfig, ConfigError, load_config
from bot.monitoring.log import configure_logging, get_logger
from bot.singleton_lock import AlreadyRunning, SingletonLock
from bot.storage.repo import Repository

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KILL_FILE = PROJECT_ROOT / "KILL"

log = get_logger("main")


def build_rest_client(cfg: AppConfig) -> KalshiRestClient:
    signer = Signer.from_file(cfg.secrets.kalshi_api_key_id, cfg.secrets.kalshi_private_key_path)
    return KalshiRestClient(cfg.rest_base_url, signer, writes_allowed=cfg.writes_allowed)


async def check(cfg: AppConfig, show_balance: bool) -> int:
    """Phase 1 acceptance: authenticated reads work, and writes are refused in paper mode."""
    async with build_rest_client(cfg) as rest:
        limits = await rest.configure_rate_limits()
        print(f"auth ok        env={cfg.environment.value} mode={cfg.mode.value} tier={limits.usage_tier} "
              f"read={limits.read.refill_rate:g}/s write={limits.write.refill_rate:g}/s")

        bal = await rest.get_balance()
        shards = ", ".join(f"shard {b.exchange_index}" for b in bal.balance_breakdown) or "no breakdown"
        amount = f"${bal.balance_dollars}" if show_balance else "(hidden; --show-balance to print)"
        print(f"balance ok     {amount} [{shards}]")

        page = await rest.get_markets(status="open", limit=5, mve_filter="exclude")
        print(f"markets ok     open markets returned: {len(page.markets)} (first page, limit 5), "
              f"more={'yes' if page.cursor else 'no'}")
        for mk in page.markets:
            print(f"               {mk.ticker}  status={mk.status}  close={mk.close_time}")

        if not cfg.writes_allowed:
            # Probe the guard on the real client: a DELETE must be refused
            # locally, before signing or any network call. Goes through the
            # client's single request path, the thing being checked.
            try:
                await rest._request("DELETE", "/portfolio/events/orders/guard-check")  # noqa: SLF001
            except WriteForbidden:
                print("write guard ok non-GET requests refused locally in this mode")
            else:  # pragma: no cover - would mean the guard is broken
                print("WRITE GUARD FAILED: a write was sent", file=sys.stderr)
                return 2
    return 0


async def run_bot(cfg: AppConfig) -> int:
    db_url = resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT)
    try:
        with SingletonLock(lock_path_for(db_url, PROJECT_ROOT)):
            bot = Bot(cfg, repo=Repository.from_url(db_url))
            await bot.run_forever()
    except AlreadyRunning as e:
        print(f"refusing to start: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_stats(cfg: AppConfig, hours: float) -> int:
    from datetime import timedelta

    from sqlalchemy import func, select

    from bot.clock import SystemClock
    from bot.storage.models import BotEventRow, MarketRow, OrderbookSnapshotRow, TradeRow

    db_url = resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT)
    repo = Repository.from_url(db_url)
    since = SystemClock().utcnow() - timedelta(hours=hours)
    with repo.session() as s:
        markets = s.scalar(select(func.count()).select_from(MarketRow).where(MarketRow.first_seen_at >= since))
        new = s.scalar(select(func.count()).select_from(MarketRow).where(MarketRow.emitted_at >= since))
        resolved = s.scalar(select(func.count()).select_from(MarketRow).where(MarketRow.result.is_not(None)))
        snaps = s.scalar(select(func.count()).select_from(OrderbookSnapshotRow).where(OrderbookSnapshotRow.ts >= since))
        snap_markets = s.scalar(select(func.count(func.distinct(OrderbookSnapshotRow.ticker)))
                                .where(OrderbookSnapshotRow.ts >= since))
        trades = s.scalar(select(func.count()).select_from(TradeRow).where(TradeRow.ts >= since))
        kinds = dict(s.execute(select(BotEventRow.kind, func.count()).where(BotEventRow.ts >= since)
                               .group_by(BotEventRow.kind)).all())
        last_stop = s.scalars(select(BotEventRow).where(BotEventRow.kind == "stop")
                              .order_by(BotEventRow.ts.desc()).limit(1)).first()
    print(f"last {hours:g}h  (db: {db_url})")
    print(f"  markets discovered      {markets}")
    print(f"  declared new (watched)  {new}")
    print(f"  with results (all time) {resolved}")
    print(f"  book snapshots          {snaps}  across {snap_markets} markets")
    print(f"  trades recorded         {trades}")
    print("  events                  " + (", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "none"))
    if last_stop is not None:
        ws = last_stop.detail.get("ws", {})
        print(f"  last stop {last_stop.ts:%Y-%m-%d %H:%M}Z: ws gaps={ws.get('gaps')} resyncs={ws.get('resyncs')} "
              f"reconnects={max(0, (ws.get('connects') or 1) - 1)} task_failures={last_stop.detail.get('task_failures')}")
    path = db_url.removeprefix("sqlite:///")
    if Path(path).exists():
        size = sum(p.stat().st_size for p in Path(path).parent.glob(Path(path).name + "*"))
        print(f"  database size           {size / 1e6:.1f} MB")
    return 0


async def cmd_census(cfg: AppConfig, hours: float, window_min: float) -> int:
    from datetime import timedelta
    from decimal import Decimal

    from bot.analysis.census import ALL, format_report, run_census
    from bot.analysis.opening import format_opening, run_opening
    from bot.clock import SystemClock

    repo = Repository.from_url(resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT))
    since = SystemClock().utcnow() - timedelta(hours=hours)
    min_depth = Decimal(str(cfg.file.strategy.min_depth_contracts))
    async with build_rest_client(cfg) as rest:
        reports = await run_census(repo, rest, since, timedelta(minutes=window_min), min_depth)
    print(f"=== SIBLING CENSUS: mutually exclusive events with a market declared new in the last {hours:g}h, "
          f"first {window_min:g} min after open")
    for cat, rep in reports.items():
        if cat != ALL and rep.events == 0 and not rep.events_skipped_not_exclusive \
                and not rep.events_skipped_single_market:
            continue
        print(f"\n--- {cat}")
        print(format_report(rep, min_depth))
    print(f"\n=== OPENING MOVE: every market declared new in the last {hours:g}h, vs its own later price")
    print(format_opening(run_opening(repo, since, min_depth), min_depth))
    return 0


async def cmd_backfill_history(cfg: AppConfig, only: list[str] | None) -> int:
    from bot.clock import SystemClock
    from bot.discovery.catalog import SeriesCatalog
    from bot.fair_value.history import BACKFILL_RATE_SHARE, backfill, load_index

    clock = SystemClock()
    repo = Repository.from_url(resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT))
    async with build_rest_client(cfg) as rest:
        # Shares the account's rate limit with the running bot and the lab.
        await rest.configure_rate_limits(share=BACKFILL_RATE_SHARE)
        d = cfg.file.discovery
        catalog = SeriesCatalog(rest, d.include_categories, d.exclude_series, clock)
        await catalog.refresh()
        series = sorted(only) if only else sorted(catalog.included)
        print(f"backfilling {len(series)} series at {BACKFILL_RATE_SHARE:.0%} of the rate limit...")
        added = await backfill(rest, repo, series, clock)
    idx = load_index(repo)
    print(f"new settled markets: {sum(added.values())} across {sum(1 for v in added.values() if v)} series; "
          f"complete events now indexed: {idx.events}")
    return 0


async def cmd_fair_report(cfg: AppConfig, events: int, series: str | None) -> int:
    from bot.analysis.fair_report import fair_report
    from bot.clock import SystemClock
    from bot.fair_value.engine import build_engine
    from bot.fair_value.history import load_index

    repo = Repository.from_url(resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT))
    history = load_index(repo) if "historical" in cfg.file.fair_value.models else None
    engine = build_engine(cfg.file.fair_value, history)
    async with build_rest_client(cfg) as rest:
        print(await fair_report(repo, rest, engine, SystemClock().utcnow(), events, series))
    print(f"(history: {history.events if history else 0} settled events indexed; "
          f"min_confidence {cfg.file.fair_value.min_confidence})")
    return 0


async def cmd_category_study(cfg: AppConfig, categories: list[str] | None, series: int, markets: int) -> int:
    from bot.analysis.category_study import DEFAULT_CATEGORIES, format_study, study
    from bot.clock import SystemClock

    # Unsigned: public data does not draw on the account's rate budget, so the
    # study never competes with the running bot or the lab. Paced at ~8 req/s.
    rest = KalshiRestClient(cfg.rest_base_url, None, writes_allowed=False)
    rest.read_bucket.reconfigure(80.0, 80.0)
    async with rest:
        results = await study(rest, categories or list(DEFAULT_CATEGORIES), SystemClock().utcnow(),
                              series_per_category=series, markets_per_category=markets)
    print(format_study(results))
    return 0


async def cmd_total_move(cfg: AppConfig, categories: list[str], days: int, series: int, markets: int) -> int:
    import sys

    from bot.analysis.total_move import format_study, study
    from bot.clock import SystemClock

    rest = KalshiRestClient(cfg.rest_base_url, None, writes_allowed=False)  # unsigned: no account budget
    rest.read_bucket.reconfigure(50.0, 50.0)  # ~5 req/s: 8/s drew 429s from the public limit (2026-10-27)
    async with rest:
        res = await study(rest, categories, SystemClock().utcnow(), days=days, series_per_category=series,
                          markets_per_category=markets, progress=lambda msg: print(msg, file=sys.stderr, flush=True))
    print(f"=== TOTAL MOVE, OPEN -> PRE-EVENT: {len(res.moves)} settled markets (last {days} d), "
          f"categories {', '.join(categories)}")
    print(format_study(res))
    return 0


def cmd_movers(cfg: AppConfig, hours: float, top: int) -> int:
    from datetime import timedelta

    from bot.analysis.movers import format_movers, run_movers
    from bot.clock import SystemClock

    repo = Repository.from_url(resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT))
    movers = run_movers(repo, SystemClock().utcnow() - timedelta(hours=hours))
    print(f"=== BIGGEST OPENING MOVERS: {len(movers)} markets declared new in the last {hours:g}h with a book "
          "in their first 10 min")
    for ref in ("+1h", "+6h", "settle"):
        print(f"\n--- vs {ref}")
        print(format_movers(movers, ref=ref, top=top))
    return 0


async def cmd_candidates(cfg: AppConfig, hours: float, window_min: float, top: int) -> int:
    from datetime import timedelta
    from decimal import Decimal

    from bot.analysis.candidates import find_candidates, format_candidates
    from bot.clock import SystemClock

    repo = Repository.from_url(resolve_db_url(cfg.file.storage.database_url, PROJECT_ROOT))
    since = SystemClock().utcnow() - timedelta(hours=hours)
    min_depth = Decimal(str(cfg.file.strategy.min_depth_contracts))
    # Unsigned like category-study: never draws on the running bot's rate budget.
    rest = KalshiRestClient(cfg.rest_base_url, None, writes_allowed=False)
    rest.read_bucket.reconfigure(80.0, 80.0)
    async with rest:
        cands = await find_candidates(repo, rest, since, timedelta(minutes=window_min), min_depth,
                                      threshold=Decimal(cfg.file.strategy.entry_threshold_cents), top=top)
    print(f"=== TOP {len(cands)} SIBLING-CENSUS GAPS (>= {cfg.file.strategy.entry_threshold_cents}c, "
          f">= {min_depth} contracts), markets declared new in the last {hours:g}h")
    print(format_candidates(cands))
    return 0


def cmd_kill() -> int:
    KILL_FILE.write_text("kill switch engaged via CLI\n")
    print(f"KILL file created at {KILL_FILE}. New orders stop; resting orders are cancelled on the next cycle.")
    print("Remove the file to re-enable trading.")
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshi-bot")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="validate config and make authenticated read-only calls")
    p_check.add_argument("--show-balance", action="store_true")
    sub.add_parser("run", help="start the bot")
    p_stats = sub.add_parser("stats", help="what has been recorded")
    p_stats.add_argument("--hours", type=float, default=24.0)
    p_census = sub.add_parser("census", help="mispricing census over recorded new markets")
    p_census.add_argument("--hours", type=float, default=48.0)
    p_census.add_argument("--window-min", type=float, default=60.0)
    p_bf = sub.add_parser("backfill-history", help="load settled markets for the historical model")
    p_bf.add_argument("--series", nargs="*", help="only these series (default: all included)")
    p_fr = sub.add_parser("fair-report", help="fair estimates beside live prices")
    p_fr.add_argument("--events", type=int, default=5)
    p_fr.add_argument("--series", help="only series starting with this, e.g. KXHIGH")
    p_tm = sub.add_parser("total-move", help="open-to-pre-event move of settled markets")
    p_tm.add_argument("--categories", nargs="+", default=["Sports", "Politics", "Economics"])
    p_tm.add_argument("--days", type=int, default=60)
    p_tm.add_argument("--series-per-category", type=int, default=400)
    p_tm.add_argument("--markets-per-category", type=int, default=700)
    p_mv = sub.add_parser("movers", help="biggest tradeable opening moves, ranked by dollars")
    p_mv.add_argument("--hours", type=float, default=80.0)
    p_mv.add_argument("--top", type=int, default=25)
    p_cand = sub.add_parser("candidates", help="inspect the top sibling-census gaps")
    p_cand.add_argument("--hours", type=float, default=72.0)
    p_cand.add_argument("--window-min", type=float, default=60.0)
    p_cand.add_argument("--top", type=int, default=20)
    p_cs = sub.add_parser("category-study", help="which categories traded far from fair at open")
    p_cs.add_argument("--categories", nargs="*")
    p_cs.add_argument("--series-per-category", type=int, default=60)
    p_cs.add_argument("--markets-per-category", type=int, default=60)
    sub.add_parser("kill", help="engage the kill switch")
    sub.add_parser("report", help="print the performance report")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    if args.command == "kill":  # must work even if the config is broken
        return cmd_kill()

    try:
        cfg = load_config(args.config, args.env_file)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    if args.command == "check":
        return asyncio.run(check(cfg, args.show_balance))
    if args.command == "run":
        return asyncio.run(run_bot(cfg))
    if args.command == "stats":
        return cmd_stats(cfg, args.hours)
    if args.command == "census":
        return asyncio.run(cmd_census(cfg, args.hours, args.window_min))
    if args.command == "backfill-history":
        return asyncio.run(cmd_backfill_history(cfg, args.series))
    if args.command == "fair-report":
        return asyncio.run(cmd_fair_report(cfg, args.events, args.series))
    if args.command == "total-move":
        return asyncio.run(cmd_total_move(cfg, args.categories, args.days, args.series_per_category,
                                          args.markets_per_category))
    if args.command == "movers":
        return cmd_movers(cfg, args.hours, args.top)
    if args.command == "candidates":
        return asyncio.run(cmd_candidates(cfg, args.hours, args.window_min, args.top))
    if args.command == "category-study":
        return asyncio.run(cmd_category_study(cfg, args.categories, args.series_per_category,
                                              args.markets_per_category))
    if args.command == "report":
        print("report: not available until Phase 5.", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(cli())
