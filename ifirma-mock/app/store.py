# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""Single owner of the mock's mutable state (documents, contractors, switches, call counters).

Every mutating function ends with ``_changed()`` while still holding the lock, which hands a
plain-dict snapshot to the listener registered through ``on_change`` (none by default, so the
mock stays memory-only until ``state.start`` registers the persistence listener).
"""
from __future__ import annotations

import collections
import dataclasses
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from .config import settings

_lock = threading.Lock()
_next_document_id = 1

#: Listener called under the lock with a snapshot after every mutation (None = memory only).
_listener: Callable[[dict[str, Any]], None] | None = None

#: The mock's clock, a seam so tests move time without sleeping.
_now: Callable[[], float] = time.time

#: Authenticated-call accounting (feeds abonent/limit.json and the rate-limit simulation).
#: The per-minute window and the busiest-minute count are kept in memory only and are not part
#: of the snapshot; the day window, the refused-call total and the runtime limits are.
_calls_total = 0
_calls_minute: int | None = None
_calls_minute_count = 0
_calls_day_key = ""
_calls_day = 0
_rejected_total = 0
_peak_minute = 0

#: Runtime limits set through /__mock/rate-limit; None means "use the environment value".
_limit_minute: int | None = None
_limit_day: int | None = None

SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 86400
RATE_WINDOWS = ("minute", "day")

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
    nazwa_szablonu: str = ""
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
    ksef_send_calls: int = 0

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
_next_contractor_seq = 1

#: Test-only switches (see /__mock/contractor-mode, /__mock/pdf-not-ready and
#: /__mock/legacy-mode). Guarded by _lock.
CONTRACTOR_CREATE_MODES = ("normal", "rename")
_contractor_create_mode = "normal"
_pdf_not_ready_remaining = 0
_legacy_mode = False


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


def _take_contractor_seq() -> int:
    """Caller must hold _lock."""
    global _next_contractor_seq
    value = _next_contractor_seq
    _next_contractor_seq += 1
    return value


def _new_contractor_identifier() -> str:
    """Caller must hold _lock."""
    while True:
        candidate = "K%06d" % _take_contractor_seq()
        if candidate not in _contractors:
            return candidate


def _renamed_contractor_identifier(sent: str) -> str:
    """Caller must hold _lock. "IFI" + digits of the sent identifier + 6-digit sequence."""
    digits = "".join(ch for ch in sent if ch.isdigit())
    while True:
        candidate = "IFI%s%06d" % (digits, _take_contractor_seq())
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
        _changed()
        return stored


def contractor_upsert(data: dict[str, Any]) -> dict[str, Any]:
    """Create or fully replace a contractor; returns the stored dict."""
    with _lock:
        identifier = contractor_identifier(data)
        if identifier is None:
            identifier = _new_contractor_identifier()
        stored = _normalize_contractor(data, identifier)
        _contractors[identifier] = stored
        _changed()
        return stored


def contractor_update(identifier: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Full replace of an existing contractor; the identifier is kept. None when unknown."""
    with _lock:
        if identifier not in _contractors:
            return None
        stored = _normalize_contractor(data, identifier)
        _contractors[identifier] = stored
        _changed()
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


def all_contractors() -> list[dict[str, Any]]:
    """The stored contractors in insertion order."""
    with _lock:
        return list(_contractors.values())


def contractors_reset() -> None:
    global _next_contractor_seq
    with _lock:
        _contractors.clear()
        _next_contractor_seq = 1
        _changed()


def set_contractor_create_mode(mode: str) -> None:
    global _contractor_create_mode
    if mode not in CONTRACTOR_CREATE_MODES:
        raise ValueError(f"unknown contractor create mode: {mode!r}")
    with _lock:
        _contractor_create_mode = mode
        _changed()


def contractor_create_mode() -> str:
    with _lock:
        return _contractor_create_mode


def set_pdf_not_ready(calls: int) -> None:
    """Make the next `calls` PDF downloads answer Kod 404 (replaces any pending count)."""
    global _pdf_not_ready_remaining
    with _lock:
        _pdf_not_ready_remaining = max(0, int(calls))
        _changed()


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
        _changed()
        return True


def set_legacy_mode(enabled: bool) -> None:
    global _legacy_mode
    with _lock:
        _legacy_mode = bool(enabled)
        _changed()


def legacy_mode() -> bool:
    with _lock:
        return _legacy_mode


