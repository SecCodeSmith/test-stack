# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the KSeF send endpoint (POST /iapi/{type}/ksef/send/{id}.json) and the
/__mock/ksef/{accept,reject} helpers, plus regression checks for the e-mail send
and PDF routes that share the /iapi/ prefix.
"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.test_mock_extensions import get_json, listing, post_json, seed, sign

client = TestClient(app)

KSEF_NUMBER_RE = re.compile(r"^\d{10}-\d{8}-[0-9A-F]{12}-[0-9A-F]{2}$")
UNSUPPORTED_INFO = "Niepoprawna zawartość żądania - nie można utworzyć obiektu na podstawie zawartości żądania."


@pytest.fixture(autouse=True)
def _clean_state():
    saved = settings.test_endpoints
    settings.test_endpoints = True
    client.post("/__mock/reset")
    yield
    settings.test_endpoints = saved
    client.post("/__mock/reset")


def ksef_send(doc_type: str, ident: str, payload: dict) -> dict:
    return post_json(f"/iapi/{doc_type}/ksef/send/{ident}.json", payload)


def row_for(doc_id: int) -> dict:
    rows = [r for r in listing() if r["FakturaId"] == doc_id]
    assert len(rows) == 1
    return rows[0]


def test_send_now_marks_wysylana_without_number():
    doc = seed(doc_type="fakturakraj")
    resp = ksef_send("fakturakraj", str(doc["FakturaId"]), {"DataWysylki": None})
    assert resp == {"Kod": 0, "Informacja": "Faktura została wysłana do KSeF."}
    row = row_for(doc["FakturaId"])
    assert row["KsefStatus"] == "WYSLANA"
    assert row["KsefNumer"] is None


def test_second_send_is_refused():
    doc = seed(doc_type="fakturakraj")
    ident = str(doc["FakturaId"])
    assert ksef_send("fakturakraj", ident, {"DataWysylki": None})["Kod"] == 0
    resp = ksef_send("fakturakraj", ident, {"DataWysylki": None})
    assert resp["Kod"] == 201
    assert resp["Informacja"] == "Faktura została już wysłana do KSeF"


def test_unknown_id_is_kod_201():
    resp = ksef_send("fakturakraj", "987654", {"DataWysylki": None})
    assert resp == {"Kod": 201, "Informacja": "Nie znaleziono faktury o zadanym identyfikatorze"}


def test_deleted_document_is_unknown():
    doc = seed(doc_type="fakturakraj")
    client.delete(f"/__mock/documents/{doc['FakturaId']}")
    resp = ksef_send("fakturakraj", str(doc["FakturaId"]), {"DataWysylki": None})
    assert resp["Kod"] == 201


def test_unsupported_type_is_kod_200():
    doc = seed(doc_type="fakturakraj")
    resp = ksef_send("fakturaproformakraj", str(doc["FakturaId"]), {"DataWysylki": None})
    assert resp == {"Kod": 200, "Informacja": UNSUPPORTED_INFO}


def test_missing_data_wysylki_is_kod_201():
    doc = seed(doc_type="fakturakraj")
    resp = ksef_send("fakturakraj", str(doc["FakturaId"]), {})
    assert resp == {"Kod": 201, "Informacja": "DataWysylki: nieprawidłowa wartość"}
    assert row_for(doc["FakturaId"])["KsefStatus"] is None


@pytest.mark.parametrize("value", ["2026-13-01", "01-02-2026", 20260101, "2026-01-01T10:00"])
def test_invalid_data_wysylki_is_kod_201(value):
    doc = seed(doc_type="fakturakraj")
    resp = ksef_send("fakturakraj", str(doc["FakturaId"]), {"DataWysylki": value})
    assert resp["Kod"] == 201
    assert resp["Informacja"] == "DataWysylki: nieprawidłowa wartość"


def test_scheduled_date_marks_zaplanowana():
    doc = seed(doc_type="fakturakraj")
    resp = ksef_send("fakturakraj", str(doc["FakturaId"]), {"DataWysylki": "2026-12-01"})
    assert resp["Kod"] == 0
    assert row_for(doc["FakturaId"])["KsefStatus"] == "ZAPLANOWANA"


def test_number_with_underscore_identifier_works():
    doc = seed(doc_type="fakturakraj")
    underscored = doc["PelnyNumer"].replace("/", "_")
    assert "_" in underscored
    assert ksef_send("fakturakraj", underscored, {"DataWysylki": None})["Kod"] == 0
    assert row_for(doc["FakturaId"])["KsefStatus"] == "WYSLANA"


def test_korekta_path_is_routed():
    doc = seed(doc_type="fakturakraj")
    resp = ksef_send("fakturakraj/korekta", str(doc["FakturaId"]), {"DataWysylki": None})
    assert resp["Kod"] == 0


