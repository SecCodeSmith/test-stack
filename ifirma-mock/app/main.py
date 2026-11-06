# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
iFirma mock API for testing the PrestaShop `ifirma` module end-to-end.

It implements the small subset of the iFirma API the module actually calls, with
the same authentication scheme, so the module's HMAC signing is exercised for real:

  POST /iapi/fakturaparagon.json            create receipt   (key: faktura)
  POST /iapi/fakturakraj.json               create invoice   (key: faktura)
  GET  /iapi/{doc_type}/{id}.pdf            download PDF     (key: faktura)
  POST /iapi/faktury/wplaty/{typ}/{id}.json register payment (key: faktura)
  GET  /iapi/faktury.json                   list documents   (key: faktura; filters + paging)
  GET  /iapi/{doc_type}/{id}.json           document detail  (key: faktura)
  POST /iapi/{doc_type}/send/{id}.json      e-mail document  (key: faktura)
  POST /iapi/{doc_type}/ksef/send/{id}.json send invoice to KSeF (key: faktura)
  GET  /iapi/abonent/limit.json             API usage limit  (key: abonent)
  GET  /iapi/kontrahenci/id/{id}.json       contractor by id (key: faktura)
  POST /iapi/kontrahenci.json               create contractor (key: faktura)
  PUT  /iapi/kontrahenci/{id}.json          replace contractor (key: faktura)
  GET  /iapi/kontrahenci/{phrase}.json      search contractors (key: faktura)

Test-only helpers (no HMAC, guarded by IFIRMA_MOCK_TEST_ENDPOINTS):
  POST /__mock/documents, DELETE /__mock/documents/{id}, POST /__mock/contractors,
  POST /__mock/ksef/accept/{id}, POST /__mock/ksef/reject/{id}, POST /__mock/reset,
  POST|GET /__mock/contractor-mode (create: normal|rename), POST|GET /__mock/pdf-not-ready,
  POST|GET /__mock/legacy-mode (enabled: true|false)

Docs: https://api.ifirma.pl/
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import store
from .auth import verify
from .config import settings
from .pdf import build_pdf

app = FastAPI(title="iFirma Mock API", version="1.0.0")


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _error(kod: int, info: str, http_status: int = 200) -> JSONResponse:
    # iFirma reports business errors with HTTP 200 and a non-zero Kod.
    return JSONResponse({"response": {"Kod": kod, "Informacja": info}}, status_code=http_status)


# Authenticated-call accounting (feeds abonent/limit.json and the rate-limit simulation).
_calls: dict[str, Any] = {"total": 0, "minute": None, "minute_count": 0}
LIMIT_GRANTED = 15000


def reset_counters() -> None:
    _calls.update(total=0, minute=None, minute_count=0)


def _register_call() -> bool:
    """Count an authenticated call; return False when the simulated rate limit is exceeded."""
    minute = int(time.time() // 60)
    if _calls["minute"] != minute:
        _calls["minute"] = minute
        _calls["minute_count"] = 0
    _calls["total"] += 1
    _calls["minute_count"] += 1
    limit = settings.rate_limit
    return not (limit > 0 and _calls["minute_count"] > limit)


async def _authorized(request: Request, key_name: str, raw_body: bytes) -> JSONResponse | None:
    """Return an error response when auth fails (in strict mode), else None."""
    # The module signs the URL exactly as sent (percent-encoded path, no query string). Verify
    # over the raw path first and accept the decoded form as a fallback.
    raw_path = (request.scope.get("raw_path") or b"").decode("latin-1") or request.url.path
    result = verify(request.headers.get("Authentication"), raw_path, key_name, raw_body)
    if not result.ok and raw_path != request.url.path:
        result = verify(request.headers.get("Authentication"), request.url.path, key_name, raw_body)
    if not result.ok:
        if settings.strict_auth:
            return _error(401, f"Błąd autoryzacji: {result.reason}", http_status=401)
        # Lenient mode: allow the request through but surface the reason in logs.
        print(f"[ifirma-mock] AUTH WARNING ({request.url.path}): {result.reason}")
    if not _register_call():
        return _error(100, "Przekroczono limit zapytań")
    return None


def _parse_body(raw_body: bytes) -> dict[str, Any] | None:
    try:
        return json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Meta                                                                         #
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ifirma-mock"}


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "iFirma Mock API",
        "version": app.version,
        "user": settings.user,
        "base_url": settings.base_url,
        "strict_auth": settings.strict_auth,
        "documents_issued": len(store.all_documents()),
    }