def reset_test_switches() -> None:
    """Restore normal contractor creation, clear the PDF-not-ready counter, legacy mode, the mail reject switch and KSeF auto-send."""
    global _contractor_create_mode, _pdf_not_ready_remaining, _legacy_mode, _mail_reject, _ksef_auto
    with _lock:
        _contractor_create_mode = "normal"
        _pdf_not_ready_remaining = 0
        _legacy_mode = False
        _mail_reject = False
        _ksef_auto = "off"
        _changed()


#: Account-level KSeF behaviour: does iFirma submit (and KSeF accept) new invoices by itself?
KSEF_AUTO_MODES = ("off", "send", "accept")
_ksef_auto = "off"


def ksef_auto() -> str:
    with _lock:
        return _ksef_auto


def set_ksef_auto(mode: str) -> None:
    global _ksef_auto
    if mode not in KSEF_AUTO_MODES:
        raise ValueError(f"unknown KSeF auto-send mode {mode!r}")
    with _lock:
        _ksef_auto = mode
        _changed()


_documents: dict[int, Document] = {}


def create(doc_type: str, payload: dict[str, Any], **extra: Any) -> Document:
    """Create a document.

    `extra` may carry created_by, draft, currency, gross, linked_document,
    ksef_status and ksef_number.
    """
    global _next_document_id
    with _lock:
        new_id = _next_document_id
        _next_document_id += 1
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
            nazwa_szablonu=str(payload.get("NazwaSzablonu") or ""),
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
        _changed()
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
        _changed()
        return doc


def mark_deleted(ifirma_id: int) -> bool:
    with _lock:
        doc = _documents.get(ifirma_id)
        if doc is None:
            return False
        doc.deleted = True
        _changed()
        return True


def all_documents() -> list[Document]:
    return list(_documents.values())


def reset() -> None:
    global _next_document_id
    with _lock:
        _documents.clear()
        _next_document_id = 1
        _changed()


def set_ksef_status(doc_id: int, status: str | None, number: str | None = None) -> Document | None:
    """Set the KSeF status (and, when given, the KSeF number) of a document; None when unknown."""
    with _lock:
        doc = _documents.get(doc_id)
        if doc is None:
            return None
        doc.ksef_status = status
        if number is not None:
            doc.ksef_number = number
        _changed()
        return doc


def count_ksef_send(doc_id: int) -> Document | None:
    """Count one KSeF send call that named the document; None when it is unknown."""
    with _lock:
        doc = _documents.get(doc_id)
        if doc is None:
            return None
        doc.ksef_send_calls += 1
        _changed()
        return doc


def mark_sent(doc_id: int, recipient: str | None) -> Document | None:
    """Flag a document as e-mailed and remember the recipient; None when unknown."""
    with _lock:
        doc = _documents.get(doc_id)
        if doc is None:
            return None
        doc.sent = True
        if recipient:
            doc.sent_to.append(str(recipient))
        _changed()
        return doc


# --------------------------------------------------------------------------- #
# Call accounting                                                              #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CallVerdict:
    """Whether the simulated limits let a call through, and which window refused it."""

    allowed: bool
    window: str | None = None
    limit: int = 0
    retry_after: int = 0


