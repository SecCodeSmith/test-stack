# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""In-memory store of documents 'issued' by the mock (reset on restart)."""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

_lock = threading.Lock()
_id_seq = itertools.count(1)

RODZAJ_MAP = {
    "fakturaparagon": "prz_faktura_paragon",
    "fakturakraj": "prz_faktura_kraj",
}


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def compute_gross(payload: dict[str, Any]) -> float:
    """Sum of CenaJednostkowa * Ilosc over Pozycje (not the paid amount)."""
    total = 0.0
    for pos in payload.get("Pozycje") or []:
        try:
            total += float(pos.get("CenaJednostkowa") or 0) * float(pos.get("Ilosc") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


@dataclass
class Document:
    ifirma_id: int
    doc_type: str
    number: str
    payload: dict[str, Any]
    paid: float = 0.0
    status: str = "created"
    payments: list[dict[str, Any]] = field(default_factory=list)
    created_by: str = "module"
    issue_date: str = ""
    due_date: str = ""
    uwagi: str = ""
    dodatkowe_uwagi: Any = None
    sent_to: list[str] = field(default_factory=list)
    sent: bool = False
    deleted: bool = False
    draft: bool = False
    currency: str = "PLN"
    gross: float = 0.0
    contractor_name: str = ""
    contractor_id: str = ""
    contractor_nip: str = ""
    contractor_email: str = ""
    linked_document: str | None = None
    ksef_status: str | None = None
    ksef_number: str | None = None

    @property
    def rodzaj(self) -> str:
        return RODZAJ_MAP.get(self.doc_type, "prz_" + self.doc_type)

    def statuses(self, today: date | None = None) -> set[str]:
        """Filter buckets (oplacone, oplaconeCzesciowo, nieoplacone, przeterminowane)."""
        today = today or date.today()
        out: set[str] = set()
        due = parse_date(self.due_date)
        if self.paid >= self.gross - 0.01:
            out.add("oplacone")
            return out
        if self.paid > 0:
            out.add("oplaconeCzesciowo")
        if due is not None and due < today:
            out.add("przeterminowane")
        elif self.paid <= 0:
            out.add("nieoplacone")
        return out


# --------------------------------------------------------------------------- #
# Contractors (kontrahenci)                                                    #
# --------------------------------------------------------------------------- #
#: Documented contractor object, in the order iFirma returns it.
CONTRACTOR_FIELDS = (
    "Nazwa", "Nazwa2", "Identyfikator", "PrefiksUE", "NIP", "Ulica", "KodPocztowy", "Kraj",
    "KodKraju", "Miejscowosc", "Email", "Telefon", "OsobaFizyczna", "ZgodaNaEfaktury",
    "DataUdzieleniaZgody", "EmailDlaFaktury", "JestDostawca", "JestOdbiorca", "AdresZagraniczny",
    "Skype", "Faks", "Uwagi", "Www", "NazwaBanku", "NumerKonta", "DrugiTelefon",
    "AdresKorespondencyjnyUlica", "AdresKorespondencyjnyKodPocztowy", "AdresKorespondencyjnyKraj",
    "AdresKorespondencyjnyMiejscowosc", "PodmiotPowiazany",
)
CONTRACTOR_BOOL_FIELDS = frozenset({
    "OsobaFizyczna", "ZgodaNaEfaktury", "JestDostawca", "JestOdbiorca",
    "AdresZagraniczny", "PodmiotPowiazany",
})
CONTRACTOR_SEARCH_LIMIT = 20

_contractors: dict[str, dict[str, Any]] = {}
_contractor_seq = itertools.count(1)

#: Test-only switches (see /__mock/contractor-mode and /__mock/pdf-not-ready). Guarded by _lock.
CONTRACTOR_CREATE_MODES = ("normal", "rename")
_contractor_create_mode = "normal"
_pdf_not_ready_remaining = 0


def contractor_identifier(data: dict[str, Any] | None) -> str | None:
    """The trimmed Identyfikator of a contractor block, or None when null/empty."""
    value = (data or {}).get("Identyfikator")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_contractor(data: dict[str, Any], identifier: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in CONTRACTOR_FIELDS:
        if key == "Identyfikator":
            out[key] = identifier
        elif key in CONTRACTOR_BOOL_FIELDS:
            out[key] = bool(data.get(key))
        else:
            out[key] = data.get(key)
    return out


def _new_contractor_identifier() -> str:
    """Caller must hold _lock."""
    while True:
        candidate = "K%06d" % next(_contractor_seq)
        if candidate not in _contractors:
            return candidate


def _renamed_contractor_identifier(sent: str) -> str:
    """Caller must hold _lock. "IFI" + digits of the sent identifier + 6-digit sequence."""
    digits = "".join(ch for ch in sent if ch.isdigit())
    while True:
        candidate = "IFI%s%06d" % (digits, next(_contractor_seq))
        if candidate not in _contractors:
            return candidate


def contractor_get(identifier: str) -> dict[str, Any] | None:
    with _lock:
        return _contractors.get(str(identifier))


def contractor_create(data: dict[str, Any]) -> dict[str, Any] | None:
    """Store a new contractor. Generates an identifier when null/empty.

    In "rename" mode a sent identifier is replaced by a generated one (IFI<digits><seq>)
    and the record is stored only under that generated identifier.

    Returns None when the identifier already exists (nothing is changed).
    """
    with _lock:
        identifier = contractor_identifier(data)
        if identifier is None:
            identifier = _new_contractor_identifier()
        elif _contractor_create_mode == "rename":
            identifier = _renamed_contractor_identifier(identifier)
        elif identifier in _contractors:
            return None
        stored = _normalize_contractor(data, identifier)
        _contractors[identifier] = stored
        return stored


def contractor_upsert(data: dict[str, Any]) -> dict[str, Any]:
    """Create or fully replace a contractor; returns the stored dict."""
    with _lock:
        identifier = contractor_identifier(data)
        if identifier is None:
            identifier = _new_contractor_identifier()
        stored = _normalize_contractor(data, identifier)
        _contractors[identifier] = stored
        return stored


def contractor_update(identifier: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Full replace of an existing contractor; the identifier is kept. None when unknown."""
    with _lock:
        if identifier not in _contractors:
            return None
        stored = _normalize_contractor(data, identifier)
        _contractors[identifier] = stored
        return stored


def contractor_find_by_nip(nip: str) -> dict[str, Any] | None:
    """First stored contractor whose NIP has the same digits as `nip` (None for empty input)."""
    digits = "".join(ch for ch in str(nip or "") if ch.isdigit())
    if not digits:
        return None
    with _lock:
        values = list(_contractors.values())
    for contractor in values:
        if "".join(ch for ch in str(contractor["NIP"] or "") if ch.isdigit()) == digits:
            return contractor
    return None


def contractor_search(phrase: str) -> list[dict[str, Any]]:
    """Substring match on Identyfikator, Nazwa or NIP (max CONTRACTOR_SEARCH_LIMIT)."""
    needle = phrase.strip().lower()
    digits = "".join(ch for ch in phrase if ch.isdigit())
    digits_only = bool(digits) and not any(ch.isalpha() for ch in phrase)
    with _lock:
        values = list(_contractors.values())
    out: list[dict[str, Any]] = []
    for contractor in values:
        hit = False
        if needle:
            haystacks = (contractor["Identyfikator"], contractor["Nazwa"], contractor["NIP"])
            hit = any(needle in str(h or "").lower() for h in haystacks)
        if not hit and digits_only:
            nip_digits = "".join(ch for ch in str(contractor["NIP"] or "") if ch.isdigit())
            hit = digits in nip_digits
        if hit:
            out.append(contractor)
            if len(out) >= CONTRACTOR_SEARCH_LIMIT:
                break
    return out


def contractors_reset() -> None:
    global _contractor_seq
    with _lock:
        _contractors.clear()
        _contractor_seq = itertools.count(1)


def set_contractor_create_mode(mode: str) -> None:
    global _contractor_create_mode
    if mode not in CONTRACTOR_CREATE_MODES:
        raise ValueError(f"unknown contractor create mode: {mode!r}")
    with _lock:
        _contractor_create_mode = mode


def contractor_create_mode() -> str:
    with _lock:
        return _contractor_create_mode


def set_pdf_not_ready(calls: int) -> None:
    """Make the next `calls` PDF downloads answer Kod 404 (replaces any pending count)."""
    global _pdf_not_ready_remaining
    with _lock:
        _pdf_not_ready_remaining = max(0, int(calls))


def pdf_not_ready_remaining() -> int:
    with _lock:
        return _pdf_not_ready_remaining


def pdf_not_ready_consume() -> bool:
    """True (and one count consumed) while the PDF-not-ready counter is above zero."""
    global _pdf_not_ready_remaining
    with _lock:
        if _pdf_not_ready_remaining <= 0:
            return False
        _pdf_not_ready_remaining -= 1
        return True


def reset_test_switches() -> None:
    """Restore normal contractor creation and clear the PDF-not-ready counter."""
    global _contractor_create_mode, _pdf_not_ready_remaining
    with _lock:
        _contractor_create_mode = "normal"
        _pdf_not_ready_remaining = 0


_documents: dict[int, Document] = {}


def create(doc_type: str, payload: dict[str, Any], **extra: Any) -> Document:
    """Create a document.

    `extra` may carry created_by, draft, currency, gross, linked_document,
    ksef_status and ksef_number.
    """
    with _lock:
        new_id = next(_id_seq)
        number = "%d/%d" % (new_id, date.today().year)
        contractor = payload.get("Kontrahent") or {}
        issue = str(payload.get("DataWystawienia") or date.today().isoformat())
        due = payload.get("TerminPlatnosci")
        if not due:
            base = parse_date(issue) or date.today()
            due = (base + timedelta(days=7)).isoformat()
        gross = extra.get("gross")
        doc = Document(
            ifirma_id=new_id,
            doc_type=doc_type,
            number=number,
            payload=payload,
            paid=float(payload.get("Zaplacono") or 0.0),
            created_by=extra.get("created_by") or "module",
            issue_date=issue,
            due_date=str(due),
            uwagi=str(payload.get("Uwagi") or ""),
            dodatkowe_uwagi=payload.get("DodatkoweUwagi"),
            draft=bool(extra.get("draft", False)),
            currency=str(extra.get("currency") or payload.get("Waluta") or "PLN"),
            gross=round(float(gross), 2) if gross is not None else compute_gross(payload),
            contractor_name=str(contractor.get("Nazwa", "")),
            contractor_id=str(contractor.get("Identyfikator", "")),
            contractor_nip=str(contractor.get("NIP", "")),
            contractor_email=str(contractor.get("Email", "")),
            linked_document=extra.get("linked_document"),
            ksef_status=extra.get("ksef_status"),
            ksef_number=extra.get("ksef_number"),
        )
        _documents[new_id] = doc
        return doc


def get(ifirma_id: int) -> Document | None:
    return _documents.get(ifirma_id)


def get_active(ifirma_id: int) -> Document | None:
    doc = _documents.get(ifirma_id)
    return None if doc is None or doc.deleted else doc


def add_payment(ifirma_id: int, amount: float) -> Document | None:
    with _lock:
        doc = _documents.get(ifirma_id)
        if doc is None:
            return None
        doc.payments.append({"Kwota": amount})
        doc.paid += amount
        doc.status = "paid"
        return doc


def mark_deleted(ifirma_id: int) -> bool:
    with _lock:
        doc = _documents.get(ifirma_id)
        if doc is None:
            return False
        doc.deleted = True
        return True


def all_documents() -> list[Document]:
    return list(_documents.values())


def reset() -> None:
    global _id_seq
    with _lock:
        _documents.clear()
        _id_seq = itertools.count(1)


def lock() -> threading.Lock:
    return _lock
