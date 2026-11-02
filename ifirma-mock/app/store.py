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
