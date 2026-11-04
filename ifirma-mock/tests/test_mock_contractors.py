# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the contractor (kontrahenci) endpoints and the production-faithful rule that
a document referencing an unknown Kontrahent.Identyfikator is rejected, while a null
identifier makes iFirma create the contractor automatically.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import store
from app.config import settings
from app.main import app

client = TestClient(app)

GENERATED_ID = re.compile(r"^K\d{6}$")
RECEIPT = "/iapi/fakturaparagon.json"
POSITIONS = [{"NazwaPelna": "Produkt", "Ilosc": 1, "CenaJednostkowa": 10, "StawkaVat": 0.23}]


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


def contractor(**overrides) -> dict:
    data = {
        "Nazwa": "Firma Testowa Sp. z o.o.",
        "NIP": "5252525252",
        "KodPocztowy": "00-001",
        "Miejscowosc": "Warszawa",
        "Ulica": "Marszałkowska 1",
        "Email": "biuro@example.com",
    }
    data.update(overrides)
    return data


def without(key: str, **overrides) -> dict:
    data = contractor(**overrides)
    del data[key]
    return data


def get_json(path: str, query: str = "") -> dict:
    headers = {"Authentication": sign(path, "faktura", "")}
    return client.get(path + (("?" + query) if query else ""), headers=headers).json()["response"]


