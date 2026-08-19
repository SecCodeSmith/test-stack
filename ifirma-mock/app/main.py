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
  GET  /iapi/faktury.json                   list documents   (key: faktura)

Docs: https://api.ifirma.pl/
"""
from __future__ import annotations

import json
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


async def _authorized(request: Request, key_name: str, raw_body: bytes) -> JSONResponse | None:
    """Return an error response when auth fails (in strict mode), else None."""
    result = verify(request.headers.get("Authentication"), request.url.path, key_name, raw_body)
    if not result.ok:
        if settings.strict_auth:
            return _error(401, f"Błąd autoryzacji: {result.reason}", http_status=401)
        # Lenient mode: allow the request through but surface the reason in logs.
        print(f"[ifirma-mock] AUTH WARNING ({request.url.path}): {result.reason}")
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

    positions = payload.get("Pozycje")
    if not positions:
        return _error(400, "Dokument musi zawierać co najmniej jedną pozycję (Pozycje).")

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
@app.get("/iapi/faktury.json")
async def list_documents(request: Request) -> JSONResponse:
    auth_err = await _authorized(request, "faktura", b"")
    if auth_err is not None:
        return auth_err

    # Faithful to the real API: rows live under "Wynik" with these field names.
    wynik = []
    for d in store.all_documents():
        kontrahent = d.payload.get("Kontrahent") or {}
        wynik.append(
            {
                "FakturaId": d.ifirma_id,
                "PelnyNumer": d.number,
                "Rodzaj": d.doc_type,
                "KontrahentNazwa": kontrahent.get("Nazwa", ""),
                "IdentyfikatorKontrahenta": kontrahent.get("Identyfikator", ""),
                "NIPKontrahenta": kontrahent.get("NIP", ""),
                "DataWystawienia": d.payload.get("DataWystawienia", ""),
                "TerminPlatnosci": d.payload.get("TerminPlatnosci", ""),
                "Brutto": round(d.paid, 2),
                "Zaplacono": round(d.paid, 2),
                "Waluta": "PLN",
                "CzyWyslano": False,
            }
        )
    return JSONResponse(
        {"response": {"Kod": 0, "Informacja": "OK", "Wynik": wynik}}
    )
