# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for enumeration validation on document creation. A documented enum field
holding an undocumented value is answered with the live API's generic Kod 200
"could not map the request" error and nothing is stored.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import store
from app.config import settings
from app.main import (
    ALLOWED_STAWKA_VAT,
    ENUM_ERROR_INFO,
    ENUM_ERROR_KOD,
    ENUM_FORMAT_DATY_SPRZEDAZY,
    ENUM_LICZ_OD,
    ENUM_RODZAJ_PODPISU_ODBIORCY,
    ENUM_SPOSOB_ZAPLATY,
    ENUM_TYP_STAWKI_VAT,
    app,
)

client = TestClient(app)

RECEIPT = "/iapi/fakturaparagon.json"
INVOICE = "/iapi/fakturakraj.json"
POSITIONS = [{"NazwaPelna": "Produkt", "Ilosc": 1, "CenaJednostkowa": 10, "StawkaVat": 0.23, "TypStawkiVat": "PRC"}]


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


def post_document(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    headers = {"Authentication": sign(path, "faktura", body), "Content-Type": "application/json"}
    return client.post(path, content=body, headers=headers).json()["response"]


def document(**extra) -> dict:
    return {"Pozycje": POSITIONS, **extra}


def assert_rejected(resp: dict) -> None:
    assert resp == {"Kod": ENUM_ERROR_KOD, "Informacja": ENUM_ERROR_INFO}, resp
    assert store.all_documents() == []


def test_constants_match_documented_values():
    assert ENUM_RODZAJ_PODPISU_ODBIORCY == ("OUP", "UPO", "BPO", "BWO")
    assert ENUM_LICZ_OD == ("NET", "BRT")
    assert ENUM_FORMAT_DATY_SPRZEDAZY == ("DZN", "MSC")
    assert ENUM_SPOSOB_ZAPLATY == (
        "GTK", "POB", "PRZ", "KAR", "PZA", "CZK", "KOM", "BAR", "DOT", "PAL", "ALG", "P24", "TPA", "ELE",
    )
    assert ENUM_TYP_STAWKI_VAT == ("PRC", "ZW")


def test_undocumented_signing_role_is_rejected_and_nothing_stored():
    resp = post_document(RECEIPT, document(RodzajPodpisuOdbiorcy="BRK"))
    assert_rejected(resp)
    assert resp["Informacja"] == "Niepoprawna zawartość żądania - nie można utworzyć obiektu na podstawie zawartości żądania."


@pytest.mark.parametrize("value", ["OUP", "UPO", "BPO", "BWO"])
def test_documented_signing_roles_are_accepted(value):
    resp = post_document(RECEIPT, document(RodzajPodpisuOdbiorcy=value))
    assert resp["Kod"] == 0, resp


@pytest.mark.parametrize("path", [RECEIPT, INVOICE])
def test_invalid_liczod_is_rejected(path):
    assert_rejected(post_document(path, document(LiczOd="GROSS")))


def test_invalid_format_daty_sprzedazy_is_rejected():
    assert_rejected(post_document(INVOICE, document(FormatDatySprzedazy="RRR")))


def test_invalid_sposob_zaplaty_is_rejected():
    assert_rejected(post_document(RECEIPT, document(SposobZaplaty="XXX")))


def test_invalid_typ_stawki_vat_in_position_is_rejected():
    positions = [{**POSITIONS[0], "TypStawkiVat": "PCT"}]
    assert_rejected(post_document(RECEIPT, document(Pozycje=positions)))


def test_invalid_value_in_second_position_is_rejected():
    positions = [POSITIONS[0], {**POSITIONS[0], "TypStawkiVat": "PCT"}]
    assert_rejected(post_document(RECEIPT, document(Pozycje=positions)))


def test_non_string_enum_value_is_rejected():
    assert_rejected(post_document(RECEIPT, document(LiczOd=["BRT"])))


def test_absent_and_null_enum_fields_are_not_checked():
    resp = post_document(RECEIPT, document(RodzajPodpisuOdbiorcy=None, LiczOd=None))
    assert resp["Kod"] == 0, resp


def test_valid_document_with_all_enums_still_creates():
    resp = post_document(
        INVOICE,
        document(
            RodzajPodpisuOdbiorcy="BPO",
            LiczOd="BRT",
            FormatDatySprzedazy="MSC",
            SposobZaplaty="P24",
            Pozycje=[{**POSITIONS[0], "TypStawkiVat": "ZW", "PKWiU": "86.90.19.0"}],
        ),
    )
    assert resp["Kod"] == 0, resp
    assert len(store.all_documents()) == 1


CONTRACTOR_BLOCK = {"Nazwa": "Jan Kowalski", "KodPocztowy": "00-001", "Miejscowosc": "Warszawa"}


def assert_validation_rejected(resp: dict, field: str) -> None:
    assert resp["Kod"] == 201, resp
    assert field in resp["Informacja"], resp
    assert store.all_documents() == []


def with_position(**extra) -> list:
    return [{**POSITIONS[0], **extra}]


def test_allowed_stawka_vat_constant_matches_documented_rates():
    assert ALLOWED_STAWKA_VAT == (0, 0.05, 0.08, 0.23)


@pytest.mark.parametrize("value", [0.23, 0.08, 0, 0.0, 0.05, None])
def test_documented_stawka_vat_is_accepted(value):
    resp = post_document(INVOICE, document(Pozycje=with_position(StawkaVat=value)))
    assert resp["Kod"] == 0, resp


def test_undocumented_stawka_vat_is_rejected_naming_field():
    resp = post_document(INVOICE, document(Pozycje=with_position(StawkaVat=0.07)))
    assert_validation_rejected(resp, "Pozycje[0].StawkaVat")
    assert resp["Informacja"] == "Pozycje[0].StawkaVat: niedozwolona stawka VAT"


def test_non_numeric_stawka_vat_is_rejected():
    resp = post_document(INVOICE, document(Pozycje=with_position(StawkaVat="0.23")))
    assert_validation_rejected(resp, "StawkaVat")


def test_stawka_vat_error_names_the_offending_position_index():
    positions = [POSITIONS[0], {**POSITIONS[0], "StawkaVat": 0.5}]
    resp = post_document(INVOICE, document(Pozycje=positions))
    assert_validation_rejected(resp, "Pozycje[1].StawkaVat")


def test_zw_without_pkwiu_is_rejected_naming_field():
    resp = post_document(INVOICE, document(Pozycje=with_position(TypStawkiVat="ZW", StawkaVat=0)))
    assert_validation_rejected(resp, "Pozycje[0].PKWiU")
    assert resp["Informacja"] == "Pozycje[0].PKWiU: pole wymagane dla stawki ZW"


@pytest.mark.parametrize("pkwiu", ["", "   ", None])
def test_zw_with_blank_pkwiu_is_rejected(pkwiu):
    resp = post_document(INVOICE, document(Pozycje=with_position(TypStawkiVat="ZW", PKWiU=pkwiu)))
    assert_validation_rejected(resp, "PKWiU")


def test_zw_with_pkwiu_is_accepted():
    resp = post_document(INVOICE, document(Pozycje=with_position(TypStawkiVat="ZW", PKWiU="86.90.19.0", StawkaVat=0)))
    assert resp["Kod"] == 0, resp


def test_prc_without_pkwiu_is_accepted():
    resp = post_document(INVOICE, document(Pozycje=with_position(TypStawkiVat="PRC")))
    assert resp["Kod"] == 0, resp


def test_kontrahent_without_miejscowosc_is_rejected_naming_field():
    block = {k: v for k, v in CONTRACTOR_BLOCK.items() if k != "Miejscowosc"}
    resp = post_document(INVOICE, document(Kontrahent=block))
    assert_validation_rejected(resp, "Kontrahent.Miejscowosc")
    assert resp["Informacja"] == "Kontrahent.Miejscowosc: pole wymagane"


def test_kontrahent_with_empty_nazwa_is_rejected():
    resp = post_document(INVOICE, document(Kontrahent={**CONTRACTOR_BLOCK, "Nazwa": ""}))
    assert_validation_rejected(resp, "Kontrahent.Nazwa")


def test_kontrahent_with_too_long_kod_pocztowy_is_rejected():
    resp = post_document(INVOICE, document(Kontrahent={**CONTRACTOR_BLOCK, "KodPocztowy": "1" * 17}))
    assert_validation_rejected(resp, "Kontrahent.KodPocztowy")


def test_valid_kontrahent_block_creates_contractor_and_document():
    resp = post_document(INVOICE, document(Kontrahent=CONTRACTOR_BLOCK))
    assert resp["Kod"] == 0, resp
    assert len(store.all_documents()) == 1


def test_standard_valid_document_still_creates():
    resp = post_document(
        INVOICE,
        document(Kontrahent=CONTRACTOR_BLOCK, Pozycje=with_position(StawkaVat=0.23, TypStawkiVat="PRC")),
    )
    assert resp["Kod"] == 0, resp
    assert len(store.all_documents()) == 1
