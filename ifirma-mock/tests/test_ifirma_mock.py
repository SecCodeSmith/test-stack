"""
Tests for the iFirma mock. The signing helper reproduces exactly what the
PrestaShop module's IfirmaApiClient does, so these tests double as a spec of the
authentication contract between the module and iFirma.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def sign(path: str, key_name: str, body: str) -> str:
    """Build the Authentication header the module would send for this request."""
    signed_url = settings.base_url + path  # query string excluded, like the module
    message = (signed_url + settings.user + key_name + body).encode("utf-8")
    digest = hmac.new(bytes.fromhex(settings.keys[key_name]), message, hashlib.sha1).hexdigest()
    return f"IAPIS user={settings.user}, hmac-sha1={digest}"


def create_receipt() -> dict:
    body = json.dumps(
        {
            "Zaplacono": 123.45,
            "LiczOd": "BRT",
            "Kontrahent": {"Nazwa": "Jan Kowalski", "Email": "jan@example.com"},
            "Pozycje": [
                {"NazwaPelna": "Produkt A", "Ilosc": 1, "CenaJednostkowa": 123.45, "StawkaVat": 0.23, "TypStawkiVat": "PRC"}
            ],
        },
        ensure_ascii=False,
    )
    headers = {"Authentication": sign("/iapi/fakturaparagon.json", "faktura", body), "Content-Type": "application/json"}
    resp = client.post("/iapi/fakturaparagon.json", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["response"]


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_create_rejects_unsigned_request():
    resp = client.post("/iapi/fakturaparagon.json", content="{}")
    assert resp.status_code == 401
    assert resp.json()["response"]["Kod"] != 0


def test_create_rejects_bad_hmac():
    body = json.dumps({"Pozycje": [{"NazwaPelna": "X", "Ilosc": 1, "CenaJednostkowa": 1, "StawkaVat": 0.23}]})
    headers = {"Authentication": f"IAPIS user={settings.user}, hmac-sha1=deadbeef"}
    resp = client.post("/iapi/fakturaparagon.json", content=body, headers=headers)
    assert resp.status_code == 401


def test_create_returns_string_identifier_without_number():
    # Faithful to iFirma: identifier is a string and the number is NOT in the
    # create response (it is read back from the list endpoint).
    node = create_receipt()
    assert node["Kod"] == 0
    assert isinstance(node["Identyfikator"], str)
    assert "PelnyNumer" not in node


def test_list_exposes_number_under_wynik():
    node = create_receipt()
    ident = node["Identyfikator"]

    # The query string must NOT be part of the signed URL (module strips it too).
    headers = {"Authentication": sign("/iapi/faktury.json", "faktura", "")}
    resp = client.get("/iapi/faktury.json?dataOd=2020-01-01", headers=headers)
    assert resp.status_code == 200

    data = resp.json()["response"]
    assert "Wynik" in data
    match = [row for row in data["Wynik"] if str(row["FakturaId"]) == str(ident)]
    assert match, "created document not found in list"
    assert "/" in match[0]["PelnyNumer"]


def test_register_payment_updates_status():
    node = create_receipt()
    ident = node["Identyfikator"]

    path = f"/iapi/faktury/wplaty/prz_faktura_kraj/{ident}.json"
    body = json.dumps({"Kwota": 123.45})
    headers = {"Authentication": sign(path, "faktura", body), "Content-Type": "application/json"}
    resp = client.post(path, content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["response"]["Kod"] == 0


def test_download_pdf_returns_pdf_bytes():
    node = create_receipt()
    ident = node["Identyfikator"]

    path = f"/iapi/fakturaparagon/{ident}.pdf"
    headers = {"Authentication": sign(path, "faktura", "")}
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


def test_download_pdf_duplicate_variant():
    node = create_receipt()
    ident = node["Identyfikator"]

    # The query string must NOT be part of the signed URL.
    path = f"/iapi/fakturaparagon/{ident}.pdf"
    headers = {"Authentication": sign(path, "faktura", "")}
    resp = client.get(path + "?typ=dup", headers=headers)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")


def test_unknown_document_pdf_is_json_error():
    path = "/iapi/fakturaparagon/999999.pdf"
    headers = {"Authentication": sign(path, "faktura", "")}
    resp = client.get(path, headers=headers)
    # iFirma returns a JSON error body even on a .pdf endpoint; the module detects this.
    assert resp.json()["response"]["Kod"] != 0
