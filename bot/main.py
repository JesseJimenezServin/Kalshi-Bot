"""
Entry point and CLI.

  kalshi-bot check     validate config, then make authenticated read-only calls
  kalshi-bot run       start the bot (Phase 2: discovery + recording, no trading)
  kalshi-bot stats     what has been recorded (Phase 2 run report)
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
    if args.command == "report":
        print("report: not available until Phase 5.", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(cli())
