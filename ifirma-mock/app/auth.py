# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
HMAC-SHA1 verification that mirrors the PrestaShop module's request signing.

The module signs:  message = signedUrl + user + keyName + rawBody
where signedUrl is the full request URL WITHOUT the query string, and the key is
the hex string decoded to bytes. The header sent is:

    Authentication: IAPIS user=<user>, hmac-sha1=<hexdigest>
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from .config import settings

_AUTH_RE = re.compile(
    r"IAPIS\s+user=(?P<user>.+?),\s*hmac-sha1=(?P<hmac>[0-9a-fA-F]+)\s*$"
)


@dataclass
class AuthResult:
    ok: bool
    reason: str = ""
    user: str = ""


def _signed_url(path: str) -> str:
    """Rebuild the exact URL string the module signed (base host + path, no query)."""
    return settings.base_url + path


def verify(header_value: str | None, path: str, key_name: str, raw_body: bytes) -> AuthResult:
    """Validate the Authentication header for a request.

    :param header_value: raw value of the Authentication header
    :param path: request path (no query string)
    :param key_name: iFirma key name expected for this endpoint (e.g. "faktura")
    :param raw_body: exact request body bytes that were signed
    """
    if not header_value:
        return AuthResult(False, "Missing Authentication header.")

    match = _AUTH_RE.search(header_value.strip())
    if not match:
        return AuthResult(False, "Malformed Authentication header.")

    user = match.group("user").strip()
    provided = match.group("hmac").strip().lower()

    if user != settings.user:
        return AuthResult(False, f"Unknown user '{user}'.", user)

    key_hex = settings.keys.get(key_name, "")
    if not key_hex:
        return AuthResult(False, f"No key configured for '{key_name}'.", user)

    try:
        key_bytes = bytes.fromhex(key_hex)
    except ValueError:
        return AuthResult(False, "Configured key is not valid hex.", user)

    content = raw_body.decode("utf-8") if raw_body else ""
    message = (_signed_url(path) + user + key_name + content).encode("utf-8")
    expected = hmac.new(key_bytes, message, hashlib.sha1).hexdigest()

    if not hmac.compare_digest(expected, provided):
        return AuthResult(False, "HMAC signature mismatch.", user)

    return AuthResult(True, "", user)
