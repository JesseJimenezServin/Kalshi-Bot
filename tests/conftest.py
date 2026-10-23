from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

SECRET_VARS = [
    "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "CONFIRM_LIVE_TRADING",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]

VALID_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "discovery": {"scan_interval_sec": 5, "new_market_window_min": 10, "watch_duration_min": 60,
                  "include_categories": ["Climate and Weather"], "exclude_series": []},
    "fair_value": {"min_confidence": 0.6, "min_history": 20, "models": ["sibling", "historical"]},
    "strategy": {"entry_threshold_cents": 20, "min_depth_contracts": 50, "take_profit_gap_cents": 5,
                 "stop_loss_cents": 15, "max_hold_hours": 48, "pre_close_exit_min": 30,
                 "hold_to_settlement": False, "order_timeout_sec": 30, "max_exit_slippage_cents": 5},
    "risk": {"max_dollars_per_trade": 25, "max_total_exposure_dollars": 200, "max_positions_per_event": 1,
             "max_open_positions": 10, "daily_loss_limit_dollars": 50},
    "alerts": {"channel": "none"},
    "storage": {"database_url": "sqlite:///:memory:"},
}


@pytest.fixture(autouse=True)
def _isolate_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's real environment leak into a test."""
    for var in SECRET_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def ed25519_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def write_pem(key: Any, path: Path, fmt: serialization.PrivateFormat = serialization.PrivateFormat.PKCS8) -> Path:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()))
    return path


@pytest.fixture
def key_file(tmp_path: Path, ed25519_key: ed25519.Ed25519PrivateKey) -> Path:
    return write_pem(ed25519_key, tmp_path / "test_key.pem")


@pytest.fixture
def config_data() -> dict[str, Any]:
    return copy.deepcopy(VALID_CONFIG)


@pytest.fixture
def write_config(tmp_path: Path) -> Any:
    def _write(data: dict[str, Any]) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(data))
        return p

    return _write


@pytest.fixture
def env_file(tmp_path: Path, key_file: Path) -> Any:
    def _write(**overrides: str) -> Path:
        values = {"KALSHI_API_KEY_ID": "test-key-id", "KALSHI_PRIVATE_KEY_PATH": str(key_file),
                  "CONFIRM_LIVE_TRADING": "no"}
        values.update(overrides)
        p = tmp_path / ".env"
        p.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        return p

    return _write
