"""Runtime configuration for the iFirma mock, read from environment variables."""
from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """Mirrors the credentials the PrestaShop module is configured with."""

    #: iFirma login the module signs its requests with.
    user: str = os.getenv("IFIRMA_MOCK_USER", "test@example.com")

    #: Hexadecimal keys, keyed by iFirma key name.
    keys: dict[str, str] = {
        "faktura": os.getenv(
            "IFIRMA_MOCK_KEY_FAKTURA",
            "1111111111111111111111111111111111111111111111111111111111111111",
        ),
        "abonent": os.getenv("IFIRMA_MOCK_KEY_ABONENT", ""),
        "rachunek": os.getenv("IFIRMA_MOCK_KEY_RACHUNEK", ""),
    }

    #: Public base URL the module signs against (scheme + host[:port], no trailing slash).
    #: Must match what you put in the module's "API base URL" field, minus the path.
    base_url: str = os.getenv("IFIRMA_MOCK_BASE_URL", "http://localhost:8001").rstrip("/")

    #: When True, requests with a bad/missing HMAC are rejected like the real API.
    strict_auth: bool = _env_bool("IFIRMA_MOCK_STRICT_AUTH", True)


settings = Settings()
