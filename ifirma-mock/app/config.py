# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

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

    #: Simulated rate limit: max authenticated calls per calendar minute (0 = off).
    rate_limit: int = int(os.getenv("IFIRMA_MOCK_RATE_LIMIT", "0") or 0)

    #: Simulated rate limit: max authenticated calls per calendar day (0 = off).
    rate_limit_day: int = int(os.getenv("IFIRMA_MOCK_RATE_LIMIT_DAY", "0") or 0)

    #: Enables the /__mock/* seeding endpoints (HTTP 404 when false).
    test_endpoints: bool = _env_bool("IFIRMA_MOCK_TEST_ENDPOINTS", True)

    #: The real faktury.json defaults dataDo to dataOd + 30 days. Off by default so the
    #: original "dataOd=2020-01-01" smoke call still lists freshly created documents.
    strict_list_window: bool = _env_bool("IFIRMA_MOCK_STRICT_LIST_WINDOW", False)

    #: File the mock keeps its state in across restarts (empty = memory only, nothing is written).
    state_file: str = os.getenv("IFIRMA_MOCK_STATE_FILE", "").strip()

    #: Discard the stored state at start-up (read at start-up only).
    clean_start: bool = _env_bool("IFIRMA_MOCK_CLEAN_START", False)

    #: Port of the SMTP sink that catches the shop's e-mails (0 = no listener).
    smtp_port: int = int(os.getenv("IFIRMA_MOCK_SMTP_PORT", "1025") or 0)


settings = Settings()
