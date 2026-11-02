# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the mock extensions that support receipt lifecycle automation:
list filters, JSON detail, send, abonent/limit, rate-limit simulation and the
/__mock/* seeding endpoints.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)

ABONENT_KEY = "2" * 64
TODAY = date.today()
ROW_KEYS = [
    "KontrahentNazwa", "IdentyfikatorKontrahenta", "NIPKontrahenta", "DataWystawienia",
    "PelnyNumer", "Brutto", "FakturaId", "Rodzaj", "Waluta", "Zaplacono", "TerminPlatnosci",
    "CzyWyslano", "CzySzkic", "RodzajPowiazanegoPrzychodu", "NumerPowiazanegoPrzychodu",
    "KsefNumer", "KsefStatus",
]


def sign(path: str, key_name: str, body: str) -> str:
    signed_url = settings.base_url + path
    message = (signed_url + settings.user + key_name + body).encode("utf-8")
    digest = hmac.new(bytes.fromhex(settings.keys[key_name]), message, hashlib.sha1).hexdigest()
    return f"IAPIS user={settings.user}, hmac-sha1={digest}"


@pytest.fixture(autouse=True)
def _clean_state():
    saved = (settings.keys["abonent"], settings.rate_limit, settings.test_endpoints,
             settings.strict_list_window)
    settings.keys["abonent"] = ABONENT_KEY
    settings.rate_limit = 0
    settings.test_endpoints = True
    settings.strict_list_window = False
    client.post("/__mock/reset")
    yield
    settings.keys["abonent"], settings.rate_limit, settings.test_endpoints, \
        settings.strict_list_window = saved
    settings.test_endpoints = True
    client.post("/__mock/reset")


def get_json(path: str, query: str = "", key: str = "faktura") -> dict:
    headers = {"Authentication": sign(path, key, "")}
    return client.get(path + (("?" + query) if query else ""), headers=headers).json()["response"]


def post_json(path: str, payload: dict, key: str = "faktura") -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    headers = {"Authentication": sign(path, key, body), "Content-Type": "application/json"}
    return client.post(path, content=body, headers=headers).json()["response"]


def seed(**kwargs) -> dict:
    kwargs.setdefault("doc_type", "fakturaparagon")
    kwargs.setdefault("contractor", {"Nazwa": "Jan", "Identyfikator": "JAN1",
                                     "NIP": "1234567890", "Email": "jan@example.com"})
    kwargs.setdefault("gross", 100)
    kwargs.setdefault("issue_date", TODAY.isoformat())
    resp = client.post("/__mock/documents", json=kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def listing(query: str = "dataOd=2020-01-01") -> list[dict]:
    resp = get_json("/iapi/faktury.json", query)
    assert resp["Kod"] == 0, resp
    return resp["Wynik"]


def ids(rows: list[dict]) -> set[int]:
    return {r["FakturaId"] for r in rows}


# ----------------------------- list ----------------------------------------- #
def test_list_requires_data_od():
    resp = get_json("/iapi/faktury.json")
    assert resp["Kod"] == 201


def test_list_row_key_order_and_types():
    row = seed(ksef_status="OK", ksef_number="KSEF-1", linked_document="1/2026")
    rows = listing()
    assert list(rows[0].keys()) == ROW_KEYS
    assert isinstance(rows[0]["FakturaId"], int)
    assert rows[0]["Rodzaj"] == "prz_faktura_paragon"
    assert rows[0]["Brutto"] == 100
    assert rows[0]["KsefNumer"] == "KSEF-1"
    assert rows[0]["NumerPowiazanegoPrzychodu"] == "1/2026"
    assert row["FakturaId"] == rows[0]["FakturaId"]


def test_list_default_window_is_30_days_when_strict():
    settings.strict_list_window = True
    old = seed(issue_date="2025-01-10")
    far = seed(issue_date="2025-03-01")
    got = ids(listing("dataOd=2025-01-01"))
    assert old["FakturaId"] in got
    assert far["FakturaId"] not in got
    # explicit dataDo overrides the window
    assert far["FakturaId"] in ids(listing("dataOd=2025-01-01&dataDo=2025-12-31"))


def test_list_filter_kontrahent_and_nip():
    a = seed(contractor={"Nazwa": "A", "Identyfikator": "AAA", "NIP": "111-111-11-11"})
    b = seed(contractor={"Nazwa": "B", "Identyfikator": "BBB", "NIP": "2222222222"})
    assert ids(listing("dataOd=2020-01-01&kontrahent=AAA")) == {a["FakturaId"]}
    assert ids(listing("dataOd=2020-01-01&nipKontrahenta=2222222222")) == {b["FakturaId"]}
    assert ids(listing("dataOd=2020-01-01&nipKontrahenta=1111111111")) == {a["FakturaId"]}


def test_list_filter_typ():
    p = seed(doc_type="fakturaparagon")
    k = seed(doc_type="fakturakraj")
    assert ids(listing("dataOd=2020-01-01&typ=prz_faktura_paragon")) == {p["FakturaId"]}
    assert ids(listing("dataOd=2020-01-01&typ=prz_faktura_kraj")) == {k["FakturaId"]}


def test_list_filter_status_values():
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    paid = seed(gross=100, paid=100)
    partial = seed(gross=100, paid=40)
    unpaid = seed(gross=100, due_date=(TODAY + timedelta(days=5)).isoformat())
    overdue = seed(gross=100, due_date=yesterday)

    def only(status: str) -> set[int]:
        return ids(listing(f"dataOd=2020-01-01&status={status}"))

    assert only("oplacone") == {paid["FakturaId"]}
    assert only("oplaconeCzesciowo") == {partial["FakturaId"]}
    assert only("nieoplacone") == {unpaid["FakturaId"]}
    assert only("przeterminowane") == {overdue["FakturaId"]}
    both = only("oplacone,nieoplacone")
    assert both == {paid["FakturaId"], unpaid["FakturaId"]}
    assert get_json("/iapi/faktury.json", "dataOd=2020-01-01&status=bogus")["Kod"] == 201


def test_list_filter_amount():
    small = seed(gross=10)
    big = seed(gross=500)
    assert ids(listing("dataOd=2020-01-01&kwotaOd=100")) == {big["FakturaId"]}
    assert ids(listing("dataOd=2020-01-01&kwotaDo=100")) == {small["FakturaId"]}


def test_list_paging():
    created = [seed()["FakturaId"] for _ in range(5)]
    page1 = [r["FakturaId"] for r in listing("dataOd=2020-01-01&iloscNaStronie=2&strona=1")]
    page2 = [r["FakturaId"] for r in listing("dataOd=2020-01-01&iloscNaStronie=2&strona=2")]
    page3 = [r["FakturaId"] for r in listing("dataOd=2020-01-01&iloscNaStronie=2&strona=3")]
    page4 = listing("dataOd=2020-01-01&iloscNaStronie=2&strona=4")
    assert page1 + page2 + page3 == created
    assert len(page3) == 1 and page4 == []
    assert len(listing("dataOd=2020-01-01")) == 5  # default page size 20


def test_list_excludes_deleted():
    doc = seed()
    assert client.delete(f"/__mock/documents/{doc['FakturaId']}").status_code == 200
    assert listing() == []


# ----------------------------- detail --------------------------------------- #
def test_detail_returns_stamped_notes():
    node = {"Zaplacono": 0, "Uwagi": "Zamowienie 42",
            "DodatkoweUwagi": {"Typ": "NUMER_ZAMOWIENIA", "Info": "REF42"},
            "Kontrahent": {"Nazwa": "Jan"},
            "Pozycje": [{"NazwaPelna": "X", "Ilosc": 2, "CenaJednostkowa": 10, "StawkaVat": 0.23}]}
    created = post_json("/iapi/fakturaparagon.json", node)
    ident = created["Identyfikator"]
    detail = get_json(f"/iapi/fakturaparagon/{ident}.json")
    assert detail["Kod"] == 0
    assert detail["Identyfikator"] == ident
    assert detail["Uwagi"] == "Zamowienie 42"
    assert detail["DodatkoweUwagi"] == {"Typ": "NUMER_ZAMOWIENIA", "Info": "REF42"}
    assert "/" in detail["PelnyNumer"]
    assert detail["Zaplacono"] == 0
    # gross computed from positions, visible in list
    assert listing()[0]["Brutto"] == 20


def test_detail_unknown_and_deleted_are_404_envelope():
    assert get_json("/iapi/fakturaparagon/999.json")["Kod"] == 404
    doc = seed()
    client.delete(f"/__mock/documents/{doc['FakturaId']}")
    assert get_json(f"/iapi/fakturaparagon/{doc['FakturaId']}.json")["Kod"] == 404


def test_detail_requires_auth_and_pdf_route_still_works():
    doc = seed()
    resp = client.get(f"/iapi/fakturaparagon/{doc['FakturaId']}.json")
    assert resp.status_code == 401
    path = f"/iapi/fakturaparagon/{doc['FakturaId']}.pdf"
    pdf = client.get(path, headers={"Authentication": sign(path, "faktura", "")})
    assert pdf.content.startswith(b"%PDF")


# ----------------------------- send ----------------------------------------- #
def test_send_success_marks_sent_and_records_recipient():
    doc = seed(doc_type="fakturakraj")
    path = f"/iapi/fakturakraj/send/{doc['FakturaId']}.json"
    resp = post_json(path, {"Tekst": "Dziekujemy", "SkrzynkaEmailOdbiorcy": "x@example.com"})
    assert resp["Kod"] == 0
    assert resp["Informacja"] == "Wiadomość została wysłana."
    row = listing()[0]
    assert row["CzyWyslano"] is True
    from app import store
    assert store.get(doc["FakturaId"]).sent_to == ["x@example.com"]


def test_send_defaults_to_contractor_email():
    doc = seed(doc_type="fakturakraj")
    post_json(f"/iapi/fakturakraj/send/{doc['FakturaId']}.json", {})
    from app import store
    assert store.get(doc["FakturaId"]).sent_to == ["jan@example.com"]


def test_send_unsupported_for_receipts():
    doc = seed(doc_type="fakturaparagon")
    resp = post_json(f"/iapi/fakturaparagon/send/{doc['FakturaId']}.json", {})
    assert resp["Kod"] == 200
    assert listing()[0]["CzyWyslano"] is False


def test_send_unknown_id_and_long_text():
    assert post_json("/iapi/fakturakraj/send/777.json", {})["Kod"] == 404
    doc = seed(doc_type="fakturakraj")
    resp = post_json(f"/iapi/fakturakraj/send/{doc['FakturaId']}.json", {"Tekst": "a" * 1001})
    assert resp["Kod"] == 201


# ----------------------------- limit / rate limit --------------------------- #
def test_limit_counts_calls():
    first = get_json("/iapi/abonent/limit.json", key="abonent")
    get_json("/iapi/faktury.json", "dataOd=2020-01-01")
    second = get_json("/iapi/abonent/limit.json", key="abonent")
    assert first["Kod"] == 0 and first["LimitPrzyznany"] == 15000
    assert second["LimitWykorzystany"] == first["LimitWykorzystany"] + 2


def test_limit_without_abonent_key_is_rejected():
    settings.keys["abonent"] = ""
    path = "/iapi/abonent/limit.json"
    resp = client.get(path, headers={"Authentication": sign(path, "abonent", "")})
    assert resp.status_code == 401
    assert resp.json()["response"]["Kod"] != 0


def test_rate_limit_triggers_kod_100():
    settings.rate_limit = 2
    ok1 = get_json("/iapi/faktury.json", "dataOd=2020-01-01")
    ok2 = get_json("/iapi/faktury.json", "dataOd=2020-01-01")
    limited = get_json("/iapi/faktury.json", "dataOd=2020-01-01")
    assert ok1["Kod"] == 0 and ok2["Kod"] == 0
    assert limited["Kod"] == 100
    assert "limit" in limited["Informacja"]


# ----------------------------- test endpoints ------------------------------- #
def test_seed_creates_listable_external_document():
    from app import store
    row = seed(uwagi="ext", paid=10, gross=50, currency="EUR", draft=True)
    assert row["Waluta"] == "EUR" and row["CzySzkic"] is True and row["Zaplacono"] == 10
    assert store.get(row["FakturaId"]).created_by == "external"
    assert row["FakturaId"] in ids(listing())


def test_delete_hides_and_unknown_delete_404():
    doc = seed()
    assert client.delete(f"/__mock/documents/{doc['FakturaId']}").status_code == 200
    assert doc["FakturaId"] not in ids(listing())
    assert client.delete("/__mock/documents/9999").status_code == 404


def test_reset_clears_store_and_counters():
    seed()
    get_json("/iapi/faktury.json", "dataOd=2020-01-01")
    assert client.post("/__mock/reset").status_code == 200
    assert listing() == []
    # the listing call above is the only counted call after reset
    assert get_json("/iapi/abonent/limit.json", key="abonent")["LimitWykorzystany"] == 2


def test_test_endpoints_disabled_return_404():
    settings.test_endpoints = False
    assert client.post("/__mock/documents", json={}).status_code == 404
    assert client.delete("/__mock/documents/1").status_code == 404
    assert client.post("/__mock/reset").status_code == 404
