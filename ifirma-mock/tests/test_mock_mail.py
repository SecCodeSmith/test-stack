# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""Tests for the SMTP sink: store mailbox, listener, mailbox routes and the reject switch.

A module-scoped fixture runs the real listener on an ephemeral port (independent of the FastAPI
lifespan); messages are sent with ``smtplib`` exactly as a shop would.
"""
from __future__ import annotations

import email.policy
import hashlib
import hmac
import smtplib
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app import smtp, state, store
from app.config import settings
from app.main import app

client = TestClient(app)

ABONENT_KEY = "2" * 64
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 6 + b"\n%%EOF\n"

MAIL_ROUTES = [
    ("get", "/__mock/mail"),
    ("get", "/__mock/mail/1"),
    ("get", "/__mock/mail/1/raw"),
    ("get", "/__mock/mail/1/attachments/0"),
    ("delete", "/__mock/mail"),
    ("get", "/__mock/mail/reject"),
    ("post", "/__mock/mail/reject"),
]


def sign(path: str, key_name: str, body: str) -> str:
    signed_url = settings.base_url + path
    message = (signed_url + settings.user + key_name + body).encode("utf-8")
    digest = hmac.new(bytes.fromhex(settings.keys[key_name]), message, hashlib.sha1).hexdigest()
    return f"IAPIS user={settings.user}, hmac-sha1={digest}"


@pytest.fixture(scope="module")
def port():
    bound = smtp.start(0, host="127.0.0.1")
    yield bound
    smtp.stop()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, port):
    monkeypatch.setitem(settings.keys, "abonent", ABONENT_KEY)
    monkeypatch.setattr(settings, "rate_limit", 0)
    monkeypatch.setattr(settings, "test_endpoints", True)
    monkeypatch.setattr(settings, "state_file", "")
    monkeypatch.setattr(settings, "clean_start", False)
    state.start(settings)
    store.reset_all()
    yield
    monkeypatch.setattr(settings, "state_file", "")
    state.start(settings)
    store.reset_all()


def build(subject="Hello", to="customer@example.test", cc=None, with_pdf=False, text="Plain body",
          html="<p>Html body</p>", pdf_name="ifirma-1-2026.pdf") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Shop <shop@example.test>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if with_pdf:
        msg.add_attachment(PDF, maintype="application", subtype="pdf", filename=pdf_name)
    return msg


def send(port: int, msg: EmailMessage, rcpts: list[str] | None = None) -> None:
    rcpts = rcpts if rcpts is not None else ["customer@example.test"]
    with smtplib.SMTP("127.0.0.1", port, local_hostname="localhost") as conn:
        conn.sendmail("shop@example.test", rcpts, msg.as_bytes(policy=email.policy.SMTP))


def mail_list() -> list[dict]:
    resp = client.get("/__mock/mail")
    assert resp.status_code == 200
    return resp.json()["mail"]


# --------------------------------------------------------------------------- #
# Store: mailbox state (task 1.2)                                              #
# --------------------------------------------------------------------------- #
def record(n: int = 1) -> dict:
    return {"received_at": "t", "from": "a@x", "to": [], "cc": [], "bcc": [], "subject": f"s{n}",
            "attachments": [], "size": 1}


def test_ids_increase_and_newest_comes_first():
    first = store.mail_add(record(1))
    second = store.mail_add(record(2))
    assert second == first + 1
    assert [m["subject"] for m in store.mail_all()] == ["s2", "s1"]
    assert store.mail_get(first)["subject"] == "s1"
    assert store.mail_get(999) is None


def test_oldest_message_dropped():
    first = store.mail_add(record(0))
    for n in range(1, 201):
        store.mail_add(record(n))
    messages = store.mail_all()
    assert len(messages) == 200
    assert store.mail_get(first) is None
    assert messages[0]["subject"] == "s200" and messages[-1]["subject"] == "s1"


def test_oldest_message_dropped_over_http():
    for n in range(201):
        store.mail_add(record(n))
    listed = mail_list()
    assert len(listed) == 200
    assert 1 not in {m["id"] for m in listed}


def test_snapshot_has_no_mail_and_restore_leaves_the_mailbox_alone():
    store.mail_add(record())
    store.set_mail_reject(True)
    snap = store.snapshot()
    assert not any("mail" in key for key in snap)
    assert not any("mail" in key for key in snap["switches"])
    store.restore(snap)
    assert len(store.mail_all()) == 1
    assert store.mail_reject() is True


def test_mailbox_functions_do_not_notify_the_listener():
    calls = []
    store.on_change(lambda snap: calls.append(snap))
    try:
        store.mail_add(record())
        store.mail_all()
        store.mail_get(1)
        store.set_mail_reject(True)
        store.mail_reject()
        store.mail_clear()
    finally:
        store.on_change(None)
    assert calls == []


def test_reset_all_empties_the_mailbox_and_clears_the_switch():
    store.mail_add(record())
    store.set_mail_reject(True)
    store.reset_all()
    assert store.mail_all() == [] and store.mail_reject() is False


def test_reset_test_switches_clears_the_reject_switch_only():
    store.mail_add(record())
    store.set_mail_reject(True)
    store.reset_test_switches()
    assert store.mail_reject() is False and len(store.mail_all()) == 1


# --------------------------------------------------------------------------- #
# Requirement: SMTP listener accepts any message (task 1.3)                    #
# --------------------------------------------------------------------------- #
def test_plain_smtp_session(port):
    conn = smtplib.SMTP("127.0.0.1", port, local_hostname="localhost")
    assert conn.ehlo()[0] == 250
    assert conn.mail("sender@example.test")[0] == 250
    assert conn.rcpt("one@example.test")[0] == 250
    assert conn.rcpt("two@example.test")[0] == 250
    code, _ = conn.data(b"From: sender@example.test\r\nTo: one@example.test\r\nSubject: plain\r\n\r\nbody\r\n")
    assert code == 250
    conn.quit()
    stored = mail_list()
    assert len(stored) == 1
    detail = client.get(f"/__mock/mail/{stored[0]['id']}").json()
    assert detail["envelope"] == {"mail_from": "sender@example.test",
                                  "rcpt_tos": ["one@example.test", "two@example.test"]}


def test_no_credentials_needed(port):
    conn = smtplib.SMTP("127.0.0.1", port, local_hostname="localhost")
    conn.ehlo()
    assert not conn.has_extn("auth") and not conn.has_extn("starttls")
    conn.sendmail("shop@example.test", ["customer@example.test"], build().as_bytes(policy=email.policy.SMTP))
    conn.quit()
    assert len(mail_list()) == 1


def test_listener_disabled(monkeypatch):
    started = []
    monkeypatch.setattr(smtp, "start", lambda *a, **k: started.append(a))
    monkeypatch.setattr(settings, "smtp_port", 0)
    with TestClient(app) as web:
        assert web.get("/__mock/mail").json() == {"mail": []}
        assert web.get("/health").status_code == 200
    assert started == []


def test_lifespan_starts_and_stops_the_listener(monkeypatch):
    events = []
    monkeypatch.setattr(smtp, "start", lambda p, *a, **k: events.append(("start", p)) or p)
    monkeypatch.setattr(smtp, "stop", lambda: events.append(("stop",)))
    monkeypatch.setattr(settings, "smtp_port", 4321)
    with TestClient(app):
        assert events == [("start", 4321)]
    assert events == [("start", 4321), ("stop",)]


def test_start_while_running_replaces_the_listener(port):
    other = smtp.start(0, host="127.0.0.1")
    try:
        send(other, build())
        assert len(mail_list()) == 1
    finally:
        # restore the module's listener for the following tests
        smtp.stop()
        smtp.start(port, host="127.0.0.1")


# --------------------------------------------------------------------------- #
# Requirement: Received messages are recorded in full                          #
# --------------------------------------------------------------------------- #
def test_multipart_message_with_a_pdf_attachment(port):
    send(port, build(subject="Invoice 1/2026", with_pdf=True))
    stored = mail_list()
    assert len(stored) == 1
    detail = client.get(f"/__mock/mail/{stored[0]['id']}").json()
    assert detail["subject"] == "Invoice 1/2026"
    assert "Plain body" in detail["text"]
    assert "<p>Html body</p>" in detail["html"]
    assert detail["attachments"] == [{
        "filename": "ifirma-1-2026.pdf",
        "content_type": "application/pdf",
        "size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }]
    assert detail["from"] == "shop@example.test"


def test_blind_copy_derived_from_the_envelope(port):
    send(port, build(to="Customer@Example.test", cc="cc@example.test"),
         rcpts=["customer@example.test", "cc@example.test", "shop@example.test", "audit@example.test"])
    only = mail_list()[0]
    assert only["to"] == ["customer@example.test"]
    assert only["cc"] == ["cc@example.test"]
    assert only["bcc"] == ["shop@example.test", "audit@example.test"]


def test_blind_copy_with_one_extra_recipient(port):
    send(port, build(to="customer@example.test"), rcpts=["customer@example.test", "shop@example.test"])
    only = mail_list()[0]
    assert only["to"] == ["customer@example.test"] and only["bcc"] == ["shop@example.test"]


def test_encoded_subject(port):
    send(port, build(subject="Sklep Żółć: Faktura 1/2026"))
    assert mail_list()[0]["subject"] == "Sklep Żółć: Faktura 1/2026"
    raw = client.get(f"/__mock/mail/{mail_list()[0]['id']}/raw").content
    assert b"=?utf-8?" in raw.lower()


def test_text_only_message_has_empty_html(port):
    send(port, build(html=None))
    detail = client.get(f"/__mock/mail/{mail_list()[0]['id']}").json()
    assert "Plain body" in detail["text"] and detail["html"] == ""
    assert detail["attachments"] == []


def test_message_refused(port):
    assert client.post("/__mock/mail/reject", json={"enabled": True}).json() == {"enabled": True}
    with pytest.raises(smtplib.SMTPDataError) as caught:
        send(port, build())
    assert caught.value.smtp_code == 554
    assert mail_list() == []


def test_restart_empties_the_mailbox(monkeypatch, tmp_path, port):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.json"))
    with TestClient(app) as web:
        assert web.post("/__mock/documents", json={"doc_type": "fakturakraj", "gross": 12}).status_code == 200
        send(port, build())
        assert len(web.get("/__mock/mail").json()["mail"]) == 1
    with TestClient(app) as web:
        rows = web.get("/__mock/status.json").json()["documents"]
        assert len(rows) == 1
        assert web.get("/__mock/mail").json() == {"mail": []}


# --------------------------------------------------------------------------- #
# Requirement: Mailbox endpoints (task 2.1)                                    #
# --------------------------------------------------------------------------- #
def test_listing_after_two_messages(port):
    send(port, build(subject="first"))
    send(port, build(subject="second", with_pdf=True), rcpts=["customer@example.test", "shop@example.test"])
    listed = mail_list()
    assert [m["subject"] for m in listed] == ["second", "first"]
    assert listed[0]["id"] > listed[1]["id"]
    for entry in listed:
        assert set(entry) == {"id", "received_at", "from", "to", "cc", "bcc", "subject", "attachments", "size"}
    assert listed[0]["attachments"] == 1 and listed[1]["attachments"] == 0
    assert listed[0]["bcc"] == ["shop@example.test"]
    assert listed[0]["size"] > 0


def test_reading_one_message(port):
    send(port, build(with_pdf=True), rcpts=["customer@example.test", "shop@example.test"])
    mail_id = mail_list()[0]["id"]
    detail = client.get(f"/__mock/mail/{mail_id}").json()
    assert {"id", "received_at", "from", "to", "cc", "bcc", "subject", "size", "headers", "text", "html",
            "envelope", "attachments"} <= set(detail)
    assert not any(key.startswith("_") for key in detail)
    assert detail["envelope"]["mail_from"] == "shop@example.test"
    assert detail["envelope"]["rcpt_tos"] == ["customer@example.test", "shop@example.test"]
    assert detail["headers"]["Subject"] == "Hello" and "To" in detail["headers"]
    assert isinstance(detail["attachments"], list) and set(detail["attachments"][0]) == {
        "filename", "content_type", "size", "sha256"}


def test_raw_message(port):
    msg = build(with_pdf=True)
    send(port, msg)
    resp = client.get(f"/__mock/mail/{mail_list()[0]['id']}/raw")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("message/rfc822")
    assert b"Subject: Hello" in resp.content


def test_attachment_download(port):
    send(port, build(with_pdf=True))
    mail_id = mail_list()[0]["id"]
    resp = client.get(f"/__mock/mail/{mail_id}/attachments/0")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "ifirma-1-2026.pdf" in resp.headers["content-disposition"]
    assert resp.content == PDF
    listed = client.get(f"/__mock/mail/{mail_id}").json()["attachments"][0]
    assert hashlib.sha256(resp.content).hexdigest() == listed["sha256"]
    assert resp.content[:5] == b"%PDF-"


def test_unknown_message(port):
    assert client.get("/__mock/mail/999").status_code == 404
    assert client.get("/__mock/mail/999/raw").status_code == 404
    send(port, build(with_pdf=True))
    mail_id = mail_list()[0]["id"]
    assert client.get(f"/__mock/mail/{mail_id}/attachments/7").status_code == 404
    assert client.get("/__mock/mail/999/attachments/0").status_code == 404
    send(port, build())
    assert client.get(f"/__mock/mail/{mail_list()[0]['id']}/attachments/0").status_code == 404


def test_mailbox_emptied(port):
    send(port, build())
    old_id = mail_list()[0]["id"]
    assert client.delete("/__mock/mail").json() == {"cleared": True}
    assert mail_list() == []
    send(port, build())
    assert mail_list()[0]["id"] > old_id


def test_switch_read_back_and_cleared(port):
    assert client.get("/__mock/mail/reject").json() == {"enabled": False}
    assert client.post("/__mock/mail/reject", json={"enabled": True}).json() == {"enabled": True}
    assert client.get("/__mock/mail/reject").json() == {"enabled": True}
    assert client.post("/__mock/mail/reject", json={"enabled": False}).json() == {"enabled": False}
    assert client.get("/__mock/mail/reject").json() == {"enabled": False}
    send(port, build())
    assert len(mail_list()) == 1


@pytest.mark.parametrize("body", [{"enabled": "yes"}, {"enabled": 1}, {}, {"enabled": None}])
def test_non_boolean_switch_value(body):
    assert client.post("/__mock/mail/reject", json=body).status_code == 422
    assert client.get("/__mock/mail/reject").json() == {"enabled": False}


def test_reset_clears_the_mailbox_and_the_switch(port):
    send(port, build())
    client.post("/__mock/mail/reject", json={"enabled": True})
    assert client.post("/__mock/reset").json() == {"reset": True}
    assert mail_list() == []
    assert client.get("/__mock/mail/reject").json() == {"enabled": False}


def test_test_endpoints_disabled(monkeypatch, port):
    send(port, build())
    monkeypatch.setattr(settings, "test_endpoints", False)
    for method, path in MAIL_ROUTES:
        resp = getattr(client, method)(path, **({"json": {"enabled": True}} if method == "post" else {}))
        assert resp.status_code == 404, path
    send(port, build(subject="still stored"))
    assert [m["subject"] for m in store.mail_all()] == ["still stored", "Hello"]
    monkeypatch.setattr(settings, "test_endpoints", True)
    assert len(mail_list()) == 2


def test_mailbox_reads_are_not_api_traffic(port):
    send(port, build())
    path = "/iapi/abonent/limit.json"

    def used() -> int:
        resp = client.get(path, headers={"Authentication": sign(path, "abonent", "")})
        return resp.json()["response"]["LimitWykorzystany"]

    before = used()
    for _ in range(10):
        client.get("/__mock/mail")
    client.get("/__mock/mail/reject")
    client.post("/__mock/mail/reject", json={"enabled": False})
    after = used()
    assert after == before + 1  # only the second limit.json call itself counts
    assert store.calls_total() == 2


def test_root_answer_names_the_sink(monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", 1025)
    body = client.get("/").json()
    assert body["smtp_port"] == 1025 and body["mailbox"] == "/__mock/mail"
    assert {"service", "version", "user", "base_url", "strict_auth", "documents_issued",
            "status_page"} <= set(body)
    assert body["version"] == "1.2.0"


def test_inline_image_is_not_an_attachment(port):
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    msg = EmailMessage()
    msg["From"] = "shop@example.test"
    msg["To"] = "customer@example.test"
    msg["Subject"] = "With logo"
    msg.set_content("Plain body")
    msg.add_alternative('<p>Html <img src="cid:logo@shop"></p>', subtype="html")
    msg.get_payload()[1].add_related(png, "image", "png", cid="<logo@shop>", disposition="inline",
                                     filename="logo.png")
    msg.add_attachment(PDF, maintype="application", subtype="pdf", filename="ifirma-1-2026.pdf")
    send(port, msg)
    listed = mail_list()
    assert listed[0]["attachments"] == 1 and "inline" not in listed[0]
    mail_id = listed[0]["id"]
    detail = client.get(f"/__mock/mail/{mail_id}").json()
    assert [a["filename"] for a in detail["attachments"]] == ["ifirma-1-2026.pdf"]
    assert detail["inline"] == [{
        "filename": "logo.png", "content_type": "image/png", "content_id": "logo@shop",
        "size": len(png), "sha256": hashlib.sha256(png).hexdigest(),
    }]
    assert "Html" in detail["html"] and "Plain body" in detail["text"]
    resp = client.get(f"/__mock/mail/{mail_id}/inline/0")
    assert resp.status_code == 200 and resp.content == png
    assert resp.headers["content-type"].startswith("image/png")
    assert client.get(f"/__mock/mail/{mail_id}/inline/1").status_code == 404
    assert client.get("/__mock/mail/999/inline/0").status_code == 404
    assert client.get(f"/__mock/mail/{mail_id}/attachments/0").content == PDF
