# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the legacy-mode test switch (/__mock/legacy-mode). Off by default. While on,
a new document may carry any RodzajPodpisuOdbiorcy, any StawkaVat and an unknown nested
Kontrahent.Identyfikator (the mock creates that contractor); every other rule still applies.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import store
from app.config import settings
from app.main import app

client = TestClient(app)

RECEIPT = "/iapi/fakturaparagon.json"
POSITIONS = [{"NazwaPelna": "Produkt", "Ilosc": 1, "CenaJednostkowa": 10, "StawkaVat": 0.23, "TypStawkiVat": "PRC"}]
CONTRACTOR = {"Nazwa": "Jan Kowalski", "KodPocztowy": "00-001", "Miejscowosc": "Warszawa",
              "Identyfikator": "jan@example.test"}
LEGACY_IDENTIFIER = "jan@example.test"


def sign(path: str, key_name: str, body: str) -> str:
    signed_url = settings.base_url + path
    message = (signed_url + settings.user + key_name + body).encode("utf-8")
    digest = hmac.new(bytes.fromhex(settings.keys[key_name]), message, hashlib.sha1).hexdigest()
    return f"IAPIS user={settings.user}, hmac-sha1={digest}"


@pytest.fixture(autouse=True)
def _clean_state():
    saved = settings.test_endpoints
    settings.test_endpoints = True
    client.post("/__mock/reset")
    yield
    settings.test_endpoints = True
    client.post("/__mock/reset")
    settings.test_endpoints = saved


def post_document(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    headers = {"Authentication": sign(RECEIPT, "faktura", body), "Content-Type": "application/json"}
    return client.post(RECEIPT, content=body, headers=headers).json()["response"]


def document(**extra) -> dict:
    return {"Pozycje": POSITIONS, **extra}


def brk_document() -> dict:
    return document(RodzajPodpisuOdbiorcy="BRK")


def stawka_document() -> dict:
    return document(Pozycje=[{**POSITIONS[0], "StawkaVat": 0.2301}])


def unknown_contractor_document() -> dict:
    return document(Kontrahent=dict(CONTRACTOR))


def set_legacy(enabled) -> None:
    resp = client.post("/__mock/legacy-mode", json={"enabled": enabled})
    assert resp.status_code == 200, resp.text


def assert_rejected(resp: dict, kod: int) -> None:
    assert resp["Kod"] == kod, resp
    assert store.all_documents() == []


# ----------------------------- default off ---------------------------------- #
def test_legacy_mode_defaults_to_off():
    assert client.get("/__mock/legacy-mode").json() == {"enabled": False}


def test_default_off_rejects_brk_stawka_and_unknown_contractor():
    assert_rejected(post_document(brk_document()), 200)
    assert_rejected(post_document(stawka_document()), 201)
    resp = post_document(unknown_contractor_document())
    assert resp["Kod"] == 201
    assert store.contractor_get(LEGACY_IDENTIFIER) is None
    assert store.all_documents() == []


# ----------------------------- switched on ---------------------------------- #
def test_switched_on_accepts_brk_and_any_stawka():
    set_legacy(True)
    for payload in (brk_document(), stawka_document()):
        resp = post_document(payload)
        assert resp["Kod"] == 0, resp
        assert resp["Identyfikator"]
    assert len(store.all_documents()) == 2


def test_switched_on_creates_unknown_nested_contractor_under_sent_identifier():
    set_legacy(True)
    resp = post_document(unknown_contractor_document())
    assert resp["Kod"] == 0, resp
    created = store.get(int(resp["Identyfikator"]))
    assert store.contractor_get(LEGACY_IDENTIFIER) is not None
    assert created.payload["Kontrahent"]["Identyfikator"] == LEGACY_IDENTIFIER


# ----------------------------- other rules still apply ---------------------- #
def test_switched_on_still_rejects_undocumented_liczod():
    set_legacy(True)
    assert_rejected(post_document(document(LiczOd="XYZ")), 200)


def test_switched_on_still_requires_pkwiu_for_zw():
    set_legacy(True)
    resp = post_document(document(Pozycje=[{**POSITIONS[0], "TypStawkiVat": "ZW"}]))
    assert_rejected(resp, 201)


def test_switched_on_still_requires_kontrahent_fields():
    set_legacy(True)
    block = {k: v for k, v in CONTRACTOR.items() if k != "KodPocztowy"}
    assert_rejected(post_document(document(Kontrahent=block)), 201)


def test_switched_on_still_requires_positions():
    set_legacy(True)
    resp = post_document({"Pozycje": []})
    assert_rejected(resp, 400)


# ----------------------------- reset and endpoint gating -------------------- #
def test_reset_clears_legacy_mode():
    set_legacy(True)
    assert client.get("/__mock/legacy-mode").json() == {"enabled": True}
    client.post("/__mock/reset")
    assert client.get("/__mock/legacy-mode").json() == {"enabled": False}
    assert_rejected(post_document(brk_document()), 200)


def test_legacy_mode_endpoints_disabled_return_404():
    settings.test_endpoints = False
    assert client.post("/__mock/legacy-mode", json={"enabled": True}).status_code == 404
    assert client.get("/__mock/legacy-mode").status_code == 404


@pytest.mark.parametrize("body", [{"enabled": "yes"}, {}, {"enabled": 1}])
def test_bad_body_is_400_and_leaves_switch_unchanged(body):
    set_legacy(True)
    resp = client.post("/__mock/legacy-mode", json=body)
    assert resp.status_code == 400
    assert resp.json() == {"detail": "enabled must be true or false"}
    assert client.get("/__mock/legacy-mode").json() == {"enabled": True}
