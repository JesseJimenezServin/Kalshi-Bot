"""
Alerts (plan 2.9): crashes, errors, and later fills, exits and risk-limit hits.

Every alert is logged as a structured "ALERT" event regardless of channel,
so the log is the complete record. A channel adds delivery on top:
  none      log only (default until the owner picks a channel)
  telegram  POST to the Bot API, using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  discord, email   not implemented yet -- refused at startup rather than
                   silently dropping alerts.

Repeats of the same key are throttled, so a flapping condition is legible
instead of a flood (the liquidity lab's healthcheck lesson).
"""

from __future__ import annotations

import httpx

from bot.clock import Clock
from bot.config import AppConfig, ConfigError
from bot.monitoring.log import get_logger

log = get_logger("alerts")

THROTTLE_SEC = 900.0


class Alerter:
    def __init__(self, channel: str, clock: Clock, *, telegram_token: str | None = None,
                 telegram_chat_id: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if channel in ("discord", "email"):
            raise ConfigError(f"alerts.channel '{channel}' is not implemented yet; use 'none' or 'telegram'")
        if channel == "telegram" and not (telegram_token and telegram_chat_id):
            raise ConfigError("telegram alerts need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        self.channel = channel
        self._clock = clock
        self._token = telegram_token
        self._chat_id = telegram_chat_id
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._http = httpx.AsyncClient(timeout=10, transport=transport) if channel == "telegram" else None
        self.sent = 0

    @classmethod
    def from_config(cls, cfg: AppConfig, clock: Clock) -> Alerter:
        token = cfg.secrets.telegram_bot_token.get_secret_value() if cfg.secrets.telegram_bot_token else None
        return cls(cfg.file.alerts.channel, clock, telegram_token=token,
                   telegram_chat_id=cfg.secrets.telegram_chat_id)

    async def alert(self, key: str, message: str, *, level: str = "error", **context: object) -> bool:
        """Returns True if delivered (not throttled)."""
        now = self._clock.now()
        last = self._last.get(key)
        if last is not None and now - last < THROTTLE_SEC:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False
        repeats = self._suppressed.pop(key, 0)
        self._last[key] = now
        log.warning("ALERT", key=key, level=level, message=message, suppressed_repeats=repeats, **context)
        if self._http is not None:
            text = f"[kalshi-bot {level}] {message}" + (f" (+{repeats} repeats)" if repeats else "")
            try:
                r = await self._http.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                                          json={"chat_id": self._chat_id, "text": text[:4000]})
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.error("alert_delivery_failed", key=key, error=repr(e))
                return False
        self.sent += 1
        return True

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
