# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""SMTP sink: catches every e-mail a test shop sends and keeps it in the store's mailbox.

A dumb relay in reverse: any sender, any recipient, no ``AUTH``, no ``STARTTLS`` (no TLS context
is given to the controller, so the capability is never advertised). Messages are parsed with the
standard ``email`` package; the raw bytes and the attachment bytes stay in private (``_``-prefixed)
keys that the public JSON never carries.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import logging
import socket
import threading
from datetime import datetime, timezone
from typing import Any

from aiosmtpd.controller import Controller

from . import store

logger = logging.getLogger("ifirma-mock")

REJECT_REPLY = "554 5.7.1 Rejected by the ifirma-mock test switch"

#: Headers copied into the record's "headers" map.
_HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID", "Reply-To", "Content-Type")

_controller: Controller | None = None
_controller_lock = threading.Lock()


def _addresses(message: Any, name: str) -> list[str]:
    values = [str(v) for v in message.get_all(name, [])]
    return [addr.lower() for _, addr in email.utils.getaddresses(values) if addr]


def _text_of(part: Any) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def build_record(mail_from: str, rcpt_tos: list[str], raw: bytes) -> dict[str, Any]:
    """Parse one received message into the mailbox record (private keys start with ``_``)."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    to = _addresses(message, "To")
    cc = _addresses(message, "Cc")
    envelope = [str(r).lower() for r in rcpt_tos]
    known = set(to) | set(cc)
    bcc = [r for r in envelope if r not in known]

    text: str | None = None
    html: str | None = None
    attachments: list[dict[str, Any]] = []
    attachment_bytes: list[bytes] = []
    inline: list[dict[str, Any]] = []
    inline_bytes: list[bytes] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get("Content-Disposition")
        content_id = str(part.get("Content-ID") or "").strip().strip("<>")
        if part.is_attachment() or (filename and disposition is None and not content_id):
            data = part.get_payload(decode=True) or b""
            attachments.append({
                "filename": filename or "",
                "content_type": part.get_content_type(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            attachment_bytes.append(data)
        elif filename or content_id:
            # embedded image (shop logo): referenced as cid: from the HTML, not an attachment
            data = part.get_payload(decode=True) or b""
            inline.append({
                "filename": filename or "",
                "content_type": part.get_content_type(),
                "content_id": content_id,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            inline_bytes.append(data)
        elif part.get_content_type() == "text/plain" and text is None:
            text = _text_of(part)
        elif part.get_content_type() == "text/html" and html is None:
            html = _text_of(part)

    from_addresses = _addresses(message, "From")
    return {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "size": len(raw),
        "envelope": {"mail_from": mail_from, "rcpt_tos": list(rcpt_tos)},
        "from": from_addresses[0] if from_addresses else str(mail_from or "").lower(),
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": str(message.get("Subject", "") or ""),
        "date": str(message.get("Date", "") or ""),
        "headers": {name: str(message[name]) for name in _HEADERS if message[name] is not None},
        "text": text or "",
        "html": html or "",
        "attachments": attachments,
        "inline": inline,
        "_raw": raw,
        "_attachment_bytes": attachment_bytes,
        "_inline_bytes": inline_bytes,
    }


def public(record: dict[str, Any]) -> dict[str, Any]:
    """The record without its private (``_``-prefixed) keys."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def listing(record: dict[str, Any]) -> dict[str, Any]:
    """The compact form used by the mailbox listing and the status page (no bodies, no headers)."""
    return {
        "id": record["id"],
        "received_at": record["received_at"],
        "from": record["from"],
        "to": record["to"],
        "cc": record["cc"],
        "bcc": record["bcc"],
        "subject": record["subject"],
        "attachments": len(record["attachments"]),
        "size": record["size"],
    }


class MailboxHandler:
    """aiosmtpd handler: accept every recipient, store every message unless the switch is on."""

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        if store.mail_reject():
            return REJECT_REPLY
        try:
            record = build_record(envelope.mail_from, list(envelope.rcpt_tos), envelope.content)
        except Exception as exc:  # noqa: BLE001 - a sink must never crash on odd input
            logger.error("SMTP sink could not parse a message: %s", exc)
            return "451 4.3.0 Could not process the message"
        store.mail_add(record)
        return "250 OK"


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def start(port: int, host: str = "0.0.0.0") -> int:
    """Start the listener (stopping a running one first); returns the bound port."""
    global _controller
    with _controller_lock:
        if _controller is not None:
            _stop_unlocked()
        # aiosmtpd probes the listener on `self.port` after starting, so port 0 cannot be left
        # to the OS: pick a free port first (tiny race window, fine for tests).
        bound = port or _free_port(host)
        controller = Controller(MailboxHandler(), hostname=host, port=bound, server_hostname="ifirma-mock")
        controller.start()
        _controller = controller
        logger.info("SMTP sink listening on %s:%s", host, bound)
        return bound


def _stop_unlocked() -> None:
    global _controller
    if _controller is not None:
        try:
            _controller.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SMTP sink did not stop cleanly: %s", exc)
        _controller = None


def stop() -> None:
    """Stop the listener; safe when none is running."""
    with _controller_lock:
        _stop_unlocked()


def running() -> bool:
    return _controller is not None