def _day_key(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _seconds_left_in_minute(now: float) -> int:
    return max(1, math.ceil(SECONDS_PER_MINUTE - now % SECONDS_PER_MINUTE))


def _seconds_left_in_day(now: float) -> int:
    local = time.localtime(now)
    elapsed = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec + now % 1
    return max(1, math.ceil(SECONDS_PER_DAY - elapsed))


def limit_minute() -> int:
    """Effective per-minute limit (0 = off): the runtime value, else the environment's."""
    return settings.rate_limit if _limit_minute is None else _limit_minute


def limit_day() -> int:
    """Effective per-day limit (0 = off): the runtime value, else the environment's."""
    return settings.rate_limit_day if _limit_day is None else _limit_day


def _roll_windows_unlocked(now: float) -> None:
    global _calls_minute, _calls_minute_count, _calls_day_key, _calls_day
    minute = int(now // SECONDS_PER_MINUTE)
    if _calls_minute != minute:
        _calls_minute = minute
        _calls_minute_count = 0
    day_key = _day_key(now)
    if _calls_day_key != day_key:
        _calls_day_key = day_key
        _calls_day = 0


def _verdict_unlocked(now: float) -> CallVerdict:
    per_minute = limit_minute()
    if per_minute > 0 and _calls_minute_count > per_minute:
        return CallVerdict(False, "minute", per_minute, _seconds_left_in_minute(now))
    per_day = limit_day()
    if per_day > 0 and _calls_day > per_day:
        return CallVerdict(False, "day", per_day, _seconds_left_in_day(now))
    return CallVerdict(True)


def register_call() -> CallVerdict:
    """Count an authenticated call in the total, the minute and the day, then judge it.

    Refused calls count too, as they do at iFirma, so a client that keeps calling stays refused.
    """
    global _calls_total, _calls_minute_count, _calls_day, _rejected_total, _peak_minute
    now = _now()
    with _lock:
        _roll_windows_unlocked(now)
        _calls_total += 1
        _calls_minute_count += 1
        _calls_day += 1
        _peak_minute = max(_peak_minute, _calls_minute_count)
        verdict = _verdict_unlocked(now)
        if not verdict.allowed:
            _rejected_total += 1
        _changed()
        return verdict


def calls_total() -> int:
    with _lock:
        return _calls_total


def calls_this_minute() -> int:
    with _lock:
        if _calls_minute != int(_now() // SECONDS_PER_MINUTE):
            return 0
        return _calls_minute_count


def calls_today() -> int:
    with _lock:
        if _calls_day_key != _day_key(_now()):
            return 0
        return _calls_day


def rejected_total() -> int:
    with _lock:
        return _rejected_total


def peak_minute() -> int:
    with _lock:
        return _peak_minute


def set_rate_limits(per_minute: int | None = None, per_day: int | None = None) -> None:
    """Set either runtime limit (0 = off); a limit left as None keeps its value."""
    global _limit_minute, _limit_day
    with _lock:
        if per_minute is not None:
            _limit_minute = per_minute
        if per_day is not None:
            _limit_day = per_day
        _changed()


def next_window(window: str) -> None:
    """Clear the count of one window as if it had ended; the total and the other window stay."""
    global _calls_minute_count, _calls_day
    if window not in RATE_WINDOWS:
        raise ValueError(f"unknown window {window!r}")
    with _lock:
        _roll_windows_unlocked(_now())
        if window == "minute":
            _calls_minute_count = 0
        else:
            _calls_day = 0
        _changed()


# --------------------------------------------------------------------------- #
# Mailbox (SMTP sink). Memory only: never part of snapshot()/restore(), so none of these   #
# functions calls _changed().                                                          #
# --------------------------------------------------------------------------- #
MAIL_LIMIT = 200

_mail: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAIL_LIMIT)
_next_mail_id = 1
_mail_reject = False


def _clear_mail_unlocked() -> None:
    """Caller must hold _lock (or be the only user). Empties the mailbox and the reject switch."""
    global _next_mail_id, _mail_reject
    _mail.clear()
    _next_mail_id = 1
    _mail_reject = False


def mail_add(record: dict[str, Any]) -> int:
    """Store a received message (oldest dropped beyond MAIL_LIMIT); returns its new id."""
    global _next_mail_id
    with _lock:
        mail_id = _next_mail_id
        _next_mail_id += 1
        _mail.append({**record, "id": mail_id})
        return mail_id


def mail_all() -> list[dict[str, Any]]:
    """Stored messages, newest first."""
    with _lock:
        return list(reversed(_mail))


def mail_get(mail_id: int) -> dict[str, Any] | None:
    with _lock:
        for record in _mail:
            if record["id"] == mail_id:
                return record
    return None


def mail_clear() -> None:
    """Empty the mailbox; ids keep counting up."""
    with _lock:
        _mail.clear()


def set_mail_reject(enabled: bool) -> None:
    global _mail_reject
    with _lock:
        _mail_reject = bool(enabled)


def mail_reject() -> bool:
    with _lock:
        return _mail_reject


# --------------------------------------------------------------------------- #
# Whole-state operations: reset, change listener, snapshot, restore            #
# --------------------------------------------------------------------------- #
def _clear_unlocked(keep_mail: bool = False) -> None:
    """Empty everything; `restore()` passes keep_mail=True (the mailbox is not part of a snapshot)."""
    global _next_document_id, _next_contractor_seq, _calls_total, _calls_minute, _calls_minute_count
    global _contractor_create_mode, _pdf_not_ready_remaining, _legacy_mode, _ksef_auto
    global _calls_day_key, _calls_day, _rejected_total, _peak_minute, _limit_minute, _limit_day
    if not keep_mail:
        _clear_mail_unlocked()
    _documents.clear()
    _contractors.clear()
    _next_document_id = 1
    _next_contractor_seq = 1
    _contractor_create_mode = "normal"
    _pdf_not_ready_remaining = 0
    _legacy_mode = False
    _ksef_auto = "off"
    _calls_total = 0
    _calls_minute = None
    _calls_minute_count = 0
    _calls_day_key = ""
    _calls_day = 0
    _rejected_total = 0
    _peak_minute = 0
    _limit_minute = None
    _limit_day = None


def reset_all() -> None:
    """Clear documents, contractors, both sequences, the switches, the limits and the call counters."""
    with _lock:
        _clear_unlocked()
        _changed()


def on_change(callback: Callable[[dict[str, Any]], None] | None) -> None:
    """Register the listener called (under the lock, with a snapshot) after every mutation."""
    global _listener
    with _lock:
        _listener = callback


def _changed() -> None:
    """Caller must hold _lock."""
    if _listener is not None:
        _listener(_snapshot_unlocked())


def _snapshot_unlocked() -> dict[str, Any]:
    return {
        "next_document_id": _next_document_id,
        "next_contractor_seq": _next_contractor_seq,
        "documents": [dataclasses.asdict(d) for d in _documents.values()],
        "contractors": {k: dict(v) for k, v in _contractors.items()},
        "switches": {
            "contractor_create_mode": _contractor_create_mode,
            "pdf_not_ready_remaining": _pdf_not_ready_remaining,
            "legacy_mode": _legacy_mode,
            "ksef_auto": _ksef_auto,
        },
        "calls_total": _calls_total,
        "calls_day": {"day_key": _calls_day_key, "count": _calls_day},
        "rejected_total": _rejected_total,
        "limit_minute": _limit_minute,
        "limit_day": _limit_day,
    }


def snapshot() -> dict[str, Any]:
    """The whole mutable state as a plain, JSON-serialisable dict (per-minute window excluded)."""
    with _lock:
        return _snapshot_unlocked()


def restore(data: dict[str, Any]) -> None:
    """Replace the state with a snapshot.

    Missing document keys take the dataclass defaults and unknown ones are dropped, so an older
    or newer file still loads. A document without ``ifirma_id`` is skipped.
    """
    global _next_document_id, _next_contractor_seq, _calls_total
    global _contractor_create_mode, _pdf_not_ready_remaining, _legacy_mode, _ksef_auto
    global _calls_day_key, _calls_day, _rejected_total, _limit_minute, _limit_day
    known = {f.name for f in dataclasses.fields(Document)}
    documents: dict[int, Document] = {}
    for raw in data.get("documents") or []:
        kwargs = {k: v for k, v in raw.items() if k in known}
        if "ifirma_id" not in kwargs:
            continue
        kwargs["ifirma_id"] = int(kwargs["ifirma_id"])
        kwargs.setdefault("doc_type", "")
        kwargs.setdefault("number", "")
        kwargs.setdefault("payload", {})
        documents[kwargs["ifirma_id"]] = Document(**kwargs)
    contractors = {str(k): dict(v) for k, v in (data.get("contractors") or {}).items()}
    switches = data.get("switches") or {}
    mode = switches.get("contractor_create_mode", "normal")
    next_doc = max(int(data.get("next_document_id") or 1), max(documents, default=0) + 1)
    next_seq = max(int(data.get("next_contractor_seq") or 1), 1)
    pdf_remaining = max(0, int(switches.get("pdf_not_ready_remaining") or 0))
    saved_auto = switches.get("ksef_auto", "off")
    auto_mode = saved_auto if saved_auto in KSEF_AUTO_MODES else "off"
    calls = max(0, int(data.get("calls_total") or 0))
    day = data.get("calls_day") or {}
    day_key = str(day.get("day_key") or "")
    day_count = max(0, int(day.get("count") or 0)) if day_key == _day_key(_now()) else 0
    rejected = max(0, int(data.get("rejected_total") or 0))
    saved_minute = data.get("limit_minute")
    saved_day = data.get("limit_day")
    with _lock:
        _clear_unlocked(keep_mail=True)
        _documents.update(documents)
        _contractors.update(contractors)
        _next_document_id = next_doc
        _next_contractor_seq = next_seq
        _contractor_create_mode = mode if mode in CONTRACTOR_CREATE_MODES else "normal"
        _pdf_not_ready_remaining = pdf_remaining
        _legacy_mode = bool(switches.get("legacy_mode", False))
        _ksef_auto = auto_mode
        _calls_total = calls
        _calls_day_key = day_key if day_count else ""
        _calls_day = day_count
        _rejected_total = rejected
        _limit_minute = max(0, int(saved_minute)) if saved_minute is not None else None
        _limit_day = max(0, int(saved_day)) if saved_day is not None else None
        _changed()


def lock() -> threading.Lock:
    return _lock