def test_accept_helper_sets_przyjeta_and_number():
    doc = seed(doc_type="fakturakraj", contractor={"Nazwa": "Jan", "Identyfikator": "JAN1",
                                                    "NIP": "526-000-11-22", "Email": "j@example.com"})
    ident = str(doc["FakturaId"])
    ksef_send("fakturakraj", ident, {"DataWysylki": None})
    resp = client.post(f"/__mock/ksef/accept/{ident}")
    assert resp.status_code == 200, resp.text
    row = row_for(doc["FakturaId"])
    assert row["KsefStatus"] == "PRZYJETA"
    assert KSEF_NUMBER_RE.match(row["KsefNumer"])
    assert row["KsefNumer"].startswith("5260001122-")
    assert row["KsefNumer"].split("-")[1] == date.today().strftime("%Y%m%d")


def test_accept_without_nip_uses_zero_prefix():
    doc = seed(doc_type="fakturakraj", contractor={"Nazwa": "X", "Identyfikator": "X1"})
    client.post(f"/__mock/ksef/accept/{doc['FakturaId']}")
    row = row_for(doc["FakturaId"])
    assert KSEF_NUMBER_RE.match(row["KsefNumer"])
    assert row["KsefNumer"].startswith("0000000000-")


def test_accept_unknown_id_is_404():
    assert client.post("/__mock/ksef/accept/424242").status_code == 404


def test_reject_helper_sets_odrzucona():
    doc = seed(doc_type="fakturakraj")
    ident = str(doc["FakturaId"])
    ksef_send("fakturakraj", ident, {"DataWysylki": None})
    assert client.post(f"/__mock/ksef/reject/{ident}").status_code == 200
    assert row_for(doc["FakturaId"])["KsefStatus"] == "ODRZUCONA"
    assert client.post("/__mock/ksef/reject/424242").status_code == 404


def test_regression_email_send_route_still_works():
    doc = seed(doc_type="fakturakraj")
    resp = post_json(f"/iapi/fakturakraj/send/{doc['FakturaId']}.json", {"Tekst": "hej"})
    assert resp == {"Kod": 0, "Informacja": "Wiadomość została wysłana."}
    assert row_for(doc["FakturaId"])["CzyWyslano"] is True


def test_regression_pdf_route_still_works():
    doc = seed(doc_type="fakturakraj")
    path = f"/iapi/fakturakraj/{doc['FakturaId']}.pdf"
    resp = client.get(path, headers={"Authentication": sign(path, "faktura", "")})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


# --------------------------------------------------------------------------- #
# KSeF simulation beyond the send endpoint (spec mock-ksef-simulation)         #
# --------------------------------------------------------------------------- #
from app import store  # noqa: E402

BUYER_WITH_NIP = {"Nazwa": "Firma", "NIP": "5260001122",
                  "KodPocztowy": "00-001", "Miejscowosc": "Warszawa"}
BUYER_PRIVATE = {"Nazwa": "Jan Kowalski",
                 "KodPocztowy": "00-001", "Miejscowosc": "Warszawa"}


def api_create(doc_type: str, buyer: dict) -> int:
    payload = {"Kontrahent": dict(buyer),
               "Pozycje": [{"NazwaPelna": "Usługa", "Ilosc": 1, "CenaJednostkowa": 100.0,
                            "StawkaVat": 0.23, "TypStawkiVat": "PRC"}]}
    resp = post_json(f"/iapi/{doc_type}.json", payload)
    assert resp["Kod"] == 0, resp
    return int(resp["Identyfikator"])


def detail(doc_type: str, doc_id: int) -> dict:
    resp = get_json(f"/iapi/{doc_type}/{doc_id}.json")
    assert resp["Kod"] == 0, resp
    return resp


def set_ksef_auto(mode: str):
    return client.post("/__mock/ksef-auto", json={"mode": mode})


def test_detail_of_a_submitted_invoice_carries_the_ksef_state():
    doc = seed(doc_type="fakturakraj")
    ksef_send("fakturakraj", str(doc["FakturaId"]), {"DataWysylki": None})
    answer = detail("fakturakraj", doc["FakturaId"])
    assert (answer["KsefStatus"], answer["KsefNumer"]) == ("WYSLANA", "")


def test_detail_of_an_accepted_invoice_matches_the_list():
    doc = seed(doc_type="fakturakraj")
    ident = str(doc["FakturaId"])
    ksef_send("fakturakraj", ident, {"DataWysylki": None})
    client.post(f"/__mock/ksef/accept/{ident}")
    answer = detail("fakturakraj", doc["FakturaId"])
    row = row_for(doc["FakturaId"])
    assert answer["KsefStatus"] == "PRZYJETA"
    assert answer["KsefNumer"] == row["KsefNumer"] != ""


def test_detail_of_a_document_without_ksef_state_has_empty_strings():
    doc = seed(doc_type="fakturaparagon")
    answer = detail("fakturaparagon", doc["FakturaId"])
    assert (answer["KsefStatus"], answer["KsefNumer"]) == ("", "")


