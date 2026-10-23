"""
Kalshi request signing (docs/API_NOTES.md section 2).

Signed text is  timestamp_ms + METHOD + path , where path carries the
/trade-api/v2 prefix and NO query string. The WebSocket handshake signs
GET + /trade-api/ws/v2 the same way.

Two key types, told apart from the parsed key -- the PEM header can't do it,
since PKCS#8 RSA and Ed25519 both start "BEGIN PRIVATE KEY":
  Ed25519  signs the text directly (Kalshi's default for new keys).
  RSA      RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length.
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from bot.clock import Clock, SystemClock

PrivateKey = Ed25519PrivateKey | RSAPrivateKey

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"


class AuthError(Exception):
    pass


def load_private_key(path: Path | str) -> PrivateKey:
    p = Path(path).expanduser()
    try:
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    except FileNotFoundError as e:
        raise AuthError(f"private key file not found: {p}") from e
    except (ValueError, TypeError) as e:
        raise AuthError(f"could not parse private key at {p}: {e}") from e
    if not isinstance(key, Ed25519PrivateKey | RSAPrivateKey):
        raise AuthError(f"unsupported key type {type(key).__name__}; Kalshi accepts Ed25519 or RSA")
    return key


def signing_path(path_or_url: str) -> str:
    """The path component exactly as Kalshi signs it: no scheme/host, no query."""
    path = urlsplit(path_or_url).path
    if not path.startswith("/trade-api/"):
        raise AuthError(f"refusing to sign a path outside /trade-api/: {path!r}")
    return path


def presign_text(timestamp_ms: int, method: str, path_or_url: str) -> str:
    return f"{timestamp_ms}{method.upper()}{signing_path(path_or_url)}"


def sign(key: PrivateKey, text: str) -> str:
    message = text.encode("utf-8")
    if isinstance(key, Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("ascii")


class Signer:
    def __init__(self, key_id: str, key: PrivateKey, clock: Clock | None = None) -> None:
        if not key_id:
            raise AuthError("empty API key id")
        self.key_id = key_id
        self._key = key
        self._clock = clock or SystemClock()

    @classmethod
    def from_file(cls, key_id: str, path: Path | str, clock: Clock | None = None) -> Signer:
        return cls(key_id, load_private_key(path), clock)

    @property
    def key_type(self) -> str:
        return "ed25519" if isinstance(self._key, Ed25519PrivateKey) else "rsa"

    def headers(self, method: str, path_or_url: str) -> dict[str, str]:
        ts = int(self._clock.now() * 1000)
        return {
            HEADER_KEY: self.key_id,
            HEADER_TIMESTAMP: str(ts),
            HEADER_SIGNATURE: sign(self._key, presign_text(ts, method, path_or_url)),
        }
