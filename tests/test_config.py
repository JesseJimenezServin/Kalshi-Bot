from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot.config import BASE_URLS, AppConfig, ConfigError, Environment, Mode, Secrets, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_valid_config_loads_in_paper_mode(config_data: dict[str, Any], write_config: Any, env_file: Any) -> None:
    cfg = load_config(write_config(config_data), env_file())
    assert cfg.mode is Mode.PAPER
    assert cfg.file.strategy.entry_threshold_cents == 20
    assert cfg.file.discovery.include_categories == ["Climate and Weather"]


def test_shipped_config_yaml_is_valid(env_file: Any) -> None:
    cfg = load_config(PROJECT_ROOT / "config.yaml", env_file())
    assert cfg.mode is Mode.PAPER, "the shipped config must default to paper"


def test_paper_reads_production_and_never_writes(config_data: dict[str, Any], write_config: Any,
                                                 env_file: Any) -> None:
    cfg = load_config(write_config(config_data), env_file())
    assert cfg.environment is Environment.PROD  # decision A: real market data
    assert cfg.rest_base_url == BASE_URLS[Environment.PROD][0]
    assert cfg.writes_allowed is False


def test_paper_never_writes_even_if_live_is_confirmed(config_data: dict[str, Any], write_config: Any,
                                                      env_file: Any) -> None:
    cfg = load_config(write_config(config_data), env_file(CONFIRM_LIVE_TRADING="yes"))
    assert cfg.writes_allowed is False


def test_demo_must_be_explicit_and_uses_demo_exchange(config_data: dict[str, Any], write_config: Any,
                                                      env_file: Any) -> None:
    config_data["mode"] = "demo"
    cfg = load_config(write_config(config_data), env_file())
    assert cfg.environment is Environment.DEMO
    assert "demo.kalshi.co" in cfg.rest_base_url and "demo.kalshi.co" in cfg.ws_url
    assert cfg.writes_allowed is True


def test_live_without_confirmation_refuses_to_start(config_data: dict[str, Any], write_config: Any,
                                                    env_file: Any) -> None:
    config_data["mode"] = "live"
    with pytest.raises(ConfigError, match="CONFIRM_LIVE_TRADING"):
        load_config(write_config(config_data), env_file(CONFIRM_LIVE_TRADING="no"))


@pytest.mark.parametrize("value", ["YES", "Yes", "true", "1", "y"])
def test_live_confirmation_must_be_exactly_yes(config_data: dict[str, Any], write_config: Any,
                                               env_file: Any, value: str) -> None:
    config_data["mode"] = "live"
    with pytest.raises(ConfigError):
        load_config(write_config(config_data), env_file(CONFIRM_LIVE_TRADING=value))