def test_ksef_auto_is_off_by_default():
    assert client.get("/__mock/ksef-auto").json() == {"mode": "off"}
    doc_id = api_create("fakturakraj", BUYER_WITH_NIP)
    assert detail("fakturakraj", doc_id)["KsefStatus"] == ""


def test_ksef_auto_send_marks_a_new_invoice_for_a_buyer_with_a_nip():
    assert set_ksef_auto("send").json() == {"mode": "send"}
    doc_id = api_create("fakturakraj", BUYER_WITH_NIP)
    assert detail("fakturakraj", doc_id)["KsefStatus"] == "WYSLANA"
    assert row_for(doc_id)["KsefStatus"] == "WYSLANA"
    assert store.get(doc_id).ksef_send_calls == 0


def test_ksef_auto_accept_stores_przyjeta_with_a_number():
    set_ksef_auto("accept")
    doc_id = api_create("fakturakraj", BUYER_WITH_NIP)
    answer = detail("fakturakraj", doc_id)
    assert answer["KsefStatus"] == "PRZYJETA"
    assert KSEF_NUMBER_RE.match(answer["KsefNumer"])
    assert answer["KsefNumer"].startswith("5260001122-")


@pytest.mark.parametrize("doc_type, buyer", [
    ("fakturaparagon", BUYER_PRIVATE),
    ("fakturakraj", BUYER_PRIVATE),
])
def test_ksef_auto_leaves_a_buyer_without_a_nip_alone(doc_type, buyer):
    set_ksef_auto("send")
    doc_id = api_create(doc_type, buyer)
    assert detail(doc_type, doc_id)["KsefStatus"] == ""


def test_ksef_auto_does_not_touch_seeded_external_documents():
    set_ksef_auto("send")
    doc = seed(doc_type="fakturakraj")
    assert row_for(doc["FakturaId"])["KsefStatus"] is None


@pytest.mark.parametrize("body", [{"mode": "maybe"}, {"mode": 1}, {}, [], {"mode": "send", "x": 1}])
def test_ksef_auto_refuses_an_invalid_mode(body):
    set_ksef_auto("send")
    resp = client.post("/__mock/ksef-auto", json=body)
    assert resp.status_code == 422
    assert client.get("/__mock/ksef-auto").json() == {"mode": "send"}


def test_reset_sets_ksef_auto_back_to_off():
    set_ksef_auto("accept")
    client.post("/__mock/reset")
    assert client.get("/__mock/ksef-auto").json() == {"mode": "off"}


def test_ksef_auto_routes_answer_404_when_test_endpoints_are_off():
    settings.test_endpoints = False
    assert client.get("/__mock/ksef-auto").status_code == 404
    assert client.post("/__mock/ksef-auto", json={"mode": "send"}).status_code == 404


def test_one_submission_counts_one_send_call():
    doc = seed(doc_type="fakturakraj")
    ksef_send("fakturakraj", str(doc["FakturaId"]), {"DataWysylki": None})
    assert store.get(doc["FakturaId"]).ksef_send_calls == 1


def test_a_refused_second_submission_is_counted_too():
    doc = seed(doc_type="fakturakraj")
    ident = str(doc["FakturaId"])
    ksef_send("fakturakraj", ident, {"DataWysylki": None})
    answer = ksef_send("fakturakraj", ident, {"DataWysylki": None})
    assert answer["Kod"] != 0 and "już wysłana do KSeF" in answer["Informacja"]
    assert store.get(doc["FakturaId"]).ksef_send_calls == 2


def test_a_scheduled_invoice_refuses_another_send():
    doc = seed(doc_type="fakturakraj")
    ident = str(doc["FakturaId"])
    ksef_send("fakturakraj", ident, {"DataWysylki": "2026-12-01"})
    answer = ksef_send("fakturakraj", ident, {"DataWysylki": None})
    assert answer["Kod"] != 0 and "już wysłana do KSeF" in answer["Informacja"]
    assert row_for(doc["FakturaId"])["KsefStatus"] == "ZAPLANOWANA"


def test_a_send_refused_by_the_rate_limit_changes_nothing():
    doc = seed(doc_type="fakturakraj")
    ident = str(doc["FakturaId"])
    store.set_rate_limits(per_minute=1)
    store.register_call()
    path = f"/iapi/fakturakraj/ksef/send/{ident}.json"
    body = json.dumps({"DataWysylki": None})
    resp = client.post(path, content=body, headers={"Authentication": sign(path, "faktura", body),
                                                    "Content-Type": "application/json"})
    assert resp.status_code == 429
    assert row_for_unlimited(doc["FakturaId"])["KsefStatus"] is None
    assert store.get(doc["FakturaId"]).ksef_send_calls == 0


def row_for_unlimited(doc_id: int) -> dict:
    store.set_rate_limits(per_minute=0)
    return row_for(doc_id)
