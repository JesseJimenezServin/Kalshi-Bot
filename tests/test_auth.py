"""Auth: signature format matches docs/API_NOTES.md section 2.

Kalshi publishes no example signature, and RSA-PSS is randomised anyway, so
correctness is shown by (a) the exact pre-sign string and (b) verifying each
signature with the matching public key using the documented parameters.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from bot.api.auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    AuthError,
    Signer,
    load_private_key,
    presign_text,
    sign,
    signing_path,
)
from bot.clock import FakeClock
from tests.conftest import write_pem


def test_presign_is_timestamp_method_path() -> None:
    assert presign_text(1703123456789, "get", "/trade-api/v2/portfolio/balance") == \
        "1703123456789GET/trade-api/v2/portfolio/balance"


def test_query_string_is_stripped_before_signing() -> None:
    # The docs' own example.
    assert signing_path("/trade-api/v2/portfolio/orders?limit=5") == "/trade-api/v2/portfolio/orders"


def test_full_url_reduces_to_path() -> None:
    assert signing_path("https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=5") == \
        "/trade-api/v2/markets"


def test_websocket_path() -> None:
    assert presign_text(1, "GET", "wss://external-api-ws.kalshi.com/trade-api/ws/v2") == "1GET/trade-api/ws/v2"


def test_refuses_paths_outside_trade_api() -> None:
    with pytest.raises(AuthError):
        signing_path("/markets")


def test_ed25519_signature_verifies_and_is_deterministic(ed25519_key: ed25519.Ed25519PrivateKey) -> None:
    text = "1703123456789GET/trade-api/v2/portfolio/balance"
    sig = sign(ed25519_key, text)
    raw = base64.b64decode(sig)
    assert len(raw) == 64
    ed25519_key.public_key().verify(raw, text.encode())
    assert sign(ed25519_key, text) == sig  # Ed25519 is deterministic


def test_rsa_signature_verifies_with_documented_pss_parameters(rsa_key: rsa.RSAPrivateKey) -> None:
    text = "1703123456789GET/trade-api/v2/portfolio/balance"
    raw = base64.b64decode(sign(rsa_key, text))
    rsa_key.public_key().verify(
        raw, text.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    with pytest.raises(InvalidSignature):  # and not with a different salt length
        rsa_key.public_key().verify(raw, text.encode(),
                                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=0),
                                    hashes.SHA256())


def test_headers_use_millisecond_clock_and_key_id(ed25519_key: ed25519.Ed25519PrivateKey) -> None:
    clock = FakeClock(start=1703123456.789)
    signer = Signer("my-key-id", ed25519_key, clock)
    h = signer.headers("GET", "/trade-api/v2/portfolio/balance?x=1")
    assert h[HEADER_KEY] == "my-key-id"
    assert h[HEADER_TIMESTAMP] == "1703123456789"
    ed25519_key.public_key().verify(base64.b64decode(h[HEADER_SIGNATURE]),
                                    b"1703123456789GET/trade-api/v2/portfolio/balance")


@pytest.mark.parametrize("fmt", [serialization.PrivateFormat.PKCS8, serialization.PrivateFormat.TraditionalOpenSSL])
def test_rsa_keys_load_in_pkcs1_and_pkcs8(tmp_path: Path, rsa_key: rsa.RSAPrivateKey,
                                          fmt: serialization.PrivateFormat) -> None:
    path = write_pem(rsa_key, tmp_path / "rsa.pem", fmt)
    assert Signer.from_file("id", path).key_type == "rsa"


def test_ed25519_pkcs8_detected_from_key_not_header(key_file: Path) -> None:
    assert key_file.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    assert Signer.from_file("id", key_file).key_type == "ed25519"


def test_bad_key_files(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="not found"):
        load_private_key(tmp_path / "missing.pem")
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key")
    with pytest.raises(AuthError, match="could not parse"):
        load_private_key(junk)


def test_empty_key_id_rejected(ed25519_key: ed25519.Ed25519PrivateKey) -> None:
    with pytest.raises(AuthError):
        Signer("", ed25519_key)