def send_json(method: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    headers = {"Authentication": sign(path, "faktura", body), "Content-Type": "application/json"}
    return getattr(client, method)(path, content=body, headers=headers).json()["response"]


def create_contractor(payload: dict) -> dict:
    return send_json("post", "/iapi/kontrahenci.json", payload)


def by_id(identifier: str) -> dict:
    return get_json(f"/iapi/kontrahenci/id/{identifier}.json")


def listing() -> list[dict]:
    resp = get_json("/iapi/faktury.json", "dataOd=2020-01-01")
    assert resp["Kod"] == 0, resp
    return resp["Wynik"]


def receipt(kontrahent: dict | None) -> dict:
    payload = {"Pozycje": POSITIONS}
    if kontrahent is not None:
        payload["Kontrahent"] = kontrahent
    return send_json("post", RECEIPT, payload)


# ----------------------------- get by id ------------------------------------ #
def test_get_by_id_found_returns_documented_shape():
    assert create_contractor(contractor(Identyfikator="FIRMA1"))["Kod"] == 0
    resp = by_id("FIRMA1")
    assert resp["Kod"] == 0
    assert list(resp["Wynik"][0].keys()) == list(store.CONTRACTOR_FIELDS)
    found = resp["Wynik"][0]
    assert found["Identyfikator"] == "FIRMA1"
    assert found["Nazwa"] == "Firma Testowa Sp. z o.o."
    assert found["JestDostawca"] is False  # missing booleans are filled with false
    assert found["Telefon"] is None  # missing strings are filled with null


def test_get_by_id_not_found_is_208():
    resp = by_id("NIEMA")
    assert resp == {"Kod": 208, "Informacja": "Nie znaleziono kontrahenta"}


# ----------------------------- create --------------------------------------- #
def test_create_returns_identifier_as_wynik():
    resp = create_contractor(contractor(Identyfikator="FIRMA1"))
    assert resp == {"Kod": 0, "Informacja": "Dodano kontrahenta", "Wynik": "FIRMA1"}


@pytest.mark.parametrize("identifier", [None, ""])
def test_create_generates_identifier_when_null_or_empty(identifier):
    resp = create_contractor(contractor(Identyfikator=identifier))
    assert resp["Kod"] == 0
    assert GENERATED_ID.match(resp["Wynik"])
    assert by_id(resp["Wynik"])["Kod"] == 0


def test_create_duplicate_identifier_is_201():
    create_contractor(contractor(Identyfikator="FIRMA1"))
    resp = create_contractor(contractor(Identyfikator="FIRMA1", Nazwa="Inna"))
    assert resp["Kod"] == 201
    assert resp["Informacja"] == "Kontrahent o podanym identyfikatorze już istnieje"
    assert by_id("FIRMA1")["Wynik"][0]["Nazwa"] == "Firma Testowa Sp. z o.o."


def test_create_missing_kod_pocztowy_is_201_validation():
    resp = create_contractor(without("KodPocztowy", Identyfikator="FIRMA1"))
    assert resp["Kod"] == 201
    assert resp["Informacja"] == "Validation errors: KodPocztowy"
    assert by_id("FIRMA1")["Kod"] == 208


def test_create_validates_all_required_fields_and_lengths():
    resp = create_contractor(contractor(Nazwa="x" * 151, Miejscowosc="", KodPocztowy="1" * 17))
    assert resp["Kod"] == 201
    assert resp["Informacja"] == "Validation errors: Nazwa, KodPocztowy, Miejscowosc"


def test_create_identifier_length_limit_15():
    assert create_contractor(contractor(Identyfikator="A" * 16))["Kod"] == 201
    ok = create_contractor(contractor(Identyfikator="A" * 15))
    assert ok == {"Kod": 0, "Informacja": "Dodano kontrahenta", "Wynik": "A" * 15}


# ----------------------------- replace -------------------------------------- #
def test_put_replaces_fields_and_keeps_identifier():
    create_contractor(contractor(Identyfikator="FIRMA1"))
    resp = send_json("put", "/iapi/kontrahenci/FIRMA1.json",
                     {"Nazwa": "Nowa Nazwa", "KodPocztowy": "30-001", "Miejscowosc": "Kraków"})
    assert resp == {"Kod": 0, "Informacja": "Zaktualizowano kontrahenta", "Wynik": "FIRMA1"}
    stored = by_id("FIRMA1")["Wynik"][0]
    assert stored["Identyfikator"] == "FIRMA1"
    assert stored["Nazwa"] == "Nowa Nazwa"
    assert stored["Miejscowosc"] == "Kraków"
    assert stored["Email"] is None  # full replace: fields absent from the body are cleared
    assert stored["NIP"] is None


def test_put_unknown_identifier_is_208():
    resp = send_json("put", "/iapi/kontrahenci/NIEMA.json", contractor())
    assert resp["Kod"] == 208


# ----------------------------- search --------------------------------------- #
def test_search_by_name_fragment_and_nip():
    create_contractor(contractor(Identyfikator="ALFA", Nazwa="Alfa Handel", NIP="1111111111"))
    create_contractor(contractor(Identyfikator="BETA", Nazwa="Beta Usługi", NIP="2222222222"))

    by_name = get_json("/iapi/kontrahenci/handel.json")
    assert by_name["Kod"] == 0
    assert [c["Identyfikator"] for c in by_name["Wynik"]] == ["ALFA"]

    by_nip = get_json("/iapi/kontrahenci/2222222222.json")
    assert [c["Identyfikator"] for c in by_nip["Wynik"]] == ["BETA"]

    by_identifier = get_json("/iapi/kontrahenci/alfa.json")
    assert [c["Identyfikator"] for c in by_identifier["Wynik"]] == ["ALFA"]


def test_search_no_results_is_208():
    create_contractor(contractor(Identyfikator="ALFA"))
    resp = get_json("/iapi/kontrahenci/nieistnieje.json")
    assert resp == {"Kod": 208, "Informacja": "Brak wyników"}


def test_search_caps_results_at_20():
    for i in range(25):
        assert create_contractor(contractor(Nazwa=f"Seria {i}", NIP=None))["Kod"] == 0
    resp = get_json("/iapi/kontrahenci/Seria.json")
    assert resp["Kod"] == 0
    assert len(resp["Wynik"]) == 20


# ----------------------------- documents ------------------------------------ #
def test_document_with_unknown_identifier_is_rejected_and_stores_nothing():
    resp = receipt({"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Ktoś", "Identyfikator": "NIEMA"})
    assert resp["Kod"] == 201
    assert resp["Informacja"] == "Nie znaleziono kontrahenta o zadanym identyfikatorze"
    assert listing() == []
    assert by_id("NIEMA")["Kod"] == 208


def test_document_with_null_identifier_auto_creates_contractor():
    resp = receipt({"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Jan Kowalski", "Identyfikator": None, "Email": "jan@example.com"})
    assert resp["Kod"] == 0
    rows = listing()
    assert len(rows) == 1
    identifier = rows[0]["IdentyfikatorKontrahenta"]
    assert GENERATED_ID.match(identifier)
    created = by_id(identifier)
    assert created["Kod"] == 0
    assert created["Wynik"][0]["Nazwa"] == "Jan Kowalski"
    assert created["Wynik"][0]["Email"] == "jan@example.com"


def test_document_with_empty_identifier_auto_creates_contractor():
    assert receipt({"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Anna", "Identyfikator": ""})["Kod"] == 0
    identifier = listing()[0]["IdentyfikatorKontrahenta"]
    assert GENERATED_ID.match(identifier)
    assert by_id(identifier)["Kod"] == 0


def test_document_with_existing_identifier_succeeds():
    create_contractor(contractor(Identyfikator="FIRMA1"))
    assert receipt({"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Firma Testowa", "Identyfikator": "FIRMA1"})["Kod"] == 0
    row = listing()[0]
    assert row["IdentyfikatorKontrahenta"] == "FIRMA1"
    assert row["KontrahentNazwa"] == "Firma Testowa"


def test_document_without_kontrahent_block_still_works():
    assert receipt(None)["Kod"] == 0
    assert listing()[0]["IdentyfikatorKontrahenta"] == ""


def document(extra: dict, kontrahent: dict | None = None) -> dict:
    """A receipt with top-level lookup keys (IdentyfikatorKontrahenta / NIPKontrahenta)."""
    payload = {"Pozycje": POSITIONS, **extra}
    if kontrahent is not None:
        payload["Kontrahent"] = kontrahent
    return send_json("post", RECEIPT, payload)


def test_top_level_identifier_attaches_stored_contractor_and_ignores_block_name():
    create_contractor(contractor(Identyfikator="FIRMA1"))
    resp = document({"IdentyfikatorKontrahenta": "FIRMA1"}, {"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Ktoś inny", "Identyfikator": None})
    assert resp["Kod"] == 0
    row = listing()[0]
    assert row["IdentyfikatorKontrahenta"] == "FIRMA1"
    assert row["KontrahentNazwa"] == "Firma Testowa Sp. z o.o."
    assert by_id("FIRMA1")["Wynik"][0]["Nazwa"] == "Firma Testowa Sp. z o.o."


def test_top_level_unknown_identifier_without_nip_creates_contractor_from_block():
    resp = document({"IdentyfikatorKontrahenta": "NIEMA"}, {"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Jan Kowalski", "Identyfikator": None})
    assert resp["Kod"] == 0
    row = listing()[0]
    assert GENERATED_ID.match(row["IdentyfikatorKontrahenta"])
    assert row["IdentyfikatorKontrahenta"] != "NIEMA"
    assert row["KontrahentNazwa"] == "Jan Kowalski"
    assert by_id(row["IdentyfikatorKontrahenta"])["Wynik"][0]["Nazwa"] == "Jan Kowalski"
    assert by_id("NIEMA")["Kod"] == 208


def test_nip_keyword_attaches_contractor_stored_with_dashes():
    create_contractor(contractor(Identyfikator="FIRMA1", NIP="525-252-52-52"))
    resp = document({"NIPKontrahenta": "5252525252"}, {"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Ktoś inny", "Identyfikator": None})
    assert resp["Kod"] == 0
    row = listing()[0]
    assert row["IdentyfikatorKontrahenta"] == "FIRMA1"
    assert row["NIPKontrahenta"] == "525-252-52-52"


def test_nested_unknown_identifier_without_top_level_fields_is_201():
    create_contractor(contractor(Identyfikator="FIRMA1"))
    resp = document({}, {"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Ktoś", "Identyfikator": "NIEMA"})
    assert resp == {"Kod": 201, "Informacja": "Nie znaleziono kontrahenta o zadanym identyfikatorze"}
    assert listing() == []


def test_top_level_identifier_longer_than_15_chars_is_accepted_by_mock():
    client.post("/__mock/contractors", json=contractor(Identyfikator="EC_JACEK ORZECHO"))
    resp = document({"IdentyfikatorKontrahenta": "EC_JACEK ORZECHO"})
    assert resp["Kod"] == 0
    assert listing()[0]["IdentyfikatorKontrahenta"] == "EC_JACEK ORZECHO"


# ----------------------------- test endpoints ------------------------------- #
def test_document_seeding_registers_unknown_contractor():
    body = {
        "doc_type": "fakturaparagon",
        "contractor": {"Nazwa": "Seed", "Identyfikator": "SEED1", "Email": "s@example.com"},
        "gross": 50,
    }
    assert client.post("/__mock/documents", json=body).status_code == 200
    assert by_id("SEED1")["Kod"] == 0
    assert by_id("SEED1")["Wynik"][0]["Nazwa"] == "Seed"
    # a seeded document with a registered contractor behaves like any other: the
    # identifier can be used on the next create
    assert receipt({"KodPocztowy": "00-001", "Miejscowosc": "Warszawa", "Nazwa": "Seed", "Identyfikator": "SEED1"})["Kod"] == 0


def test_document_seeding_keeps_existing_contractor():
    create_contractor(contractor(Identyfikator="SEED1", Nazwa="Oryginał"))
    body = {"contractor": {"Nazwa": "Kopia", "Identyfikator": "SEED1"}, "gross": 50}
    client.post("/__mock/documents", json=body)
    assert by_id("SEED1")["Wynik"][0]["Nazwa"] == "Oryginał"


def test_seed_contractor_endpoint_upserts_without_hmac():
    resp = client.post("/__mock/contractors", json=contractor(Identyfikator="SEED2"))
    assert resp.status_code == 200
    assert resp.json()["Identyfikator"] == "SEED2"
    assert by_id("SEED2")["Wynik"][0]["Nazwa"] == "Firma Testowa Sp. z o.o."
    client.post("/__mock/contractors", json=contractor(Identyfikator="SEED2", Nazwa="Zmieniona"))
    assert by_id("SEED2")["Wynik"][0]["Nazwa"] == "Zmieniona"


def test_seed_contractor_without_identifier_generates_one():
    stored = client.post("/__mock/contractors", json=contractor()).json()
    assert GENERATED_ID.match(stored["Identyfikator"])


def test_seed_contractor_endpoint_disabled_returns_404():
    settings.test_endpoints = False
    assert client.post("/__mock/contractors", json=contractor()).status_code == 404


def test_reset_clears_contractors():
    create_contractor(contractor(Identyfikator="FIRMA1"))
    assert client.post("/__mock/reset").status_code == 200
    assert by_id("FIRMA1")["Kod"] == 208
    # generated identifiers restart from the beginning after a reset
    assert GENERATED_ID.match(create_contractor(contractor(Identyfikator=None))["Wynik"])


def test_search_with_encoded_phrase_signed_over_raw_path():
    """The module signs the percent-encoded URL it sends; the mock must accept that form."""
    seed = client.post("/__mock/contractors", json={"Identyfikator": "JD-01", "Nazwa": "John DOE", "KodPocztowy": "00-001", "Miejscowosc": "Warszawa"})
    assert seed.status_code == 200, seed.text
    path = "/iapi/kontrahenci/John%20DOE.json"
    resp = client.get(path, headers={"Authentication": sign(path, "faktura", "")})
    assert resp.status_code == 200, resp.text
    node = resp.json()["response"]
    assert node["Kod"] == 0 and node["Wynik"][0]["Identyfikator"] == "JD-01"


# ----------------------------- rename mode (test switch) -------------------- #
def set_contractor_mode(mode: str):
    return client.post("/__mock/contractor-mode", json={"create": mode})


def test_contractor_mode_defaults_to_normal_and_rejects_unknown_values():
    assert client.get("/__mock/contractor-mode").json() == {"create": "normal"}
    assert set_contractor_mode("bogus").status_code == 400
    assert client.get("/__mock/contractor-mode").json() == {"create": "normal"}


def test_rename_mode_stores_under_generated_id_and_echoes_sent_id():
    assert set_contractor_mode("rename").status_code == 200
    assert client.get("/__mock/contractor-mode").json() == {"create": "rename"}

    resp = create_contractor(contractor(Identyfikator="PS-C7"))
    assert resp == {"Kod": 0, "Informacja": "Dodano kontrahenta", "Wynik": "PS-C7"}
    assert by_id("PS-C7") == {"Kod": 208, "Informacja": "Nie znaleziono kontrahenta"}

    rows = get_json("/iapi/kontrahenci/5252525252.json")["Wynik"]
    assert len(rows) == 1
    assert rows[0]["Identyfikator"] == "IFI7000001"
    assert by_id("IFI7000001")["Wynik"][0]["Nazwa"] == "Firma Testowa Sp. z o.o."


def test_rename_mode_keeps_generated_identifier_for_null_identifier():
    set_contractor_mode("rename")
    resp = create_contractor(contractor(Identyfikator=None))
    assert resp["Kod"] == 0
    assert GENERATED_ID.match(resp["Wynik"])
    assert by_id(resp["Wynik"])["Kod"] == 0


def test_reset_restores_normal_contractor_mode():
    set_contractor_mode("rename")
    assert client.post("/__mock/reset").status_code == 200
    assert client.get("/__mock/contractor-mode").json() == {"create": "normal"}
    assert create_contractor(contractor(Identyfikator="PS-C7"))["Wynik"] == "PS-C7"
    assert by_id("PS-C7")["Kod"] == 0
