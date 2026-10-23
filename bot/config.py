"""
Configuration: tunables from config.yaml, secrets from the environment/.env.

Validated once at startup. Anything missing, misspelled or out of range is a
ConfigError and the bot does not start -- every model forbids unknown keys and
has no defaults, so config.yaml must state every value explicitly.

Mode gating lives here because it is a property of the configuration, not of
any one module:

  paper  reads PRODUCTION market data (docs/PLAN.md decision A) and can never
         send a write request -- see `writes_allowed`.
  demo   talks to the demo exchange and may place (mock-funded) orders.
  live   talks to production and may place real orders, and only starts if
         CONFIRM_LIVE_TRADING is exactly "yes".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """The configuration is invalid; the bot must not start."""


class Mode(StrEnum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class Environment(StrEnum):
    """Which Kalshi exchange we talk to. Distinct from Mode: paper uses prod."""

    PROD = "prod"
    DEMO = "demo"


# docs/API_NOTES.md section 1. The recommended "external-api" hosts.
BASE_URLS: dict[Environment, tuple[str, str]] = {
    Environment.PROD: (
        "https://external-api.kalshi.com/trade-api/v2",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    ),
    Environment.DEMO: (
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
}

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
# Cent thresholds are gaps between prices, so they live strictly inside 0-100.
Cents = Annotated[float, Field(gt=0, lt=100)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveryConfig(_Strict):
    scan_interval_sec: PositiveFloat
    new_market_window_min: PositiveFloat
    watch_duration_min: PositiveFloat
    include_categories: Annotated[list[str], Field(min_length=1)]
    exclude_series: list[str]
    sweep_interval_min: PositiveFloat
    sweep_lookback_hours: PositiveFloat


class SiblingModelConfig(_Strict):
    uncertainty_scale_cents: PositiveFloat
    liquidity_ref_contracts: PositiveFloat


class HistoricalModelConfig(_Strict):
    max_confidence: Annotated[float, Field(ge=0, le=1)]
    seasonal_window_days: Annotated[int, Field(ge=0, le=183)]


class FairValueConfig(_Strict):
    min_confidence: Annotated[float, Field(ge=0, le=1)]
    min_history: PositiveInt
    models: Annotated[list[Literal["sibling", "historical", "external"]], Field(min_length=1)]
    agree_within_cents: Cents
    sibling: SiblingModelConfig
    historical: HistoricalModelConfig


class StrategyConfig(_Strict):
    entry_threshold_cents: Cents
    min_depth_contracts: PositiveFloat
    take_profit_gap_cents: Cents
    stop_loss_cents: Cents
    max_hold_hours: PositiveFloat
    pre_close_exit_min: PositiveFloat
    hold_to_settlement: bool
    order_timeout_sec: PositiveFloat
    max_exit_slippage_cents: Cents


class RiskConfig(_Strict):
    max_dollars_per_trade: PositiveFloat
    max_total_exposure_dollars: PositiveFloat
    max_positions_per_event: PositiveInt
    max_open_positions: PositiveInt
    daily_loss_limit_dollars: PositiveFloat

    @model_validator(mode="after")
    def _trade_fits_exposure(self) -> RiskConfig:
        if self.max_dollars_per_trade > self.max_total_exposure_dollars:
            raise ValueError("max_dollars_per_trade exceeds max_total_exposure_dollars")
        return self


class RecordingConfig(_Strict):
    snapshot_min_interval_sec: PositiveFloat
    snapshot_heartbeat_sec: PositiveFloat
    depth_levels: PositiveInt
    status_refresh_min: PositiveFloat

    @model_validator(mode="after")
    def _heartbeat_not_shorter(self) -> RecordingConfig:
        if self.snapshot_heartbeat_sec < self.snapshot_min_interval_sec:
            raise ValueError("snapshot_heartbeat_sec must be >= snapshot_min_interval_sec")
        return self


class AlertsConfig(_Strict):
    channel: Literal["telegram", "discord", "email", "none"]


class StorageConfig(_Strict):
    database_url: Annotated[str, Field(min_length=1)]


class FileConfig(_Strict):
    """Exactly the shape of config.yaml."""

    mode: Mode
    discovery: DiscoveryConfig
    fair_value: FairValueConfig
    strategy: StrategyConfig
    risk: RiskConfig
    recording: RecordingConfig
    alerts: AlertsConfig
    storage: StorageConfig


class Secrets(BaseSettings):
    """From the process environment, falling back to .env. Never logged."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    kalshi_api_key_id: Annotated[str, Field(min_length=1)]
    kalshi_private_key_path: Path
    confirm_live_trading: str = "no"
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None


@dataclass(frozen=True)
class AppConfig:
    file: FileConfig
    secrets: Secrets

    @property
    def mode(self) -> Mode:
        return self.file.mode

    @property
    def environment(self) -> Environment:
        return Environment.DEMO if self.mode is Mode.DEMO else Environment.PROD

    @property
    def rest_base_url(self) -> str:
        return BASE_URLS[self.environment][0]

    @property
    def ws_url(self) -> str:
        return BASE_URLS[self.environment][1]

    @property
    def writes_allowed(self) -> bool:
        """Whether ANY non-GET request may leave this process.

        Paper mode reads production with a key that is able to trade, so this
        is the line that keeps a paper-mode bug from placing a real order.
        load_config already refuses live without confirmation; checking it
        again here keeps the guard correct even for a hand-built AppConfig.
        """
        if self.mode is Mode.DEMO:
            return True
        if self.mode is Mode.LIVE:
            return self.secrets.confirm_live_trading == "yes"
        return False


def _format_validation_error(source: str, err: ValidationError) -> str:
    lines = [f"{source} is invalid:"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


def load_config(config_path: Path | str = "config.yaml", env_file: Path | str | None = ".env") -> AppConfig:
    """Load and validate everything, or raise ConfigError explaining why not."""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping at the top level")

    try:
        file_cfg = FileConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(str(path), e)) from e

    try:
        secrets = Secrets(_env_file=env_file) if env_file else Secrets()  # type: ignore[call-arg]
    except ValidationError as e:
        raise ConfigError(_format_validation_error("environment / .env", e)) from e

    if not secrets.kalshi_private_key_path.expanduser().is_file():
        raise ConfigError(f"KALSHI_PRIVATE_KEY_PATH does not point to a file: {secrets.kalshi_private_key_path}")

    if file_cfg.mode is Mode.LIVE and secrets.confirm_live_trading != "yes":
        raise ConfigError(
            "mode is 'live' but CONFIRM_LIVE_TRADING is not exactly 'yes'. "
            "Live trading needs both; refusing to start."
        )

    if file_cfg.alerts.channel == "telegram" and not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        raise ConfigError("alerts.channel is 'telegram' but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")

    return AppConfig(file=file_cfg, secrets=secrets)
