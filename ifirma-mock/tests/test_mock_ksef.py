# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the KSeF send endpoint (POST /iapi/{type}/ksef/send/{id}.json) and the
/__mock/ksef/{accept,reject} helpers, plus regression checks for the e-mail send
and PDF routes that share the /iapi/ prefix.
"""
from __future__ import annotations

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