# --------------------------------------------------------------------------- #
# Create receipt / invoice                                                     #
# --------------------------------------------------------------------------- #
def _find_top_level_contractor(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Stored contractor named by the top-level IdentyfikatorKontrahenta or NIPKontrahenta."""
    identifier = payload.get("IdentyfikatorKontrahenta")
    if identifier is not None:
        text = str(identifier).strip()
        if text:
            found = store.contractor_get(text)
            if found is not None:
                return found
    nip = payload.get("NIPKontrahenta")
    if nip is not None:
        return store.contractor_find_by_nip(str(nip))
    return None


def _attach_contractor(payload: dict[str, Any], legacy: bool = False) -> JSONResponse | None:
    """Mirror iFirma's contractor lookup order for a document.

    1. Top-level IdentyfikatorKontrahenta naming a stored contractor: attach it.
       Unknown is not an error; continue with 2.
    2. Top-level NIPKontrahenta matching a stored contractor (digits only): attach it.
       Unknown is not an error; continue with 3.
    3. Nested Kontrahent block: a non-empty unknown Identyfikator is Kod 201 (nothing
       is created) unless legacy=True, in which case it creates the contractor from the
       block under that identifier; a known one is used as-is; a null/empty one creates
       the contractor from the block and the generated identifier is written back onto
       the document.
    """
    stored = _find_top_level_contractor(payload)
    if stored is not None:
        block = payload.get("Kontrahent")
        payload["Kontrahent"] = {**(block if isinstance(block, dict) else {}), **stored}
        return None

    block = payload.get("Kontrahent")
    if not isinstance(block, dict) or not block:
        return None
    identifier = store.contractor_identifier(block)
    if identifier is None:
        stored = store.contractor_create(block)
        payload["Kontrahent"] = {**block, "Identyfikator": stored["Identyfikator"]}
        return None
    if store.contractor_get(identifier) is None:
        if not legacy:
            return _error(201, "Nie znaleziono kontrahenta o zadanym identyfikatorze")
        stored = store.contractor_create(block)
        payload["Kontrahent"] = {**block, "Identyfikator": stored["Identyfikator"]}
    return None


#: Documented enumeration values (fakturakraj / fakturaparagon). Any other value that is
#: present in the request is rejected; the live API answers with a generic Kod 200 that
#: never names the field (observed 2026-11-02 for RodzajPodpisuOdbiorcy: BRK).
ENUM_RODZAJ_PODPISU_ODBIORCY = ("OUP", "UPO", "BPO", "BWO")
ENUM_LICZ_OD = ("NET", "BRT")
ENUM_FORMAT_DATY_SPRZEDAZY = ("DZN", "MSC")
ENUM_SPOSOB_ZAPLATY = (
    "GTK", "POB", "PRZ", "KAR", "PZA", "CZK", "KOM", "BAR", "DOT", "PAL", "ALG", "P24", "TPA", "ELE",
)
ENUM_TYP_STAWKI_VAT = ("PRC", "ZW")
#: (document field, allowed values) pairs checked on the document's top level.
DOCUMENT_ENUMS = (
    ("RodzajPodpisuOdbiorcy", ENUM_RODZAJ_PODPISU_ODBIORCY),
    ("LiczOd", ENUM_LICZ_OD),
    ("FormatDatySprzedazy", ENUM_FORMAT_DATY_SPRZEDAZY),
    ("SposobZaplaty", ENUM_SPOSOB_ZAPLATY),
)
#: (position field, allowed values) pairs checked on every entry of Pozycje.
POSITION_ENUMS = (("TypStawkiVat", ENUM_TYP_STAWKI_VAT),)
ENUM_ERROR_KOD = 200
ENUM_ERROR_INFO = "Niepoprawna zawartość żądania - nie można utworzyć obiektu na podstawie zawartości żądania."


def _has_invalid_enum(payload: dict[str, Any], legacy: bool = False) -> bool:
    """True when a present enumeration field holds a value outside its documented list.

    Absent keys and null values are not checked here. With legacy=True the
    RodzajPodpisuOdbiorcy pair is skipped (any value accepted); nothing else changes.
    """
    def invalid(source: dict[str, Any], enums: tuple) -> bool:
        for field, allowed in enums:
            value = source.get(field)
            if value is not None and value not in allowed:
                return True
        return False

    document_enums = DOCUMENT_ENUMS
    if legacy:
        document_enums = tuple(pair for pair in DOCUMENT_ENUMS if pair[0] != "RodzajPodpisuOdbiorcy")
    if invalid(payload, document_enums):
        return True
    positions = payload.get("Pozycje")
    if isinstance(positions, list):
        return any(isinstance(pos, dict) and invalid(pos, POSITION_ENUMS) for pos in positions)
    return False


#: Documented VAT rates for a position's StawkaVat (null is also allowed and not checked here).
ALLOWED_STAWKA_VAT = (0, 0.05, 0.08, 0.23)
_STAWKA_VAT_TOLERANCE = 1e-9


def _stawka_vat_allowed(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return any(abs(value - allowed) <= _STAWKA_VAT_TOLERANCE for allowed in ALLOWED_STAWKA_VAT)


def _document_validation_error(payload: dict[str, Any], legacy: bool = False) -> str | None:
    """First documented-rule violation in a document, as the Kod 201 message, or None.

    With legacy=True the StawkaVat check is skipped (any rate accepted); the PKWiU-for-ZW
    and Kontrahent required-field rules still apply.
    """
    positions = payload.get("Pozycje")
    if isinstance(positions, list):
        for i, pos in enumerate(positions):
            if not isinstance(pos, dict):
                continue
            stawka = pos.get("StawkaVat")
            if not legacy and stawka is not None and not _stawka_vat_allowed(stawka):
                return f"Pozycje[{i}].StawkaVat: niedozwolona stawka VAT"
            if pos.get("TypStawkiVat") == "ZW" and not str(pos.get("PKWiU") or "").strip():
                return f"Pozycje[{i}].PKWiU: pole wymagane dla stawki ZW"

    block = payload.get("Kontrahent")
    if isinstance(block, dict):
        missing = _contractor_errors(block)
        if missing:
            return f"Kontrahent.{missing[0]}: pole wymagane"
    return None


@app.post("/iapi/fakturaparagon.json")
@app.post("/iapi/fakturakraj.json")
async def create_document(request: Request) -> JSONResponse:
    raw = await request.body()

    auth_err = await _authorized(request, "faktura", raw)
    if auth_err is not None:
        return auth_err

    payload = _parse_body(raw)
    if payload is None:
        return _error(400, "Nieprawidłowy JSON w treści żądania.")

    # Legacy mode (test switch, off by default) relaxes three rules for the upgrade test from
    # 1.0.0 of the module, which sends RodzajPodpisuOdbiorcy BRK, a 4-decimal StawkaVat such as
    # 0.2301 and an e-mail as the contractor Identyfikator. Nothing else is relaxed.
    legacy = store.legacy_mode()
    if _has_invalid_enum(payload, legacy=legacy):
        return _error(ENUM_ERROR_KOD, ENUM_ERROR_INFO)

    validation_error = _document_validation_error(payload, legacy=legacy)
    if validation_error is not None:
        return _error(201, validation_error)

    positions = payload.get("Pozycje")
    if not positions:
        return _error(400, "Dokument musi zawierać co najmniej jedną pozycję (Pozycje).")

    contractor_err = _attach_contractor(payload, legacy=legacy)
    if contractor_err is not None:
        return contractor_err

    doc_type = "fakturaparagon" if request.url.path.endswith("fakturaparagon.json") else "fakturakraj"
    doc = store.create(doc_type, payload)

    # Faithful to the real API: the create response carries only the identifier
    # (as a STRING) and a message — NOT the printable number. The number is
    # assigned server-side and must be read back via GET /iapi/faktury.json.
    return JSONResponse(
        {
            "response": {
                "Kod": 0,
                "Informacja": "Faktura została pomyślnie dodana.",
                "Identyfikator": str(doc.ifirma_id),
            }
        }
    )


# --------------------------------------------------------------------------- #
# Register payment (status update)                                             #
# --------------------------------------------------------------------------- #
@app.post("/iapi/faktury/wplaty/{typ}/{doc_id}.json")
async def register_payment(typ: str, doc_id: str, request: Request) -> JSONResponse:
    raw = await request.body()

    auth_err = await _authorized(request, "faktura", raw)
    if auth_err is not None:
        return auth_err

    payload = _parse_body(raw)
    if payload is None or "Kwota" not in payload:
        return _error(400, "Brak wymaganego pola 'Kwota'.")

    try:
        ident = int(doc_id)
    except ValueError:
        return _error(404, f"Nie znaleziono dokumentu o identyfikatorze '{doc_id}'.")

    doc = store.add_payment(ident, float(payload["Kwota"]))
    if doc is None:
        return _error(404, f"Nie znaleziono dokumentu o identyfikatorze {ident}.")

    return JSONResponse({"response": {"Kod": 0, "Informacja": "Wpłata została zarejestrowana."}})


# --------------------------------------------------------------------------- #
# Download PDF                                                                 #
# --------------------------------------------------------------------------- #
@app.get("/iapi/{doc_type}/{doc_id}.pdf")
async def download_pdf(doc_type: str, doc_id: str, request: Request) -> Response:
    auth_err = await _authorized(request, "faktura", b"")
    if auth_err is not None:
        return auth_err
    if store.pdf_not_ready_consume():
        return _error(404, f"Nie znaleziono dokumentu {doc_id}.")

    try:
        ident = int(doc_id)
    except ValueError:
        return _error(404, f"Nie znaleziono dokumentu '{doc_id}'.")

    doc = store.get(ident)
    if doc is None:
        return _error(404, f"Nie znaleziono dokumentu {ident}.")

    duplicate = request.query_params.get("typ") == "dup"
    contractor = (doc.payload.get("Kontrahent") or {}).get("Nazwa", "")
    lines = [
        f"{'DUPLIKAT' if duplicate else 'ORYGINAL'}  {doc_type}",
        f"Numer: {doc.number}   (ID: {doc.ifirma_id})",
        f"Nabywca: {contractor}",
        f"Zaplacono: {doc.paid:.2f}   Status: {doc.status}",
        "",
        "Pozycje:",
    ]
    for pos in doc.payload.get("Pozycje", []):
        lines.append(
            f" - {pos.get('NazwaPelna', '')}  x{pos.get('Ilosc', 0)}  "
            f"{pos.get('CenaJednostkowa', 0)} (VAT {pos.get('StawkaVat', 0)})"
        )
    lines.append("")
    lines.append("*** DOKUMENT TESTOWY / MOCK ***")

    pdf_bytes = build_pdf("iFirma MOCK", lines)
    filename = f"ifirma-{doc.number.replace('/', '_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )




# --------------------------------------------------------------------------- #
# List                                                                         #
# --------------------------------------------------------------------------- #
VALID_STATUSES = {"oplacone", "nieoplacone", "oplaconeCzesciowo", "przeterminowane"}


def _row(d: store.Document) -> dict[str, Any]:
    # Exact key order of the official faktury.json row.
    return {
        "KontrahentNazwa": d.contractor_name,
        "IdentyfikatorKontrahenta": d.contractor_id,
        "NIPKontrahenta": d.contractor_nip,
        "DataWystawienia": d.issue_date,
        "PelnyNumer": d.number,
        "Brutto": d.gross,
        "FakturaId": d.ifirma_id,
        "Rodzaj": d.rodzaj,
        "Waluta": d.currency,
        "Zaplacono": round(d.paid, 2),
        "TerminPlatnosci": d.due_date,
        "CzyWyslano": d.sent,
        "CzySzkic": d.draft,
        "RodzajPowiazanegoPrzychodu": "prz_kor_faktura_kraj" if d.linked_document else None,
        "NumerPowiazanegoPrzychodu": d.linked_document,
        "KsefNumer": d.ksef_number,
        "KsefStatus": d.ksef_status,
    }


def _int_param(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except ValueError:
        return default


@app.get("/iapi/faktury.json")
async def list_documents(request: Request) -> JSONResponse:
    auth_err = await _authorized(request, "faktura", b"")
    if auth_err is not None:
        return auth_err

    q = request.query_params
    if not q.get("dataOd"):
        return _error(201, "Parametr dataOd jest wymagany.")
    try:
        date_from = date.fromisoformat(q["dataOd"])
        date_to = date.fromisoformat(q["dataDo"]) if q.get("dataDo") else None
    except ValueError:
        return _error(201, "Nieprawidłowy format daty (oczekiwano RRRR-MM-DD).")
    if date_to is None and settings.strict_list_window:
        date_to = date_from + timedelta(days=30)

    wanted_statuses: set[str] | None = None
    if q.get("status"):
        wanted_statuses = {x.strip() for x in q["status"].split(",") if x.strip()}
        if not wanted_statuses <= VALID_STATUSES:
            return _error(201, "Nieprawidłowa wartość parametru status.")

    try:
        amount_from = float(q["kwotaOd"]) if q.get("kwotaOd") else None
        amount_to = float(q["kwotaDo"]) if q.get("kwotaDo") else None
    except ValueError:
        return _error(201, "Nieprawidłowa kwota.")

    # Live API: a kontrahent identifier that is not in the account is an error, not an empty list.
    kontrahent = q.get("kontrahent", "").strip()
    if kontrahent and store.contractor_get(kontrahent) is None:
        return _error(201, "Nie znaleziono kontrahenta o zadanym identyfikatorze")

    nip_filter = "".join(ch for ch in q.get("nipKontrahenta", "") if ch.isdigit())

    matched = []
    for d in store.all_documents():
        if d.deleted:
            continue
        issued = store.parse_date(d.issue_date)
        if issued is None or issued < date_from or (date_to is not None and issued > date_to):
            continue
        if kontrahent and d.contractor_id != kontrahent:
            continue
        if nip_filter and "".join(c for c in d.contractor_nip if c.isdigit()) != nip_filter:
            continue
        if q.get("typ") and d.rodzaj != q["typ"]:
            continue
        if wanted_statuses is not None and not (d.statuses() & wanted_statuses):
            continue
        if amount_from is not None and d.gross < amount_from:
            continue
        if amount_to is not None and d.gross > amount_to:
            continue
        matched.append(d)

    page = max(_int_param(request, "strona", 1), 1)
    per_page = max(_int_param(request, "iloscNaStronie", 20), 1)
    window = matched[(page - 1) * per_page : page * per_page]

    # Faithful to the real API: rows live under "Wynik"; no total count is returned.
    return JSONResponse(
        {"response": {"Kod": 0, "Informacja": "OK", "Wynik": [_row(d) for d in window]}}
    )


# --------------------------------------------------------------------------- #
# Abonent limit (registered before the generic {doc_type}/{id}.json route)     #
# --------------------------------------------------------------------------- #
@app.get("/iapi/abonent/limit.json")
async def abonent_limit(request: Request) -> JSONResponse:
    auth_err = await _authorized(request, "abonent", b"")
    if auth_err is not None:
        return auth_err
    return JSONResponse(
        {
            "response": {
                "Kod": 0,
                "Informacja": "",
                "LimitWykorzystany": _calls["total"],
                "LimitPrzyznany": LIMIT_GRANTED,
            }
        }
    )


# --------------------------------------------------------------------------- #
# Contractors (registered before the generic {doc_type}/{id}.json route)       #
# --------------------------------------------------------------------------- #
#: (field, max length) pairs iFirma requires on create and replace.
CONTRACTOR_REQUIRED = (("Nazwa", 150), ("KodPocztowy", 16), ("Miejscowosc", 65))
CONTRACTOR_ID_MAX = 15


def _contractor_errors(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field, max_len in CONTRACTOR_REQUIRED:
        value = data.get(field)
        text = "" if value is None else str(value)
        if not text.strip() or len(text) > max_len:
            errors.append(field)
    return errors


@app.get("/iapi/kontrahenci/id/{identifier}.json")
async def contractor_by_id(identifier: str, request: Request) -> JSONResponse:
    auth_err = await _authorized(request, "faktura", b"")
    if auth_err is not None:
        return auth_err
    found = store.contractor_get(identifier)
    if found is None:
        return _error(208, "Nie znaleziono kontrahenta")
    return JSONResponse({"response": {"Kod": 0, "Informacja": "", "Wynik": [found]}})


@app.post("/iapi/kontrahenci.json")
async def contractor_create(request: Request) -> JSONResponse:
    raw = await request.body()
    auth_err = await _authorized(request, "faktura", raw)
    if auth_err is not None:
        return auth_err

    data = _parse_body(raw)
    if not isinstance(data, dict):
        return _error(400, "Nieprawidłowy JSON w treści żądania.")
    errors = _contractor_errors(data)
    if errors:
        return _error(201, "Validation errors: " + ", ".join(errors))
    identifier = store.contractor_identifier(data)
    if identifier is not None and len(identifier) > CONTRACTOR_ID_MAX:
        return _error(201, f"Identyfikator kontrahenta może mieć maksymalnie {CONTRACTOR_ID_MAX} znaków.")
    stored = store.contractor_create(data)
    if stored is None:
        return _error(201, "Kontrahent o podanym identyfikatorze już istnieje")
    # The answer echoes the SENT identifier; in "rename" mode the record is stored under a
    # generated one (live iFirma behaviour). Without a sent identifier, stored id is echoed.
    wynik = identifier if identifier is not None else stored["Identyfikator"]
    return JSONResponse(
        {"response": {"Kod": 0, "Informacja": "Dodano kontrahenta", "Wynik": wynik}}
    )


@app.put("/iapi/kontrahenci/{identifier}.json")
async def contractor_replace(identifier: str, request: Request) -> JSONResponse:
    raw = await request.body()
    auth_err = await _authorized(request, "faktura", raw)
    if auth_err is not None:
        return auth_err

    data = _parse_body(raw)
    if not isinstance(data, dict):
        return _error(400, "Nieprawidłowy JSON w treści żądania.")
    if store.contractor_get(identifier) is None:
        return _error(208, "Nie znaleziono kontrahenta")
    errors = _contractor_errors(data)
    if errors:
        return _error(201, "Validation errors: " + ", ".join(errors))
    stored = store.contractor_update(identifier, data)
    if stored is None:  # removed between the lookup and the update
        return _error(208, "Nie znaleziono kontrahenta")
    return JSONResponse(
        {"response": {"Kod": 0, "Informacja": "Zaktualizowano kontrahenta", "Wynik": identifier}}
    )


@app.get("/iapi/kontrahenci/{phrase}.json")
async def contractor_search_route(phrase: str, request: Request) -> JSONResponse:
    auth_err = await _authorized(request, "faktura", b"")
    if auth_err is not None:
        return auth_err
    matches = store.contractor_search(phrase)
    if not matches:
        return _error(208, "Brak wyników")
    return JSONResponse({"response": {"Kod": 0, "Informacja": "", "Wynik": matches}})


# --------------------------------------------------------------------------- #
# Document detail (JSON)                                                       #
# --------------------------------------------------------------------------- #
@app.get("/iapi/{doc_type}/{doc_id}.json")
async def document_detail(doc_type: str, doc_id: str, request: Request) -> JSONResponse:
    auth_err = await _authorized(request, "faktura", b"")
    if auth_err is not None:
        return auth_err

    doc = store.get_active(int(doc_id)) if doc_id.isdigit() else None
    if doc is None:
        return _error(404, f"Nie znaleziono dokumentu '{doc_id}'.")

    node: dict[str, Any] = {"Kod": 0, "Informacja": ""}
    node.update(doc.payload)
    node["Kod"] = 0
    node["Informacja"] = ""
    node["Identyfikator"] = str(doc.ifirma_id)
    node["PelnyNumer"] = doc.number
    node["Zaplacono"] = round(doc.paid, 2)
    node["Uwagi"] = doc.uwagi
    node["DodatkoweUwagi"] = doc.dodatkowe_uwagi
    return JSONResponse({"response": node})


# --------------------------------------------------------------------------- #
# Send invoice to KSeF                                                         #
# --------------------------------------------------------------------------- #
#: Document types accepted by the KSeF send endpoint (documented list).
KSEF_TYPES = frozenset({
    "fakturakraj", "fakturakraj/korekta", "fakturawysylka", "fakturazaliczka", "fakturakoncowa",
    "fakturazaliczkowawaluta", "fakturakoncowawaluta", "fakturaeksporttowarow", "fakturawdt",
    "fakturaeksportuslugue", "fakturaeksportuslug", "fakturawaluta", "fakturabudowa",
    "fakturaparagon", "fakturawyposazenie", "fakturasrodektrwaly", "fakturametodakasowa",
    "fakturaszczegolnyobowiazek", "fakturaoss",
})
#: KSeF statuses after which a second send is refused.
KSEF_SENT_STATES = frozenset({"WYSLANA", "PRZYJETA"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ksef_date_ok(value: Any) -> bool:
    """DataWysylki is null (send now) or a YYYY-MM-DD calendar date (scheduled)."""
    if value is None:
        return True
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _find_for_ksef(ident: str) -> store.Document | None:
    """Active document by digits id, or by number with `_` standing in for `/` (e.g. 314_2026)."""
    if ident.isascii() and ident.isdigit():
        return store.get_active(int(ident))
    number = ident.replace("_", "/")
    for doc in store.all_documents():
        if not doc.deleted and doc.number == number:
            return doc
    return None


@app.post("/iapi/{doc_type:path}/ksef/send/{ident}.json")
async def ksef_send(doc_type: str, ident: str, request: Request) -> JSONResponse:
    raw = await request.body()
    auth_err = await _authorized(request, "faktura", raw)
    if auth_err is not None:
        return auth_err

    if doc_type not in KSEF_TYPES:
        return _error(200, ENUM_ERROR_INFO)

    payload = _parse_body(raw)
    if not isinstance(payload, dict) or "DataWysylki" not in payload             or not _ksef_date_ok(payload["DataWysylki"]):
        return _error(201, "DataWysylki: nieprawidłowa wartość")

    doc = _find_for_ksef(ident)
    if doc is None:
        return _error(201, "Nie znaleziono faktury o zadanym identyfikatorze")

    scheduled = payload["DataWysylki"] is not None
    with store.lock():
        if doc.ksef_status in KSEF_SENT_STATES:
            return _error(201, "Faktura została już wysłana do KSeF")
        doc.ksef_status = "ZAPLANOWANA" if scheduled else "WYSLANA"
    return JSONResponse({"response": {"Kod": 0, "Informacja": "Faktura została wysłana do KSeF."}})


# --------------------------------------------------------------------------- #
# Send document by e-mail                                                      #
# --------------------------------------------------------------------------- #
SEND_TYPES = {
    "fakturakraj", "fakturawysylka", "fakturaproformakraj", "fakturaeksporttowarow",
    "fakturawdt", "fakturaeksportuslugue", "fakturaeksportuslug", "fakturawaluta",
}


@app.post("/iapi/{doc_type}/send/{doc_id}.json")
async def send_document(doc_type: str, doc_id: str, request: Request) -> JSONResponse:
    raw = await request.body()
    auth_err = await _authorized(request, "faktura", raw)
    if auth_err is not None:
        return auth_err

    if doc_type not in SEND_TYPES:
        return _error(200, "Operacja niedostępna dla tego typu dokumentu")

    payload = _parse_body(raw)
    if payload is None:
        return _error(200, "Nieprawidłowa struktura żądania.")

    doc = store.get_active(int(doc_id)) if doc_id.isdigit() else None
    if doc is None:
        return _error(404, f"Nie znaleziono dokumentu '{doc_id}'.")

    if len(str(payload.get("Tekst") or "")) > 1000:
        return _error(201, "Pole Tekst może mieć maksymalnie 1000 znaków.")

    recipient = payload.get("SkrzynkaEmailOdbiorcy") or doc.contractor_email
    with store.lock():
        doc.sent = True
        if recipient:
            doc.sent_to.append(str(recipient))
    return JSONResponse({"response": {"Kod": 0, "Informacja": "Wiadomość została wysłana."}})


# --------------------------------------------------------------------------- #
# Test-only helpers: simulate iFirma's own integrator (no HMAC)                #
# --------------------------------------------------------------------------- #
def _test_endpoints_disabled() -> JSONResponse | None:
    if not settings.test_endpoints:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return None


@app.post("/__mock/documents")
async def mock_seed_document(request: Request) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    body = _parse_body(await request.body())
    if body is None:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    contractor = dict(body.get("contractor") or {})
    seeded_id = store.contractor_identifier(contractor)
    if seeded_id is not None and store.contractor_get(seeded_id) is None:
        store.contractor_create(contractor)
    positions = body.get("Pozycje")
    if not positions:
        positions = [
            {
                "NazwaPelna": "Mock position",
                "Ilosc": 1,
                "CenaJednostkowa": float(body.get("gross") or 0),
                "StawkaVat": 0.23,
                "TypStawkiVat": "PRC",
            }
        ]
    payload: dict[str, Any] = {
        "Kontrahent": contractor,
        "Pozycje": positions,
        "Zaplacono": float(body.get("paid") or 0),
        "Uwagi": body.get("uwagi") or "",
        "DodatkoweUwagi": body.get("dodatkowe_uwagi"),
    }
    if body.get("issue_date"):
        payload["DataWystawienia"] = body["issue_date"]
    payload["DataSprzedazy"] = (
        body.get("sale_date") or body.get("issue_date") or date.today().isoformat()
    )
    payload["FormatDatySprzedazy"] = body.get("sale_date_format") or "DZN"
    if body.get("due_date"):
        payload["TerminPlatnosci"] = body["due_date"]
    if body.get("currency"):
        payload["Waluta"] = body["currency"]

    doc = store.create(
        body.get("doc_type") or "fakturaparagon",
        payload,
        created_by=body.get("created_by") or "external",
        draft=bool(body.get("draft", False)),
        currency=body.get("currency"),
        linked_document=body.get("linked_document"),
        ksef_status=body.get("ksef_status"),
        ksef_number=body.get("ksef_number"),
    )
    return JSONResponse(_row(doc))


@app.post("/__mock/contractors")
async def mock_seed_contractor(request: Request) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    body = _parse_body(await request.body())
    if not isinstance(body, dict):
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    return JSONResponse(store.contractor_upsert(body))


@app.delete("/__mock/documents/{doc_id}")
async def mock_delete_document(doc_id: str) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    if not (doc_id.isdigit() and store.mark_deleted(int(doc_id))):
        return JSONResponse({"detail": "Document not found"}, status_code=404)
    return JSONResponse({"deleted": int(doc_id)})


@app.post("/__mock/ksef/accept/{doc_id}")
async def mock_ksef_accept(doc_id: str) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    doc = store.get_active(int(doc_id)) if doc_id.isascii() and doc_id.isdigit() else None
    if doc is None:
        return JSONResponse({"detail": "Document not found"}, status_code=404)
    digits = "".join(ch for ch in str(doc.contractor_nip or "") if ch.isdigit())
    nip = digits if len(digits) == 10 else "0000000000"
    with store.lock():
        doc.ksef_status = "PRZYJETA"
        doc.ksef_number = (
            f"{nip}-{date.today():%Y%m%d}-{secrets.token_hex(6).upper()}-{secrets.token_hex(1).upper()}"
        )
    return JSONResponse(_row(doc))


@app.post("/__mock/ksef/reject/{doc_id}")
async def mock_ksef_reject(doc_id: str) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    doc = store.get_active(int(doc_id)) if doc_id.isascii() and doc_id.isdigit() else None
    if doc is None:
        return JSONResponse({"detail": "Document not found"}, status_code=404)
    with store.lock():
        doc.ksef_status = "ODRZUCONA"
    return JSONResponse(_row(doc))


@app.post("/__mock/reset")
async def mock_reset() -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    store.reset()
    store.contractors_reset()
    store.reset_test_switches()
    reset_counters()
    return JSONResponse({"reset": True})


@app.post("/__mock/contractor-mode")
async def mock_set_contractor_mode(request: Request) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    body = _parse_body(await request.body())
    mode = body.get("create") if isinstance(body, dict) else None
    if mode not in store.CONTRACTOR_CREATE_MODES:
        return JSONResponse({"detail": "create must be 'normal' or 'rename'"}, status_code=400)
    store.set_contractor_create_mode(mode)
    return JSONResponse({"create": mode})


@app.get("/__mock/contractor-mode")
async def mock_get_contractor_mode() -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    return JSONResponse({"create": store.contractor_create_mode()})


@app.post("/__mock/pdf-not-ready")
async def mock_set_pdf_not_ready(request: Request) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    body = _parse_body(await request.body())
    calls = body.get("calls") if isinstance(body, dict) else None
    if type(calls) is not int or calls < 0:
        return JSONResponse({"detail": "calls must be a non-negative integer"}, status_code=400)
    store.set_pdf_not_ready(calls)
    return JSONResponse({"remaining": store.pdf_not_ready_remaining()})


@app.get("/__mock/pdf-not-ready")
async def mock_get_pdf_not_ready() -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    return JSONResponse({"remaining": store.pdf_not_ready_remaining()})


@app.post("/__mock/legacy-mode")
async def mock_set_legacy_mode(request: Request) -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    body = _parse_body(await request.body())
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if type(enabled) is not bool:
        return JSONResponse({"detail": "enabled must be true or false"}, status_code=400)
    store.set_legacy_mode(enabled)
    return JSONResponse({"enabled": enabled})


@app.get("/__mock/legacy-mode")
async def mock_get_legacy_mode() -> JSONResponse:
    if (off := _test_endpoints_disabled()) is not None:
        return off
    return JSONResponse({"enabled": store.legacy_mode()})