def test_live_confirmation_with_whitespace_in_real_env_is_refused(
        config_data: dict[str, Any], write_config: Any, env_file: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A .env parser strips surrounding whitespace; a real environment
    # variable keeps it, and " yes" is not "yes".
    config_data["mode"] = "live"
    monkeypatch.setenv("CONFIRM_LIVE_TRADING", " yes")
    with pytest.raises(ConfigError):
        load_config(write_config(config_data), env_file())


def test_live_with_confirmation_starts_and_may_write(config_data: dict[str, Any], write_config: Any,
                                                     env_file: Any) -> None:
    config_data["mode"] = "live"
    cfg = load_config(write_config(config_data), env_file(CONFIRM_LIVE_TRADING="yes"))
    assert cfg.environment is Environment.PROD
    assert cfg.writes_allowed is True


def test_writes_allowed_rechecks_confirmation_on_hand_built_config(config_data: dict[str, Any],
                                                                    write_config: Any, env_file: Any) -> None:
    config_data["mode"] = "live"
    good = load_config(write_config(config_data), env_file(CONFIRM_LIVE_TRADING="yes"))
    forged = AppConfig(file=good.file, secrets=good.secrets.model_copy(update={"confirm_live_trading": "no"}))
    assert forged.writes_allowed is False


def test_process_environment_overrides_env_file(config_data: dict[str, Any], write_config: Any,
                                                env_file: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config_data["mode"] = "live"
    monkeypatch.setenv("CONFIRM_LIVE_TRADING", "no")
    with pytest.raises(ConfigError):
        load_config(write_config(config_data), env_file(CONFIRM_LIVE_TRADING="yes"))


@pytest.mark.parametrize("section", ["mode", "discovery", "fair_value", "strategy", "risk", "alerts", "storage"])
def test_missing_section_is_rejected(config_data: dict[str, Any], write_config: Any, env_file: Any,
                                     section: str) -> None:
    del config_data[section]
    with pytest.raises(ConfigError, match=section):
        load_config(write_config(config_data), env_file())


@pytest.mark.parametrize(("section", "key"), [
    ("discovery", "scan_interval_sec"), ("fair_value", "min_confidence"), ("strategy", "stop_loss_cents"),
    ("strategy", "hold_to_settlement"), ("risk", "daily_loss_limit_dollars"),
])
def test_missing_key_is_rejected(config_data: dict[str, Any], write_config: Any, env_file: Any,
                                 section: str, key: str) -> None:
    del config_data[section][key]
    with pytest.raises(ConfigError, match=key):
        load_config(write_config(config_data), env_file())


def test_misspelled_key_is_rejected_not_ignored(config_data: dict[str, Any], write_config: Any,
                                                env_file: Any) -> None:
    config_data["risk"]["max_dollar_per_trade"] = 25  # typo
    with pytest.raises(ConfigError, match="max_dollar_per_trade"):
        load_config(write_config(config_data), env_file())


@pytest.mark.parametrize(("section", "key", "value"), [
    ("discovery", "scan_interval_sec", 0),
    ("discovery", "include_categories", []),
    ("fair_value", "min_confidence", 1.5),
    ("fair_value", "min_confidence", -0.1),
    ("fair_value", "min_history", 0),
    ("fair_value", "models", ["sibling", "magic"]),
    ("strategy", "entry_threshold_cents", 0),
    ("strategy", "entry_threshold_cents", 100),
    ("strategy", "stop_loss_cents", -5),
    ("strategy", "order_timeout_sec", 0),
    ("risk", "max_dollars_per_trade", 0),
    ("risk", "max_open_positions", -1),
    ("risk", "daily_loss_limit_dollars", 0),
    ("alerts", "channel", "pager"),
])
def test_out_of_range_values_are_rejected(config_data: dict[str, Any], write_config: Any, env_file: Any,
                                          section: str, key: str, value: object) -> None:
    config_data[section][key] = value
    with pytest.raises(ConfigError, match=key):
        load_config(write_config(config_data), env_file())


def test_unknown_mode_is_rejected(config_data: dict[str, Any], write_config: Any, env_file: Any) -> None:
    config_data["mode"] = "yolo"
    with pytest.raises(ConfigError, match="mode"):
        load_config(write_config(config_data), env_file())


def test_trade_size_cannot_exceed_total_exposure(config_data: dict[str, Any], write_config: Any,
                                                 env_file: Any) -> None:
    config_data["risk"]["max_dollars_per_trade"] = 500
    with pytest.raises(ConfigError, match="max_total_exposure_dollars"):
        load_config(write_config(config_data), env_file())


def test_missing_api_key_id_is_rejected(config_data: dict[str, Any], write_config: Any, env_file: Any) -> None:
    with pytest.raises(ConfigError, match="kalshi_api_key_id"):
        load_config(write_config(config_data), env_file(KALSHI_API_KEY_ID=""))


def test_key_path_must_exist(config_data: dict[str, Any], write_config: Any, env_file: Any,
                             tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="KALSHI_PRIVATE_KEY_PATH"):
        load_config(write_config(config_data), env_file(KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "nope.pem")))


def test_telegram_requires_credentials(config_data: dict[str, Any], write_config: Any, env_file: Any) -> None:
    config_data["alerts"]["channel"] = "telegram"
    with pytest.raises(ConfigError, match="TELEGRAM"):
        load_config(write_config(config_data), env_file())
    cfg = load_config(write_config(config_data), env_file(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c"))
    assert cfg.file.alerts.channel == "telegram"


def test_missing_or_malformed_file(tmp_path: Path, env_file: Any) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml", env_file())
    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad, env_file())
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("just a string")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(scalar, env_file())


def test_secrets_repr_does_not_leak_tokens(tmp_path: Path, key_file: Path) -> None:
    s = Secrets(kalshi_api_key_id="id", kalshi_private_key_path=key_file, telegram_bot_token="sekrit-token")
    assert "sekrit-token" not in repr(s)
