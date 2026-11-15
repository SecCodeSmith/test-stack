# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the monthly API limit that abonent/limit.json reports (spec mock-rate-limit).

LimitWykorzystany counts the documents created through the API in the current calendar month,
LimitPrzyznany is 1000. The store takes its clock from ``store._now``, so month rollovers are
moved without sleeping.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import store
from app.config import settings
from app.main import app
from tests.test_mock_extensions import sign

http = TestClient(app)
ABONENT_PATH = "/iapi/abonent/limit.json"
CREATE_PATH = "/iapi/fakturaparagon.json"
MONTHLY_PATH = "/__mock/monthly-limit"
RECEIPT = {"Kontrahent": {"Nazwa": "Jan Kowalski", "KodPocztowy": "00-001", "Miejscowosc": "Warszawa",
                          "Identyfikator": "JAN1"},
           "Pozycje": [{"NazwaPelna": "x", "Ilosc": 1, "CenaJednostkowa": 10.0,
                        "StawkaVat": 0.23, "TypStawkiVat": "PRC"}]}


def local_epoch(month: int, day: int, hour: int = 10) -> float:
    """Epoch seconds of a local wall-clock time, so month keys do not depend on the host zone."""
    return time.mktime((2026, month, day, hour, 0, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = [local_epoch(3, 10)]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    monkeypatch.setattr(settings, "rate_limit", 0)
    monkeypatch.setattr(settings, "rate_limit_day", 0)
    monkeypatch.setattr(settings, "test_endpoints", True)
    monkeypatch.setattr(settings, "strict_auth", True)
    monkeypatch.setitem(settings.keys, "abonent", "2" * 64)
    store.reset_all()
    http.post("/__mock/contractors", json={"Nazwa": "Jan Kowalski", "KodPocztowy": "00-001",
                                           "Miejscowosc": "Warszawa", "Identyfikator": "JAN1"})
    yield now
    store.reset_all()


def api_create():
    body = json.dumps(RECEIPT, ensure_ascii=False)
    headers = {"Authentication": sign(CREATE_PATH, "faktura", body), "Content-Type": "application/json"}
    return http.post(CREATE_PATH, content=body, headers=headers)


def seed_document():
    return http.post("/__mock/documents", json={
        "doc_type": "fakturaparagon", "gross": 100,
        "contractor": {"Nazwa": "Jan Kowalski", "Identyfikator": "JAN1"}})


def abonent_limit() -> dict:
    headers = {"Authentication": sign(ABONENT_PATH, "abonent", "")}
    return http.get(ABONENT_PATH, headers=headers).json()["response"]


def set_monthly(body) -> object:
    return http.post(MONTHLY_PATH, json=body)


# --------------------------------------------------------------------------- #
# Counting                                                                     #
# --------------------------------------------------------------------------- #
def test_fresh_state_reports_zero_of_one_thousand():
    answer = abonent_limit()
    assert (answer["Kod"], answer["LimitWykorzystany"], answer["LimitPrzyznany"]) == (0, 0, 1000)


def test_documents_created_through_the_api_are_counted_for_the_month():
    for _ in range(3):
        assert api_create().status_code == 200
    assert abonent_limit()["LimitWykorzystany"] == 3


def test_seeded_documents_are_not_counted():
    assert seed_document().status_code == 200
    assert api_create().status_code == 200
    assert abonent_limit()["LimitWykorzystany"] == 1


def test_calls_that_are_not_document_creations_are_not_counted(clock):
    for _ in range(5):
        store.register_call()
    assert abonent_limit()["LimitWykorzystany"] == 0


def test_the_counter_restarts_when_the_month_changes(clock):
    api_create()
    api_create()
    clock[0] = local_epoch(4, 1, hour=0)
    assert abonent_limit()["LimitWykorzystany"] == 0
    api_create()
    assert abonent_limit()["LimitWykorzystany"] == 1


def test_a_rejected_create_is_not_counted():
    rejected = {**RECEIPT, "Pozycje": []}
    body = json.dumps(rejected, ensure_ascii=False)
    headers = {"Authentication": sign(CREATE_PATH, "faktura", body), "Content-Type": "application/json"}
    assert http.post(CREATE_PATH, content=body, headers=headers).json()["response"]["Kod"] != 0
    assert abonent_limit()["LimitWykorzystany"] == 0


# --------------------------------------------------------------------------- #
# Test-only overrides: POST /__mock/monthly-limit                              #
# --------------------------------------------------------------------------- #
def test_used_override_is_reported():
    resp = set_monthly({"used": 900})
    assert resp.status_code == 200
    assert resp.json() == {"used": 900, "granted": 1000}
    assert abonent_limit()["LimitWykorzystany"] == 900


def test_granted_override_is_reported():
    resp = set_monthly({"granted": 50})
    assert resp.json() == {"used": 0, "granted": 50}
    assert abonent_limit()["LimitPrzyznany"] == 50


def test_an_absent_field_keeps_its_override():
    set_monthly({"granted": 50})
    set_monthly({"used": 7})
    assert set_monthly({"used": 8}).json() == {"used": 8, "granted": 50}


def test_null_clears_only_that_override():
    set_monthly({"granted": 50, "used": 900})
    resp = set_monthly({"used": None})
    assert resp.json() == {"used": 0, "granted": 50}
    assert abonent_limit()["LimitWykorzystany"] == 0


def test_null_for_granted_restores_the_monthly_grant():
    set_monthly({"granted": 50})
    assert set_monthly({"granted": None}).json()["granted"] == 1000


def test_used_zero_is_a_real_override():
    api_create()
    set_monthly({"used": 0})
    assert abonent_limit()["LimitWykorzystany"] == 0


@pytest.mark.parametrize("body", [
    {},
    [],
    {"granted": 0},
    {"granted": -1},
    {"used": -1},
    {"granted": True},
    {"used": False},
    {"granted": 1.5},
    {"used": "10"},
    {"limit": 5},
    {"granted": 5, "limit": 1},
])
def test_invalid_body_is_refused_and_changes_nothing(body):
    set_monthly({"used": 4})
    resp = set_monthly(body)
    assert resp.status_code == 400
    assert resp.json()["detail"]
    assert abonent_limit()["LimitWykorzystany"] == 4


def test_body_that_is_not_json_is_refused():
    resp = http.post(MONTHLY_PATH, content=b"{", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_monthly_route_is_hidden_when_test_endpoints_are_disabled(monkeypatch):
    monkeypatch.setattr(settings, "test_endpoints", False)
    assert set_monthly({"used": 5}).status_code == 404
    assert store.monthly_overrides() == {}


def test_monthly_route_is_not_an_api_call():
    set_monthly({"used": 5, "granted": 9})
    assert store.calls_total() == 0


# --------------------------------------------------------------------------- #
# Reset, status and persistence                                                #
# --------------------------------------------------------------------------- #
def test_reset_clears_the_counter_and_the_overrides():
    api_create()
    set_monthly({"used": 5, "granted": 9})
    assert http.post("/__mock/reset").status_code == 200
    assert abonent_limit()["LimitWykorzystany"] == 0
    assert abonent_limit()["LimitPrzyznany"] == 1000


def test_status_json_shows_the_monthly_limit():
    api_create()
    set_monthly({"granted": 40})
    data = http.get("/__mock/status.json").json()
    assert data["monthly_limit"] == {"used": 1, "granted": 40}


def test_snapshot_keeps_the_month_count_and_the_overrides():
    api_create()
    api_create()
    set_monthly({"granted": 40})
    snap = store.snapshot()
    store.reset_all()
    store.restore(snap)
    assert abonent_limit()["LimitWykorzystany"] == 2
    assert abonent_limit()["LimitPrzyznany"] == 40


def test_a_snapshot_from_an_earlier_month_restores_to_zero(clock):
    api_create()
    snap = store.snapshot()
    clock[0] = local_epoch(4, 2)
    store.restore(snap)
    assert store.monthly_created() == 0


def test_the_default_grant_follows_the_configured_monthly_limit(monkeypatch):
    monkeypatch.setattr(settings, "monthly_limit", 2000)
    assert abonent_limit()["LimitPrzyznany"] == 2000


def test_raising_the_grant_mid_month_keeps_the_used_count():
    api_create()
    set_monthly({"granted": 2000})
    assert (abonent_limit()["LimitWykorzystany"], abonent_limit()["LimitPrzyznany"]) == (1, 2000)
