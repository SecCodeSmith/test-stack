# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""In-memory store of documents 'issued' by the mock (reset on restart)."""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

_lock = threading.Lock()
_id_seq = itertools.count(1)


@dataclass
class Document:
    ifirma_id: int
    doc_type: str
    number: str
    payload: dict[str, Any]
    paid: float = 0.0
    status: str = "created"
    payments: list[dict[str, Any]] = field(default_factory=list)


_documents: dict[int, Document] = {}


def create(doc_type: str, payload: dict[str, Any]) -> Document:
    with _lock:
        new_id = next(_id_seq)
        number = "%d/%d" % (new_id, date.today().year)
        doc = Document(
            ifirma_id=new_id,
            doc_type=doc_type,
            number=number,
            payload=payload,
            paid=float(payload.get("Zaplacono") or 0.0),
        )
        _documents[new_id] = doc
        return doc


def get(ifirma_id: int) -> Document | None:
    return _documents.get(ifirma_id)


def add_payment(ifirma_id: int, amount: float) -> Document | None:
    with _lock:
        doc = _documents.get(ifirma_id)
        if doc is None:
            return None
        doc.payments.append({"Kwota": amount})
        doc.paid += amount
        doc.status = "paid"
        return doc


def all_documents() -> list[Document]:
    return list(_documents.values())
