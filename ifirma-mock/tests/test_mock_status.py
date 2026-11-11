# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""Tests for the mock status page (``/__mock/status``), its JSON twin and its form routes."""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import state, store
from app.config import settings
from app.main import app

client = TestClient(app)

ABONENT_KEY = "2" * 64


def sign(path: str, key_name: str, body: str) -> str:
    signed_url = settings.base_url + path
    message = (signed_url + settings.user + key_name + body).encode("utf-8")
    digest = hmac.new(bytes.fromhex(settings.keys[key_name]), message, hashlib.sha1).hexdigest()
    return f"IAPIS user={settings.user}, hmac-sha1={digest}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setitem(settings.keys, "abonent", ABONENT_KEY)
    monkeypatch.setattr(settings, "rate_limit", 0)
    monkeypatch.setattr(settings, "test_endpoints", True)
    monkeypatch.setattr(settings, "state_file", "")
    monkeypatch.setattr(settings, "clean_start", False)
    state.start(settings)
    client.post("/__mock/reset")
    yield
    monkeypatch.setattr(settings, "state_file", "")
    state.start(settings)
    client.post("/__mock/reset")


def post(path: str, **fields) -> object:
    return client.post(path, data=fields, follow_redirects=False)


def redirect(resp) -> dict[str, str]:
    """Assert a 303 to the status page and return its decoded query parameters."""
    assert resp.status_code == 303, resp.text
    url = urlparse(resp.headers["location"])
    assert url.path == "/__mock/status"
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def page() -> str:
    resp = client.get("/__mock/status")
    assert resp.status_code == 200
    return resp.text


def seed(**overrides) -> dict:
    fields = {"doc_type": "fakturaparagon", "name": "Jan", "identifier": "", "nip": "",
              "email": "", "gross": "100", "paid": "0"}
    fields.update(overrides)
    assert "msg" in redirect(post("/__mock/ui/seed", **fields))
    return store.all_documents()[-1]


def listing() -> list[dict]:
    path = "/iapi/faktury.json"
    resp = client.get(path + "?dataOd=2020-01-01", headers={"Authentication": sign(path, "faktura", "")})
    return resp.json()["response"]["Wynik"]


# ------------------------------- availability -------------------------------- #
def test_page_available():
    resp = client.get("/__mock/status")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert html.lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in html and "<title>" in html
    for link in ("/docs", "/health", "/__mock/status.json", "https://github.com/SecCodeSmith/test-stack#readme"):
        assert f'href="{link}"' in html
    for text in ("Settings", "Persistence", "Counters", "Switches", "Seed", "Documents", "Contractors", "Reset"):
        assert f"<h2>{text}</h2>" in html
    assert "No documents stored." in html and "No contractors stored." in html


def test_flash_messages_are_shown_escaped():
    html = client.get("/__mock/status", params={"msg": "done <b>", "error": "bad <i>"}).text
    assert "done &lt;b&gt;" in html and "bad &lt;i&gt;" in html
    assert "<b>" not in html and "<i>" not in html


def test_test_endpoints_disabled(monkeypatch):
    seed()
    monkeypatch.setattr(settings, "test_endpoints", False)
    assert client.get("/__mock/status").status_code == 404
    assert client.get("/__mock/status.json").status_code == 404
    routes = [
        "/__mock/ui/seed",
        "/__mock/ui/switch/legacy-mode",
        "/__mock/ui/switch/pdf-not-ready",
        "/__mock/ui/switch/contractor-mode",
        "/__mock/ui/documents/1/ksef-accept",
        "/__mock/ui/documents/1/ksef-reject",
        "/__mock/ui/documents/1/delete",
        "/__mock/ui/reset",
    ]
    for route in routes:
        resp = post(route, value="true", gross="1")
        assert resp.status_code == 404, route
    monkeypatch.setattr(settings, "test_endpoints", True)
    assert len(store.all_documents()) == 1 and not store.all_documents()[0].deleted
    assert store.legacy_mode() is False


def test_root_answer_points_at_the_page():
    assert client.get("/").json()["status_page"] == "/__mock/status"


# -------------------------------- persistence -------------------------------- #
def test_memory_only_mock():
    assert "memory only" in page()
    persistence = client.get("/__mock/status.json").json()["persistence"]
    assert persistence["mode"] == "memory"


def test_persistent_mock_after_a_change(monkeypatch, tmp_path):
    path = tmp_path / "mock-state.json"
    monkeypatch.setattr(settings, "state_file", str(path))
    with TestClient(app) as live:
        live.post("/__mock/reset")
        assert redirect(live.post("/__mock/ui/seed", data={"gross": "10", "name": "X"},
                                  follow_redirects=False))
        html = live.get("/__mock/status").text
        assert str(path) in html
        assert "memory only" not in html
        info = live.get("/__mock/status.json").json()["persistence"]
        assert info["mode"] == "file" and info["path"] == str(path)
        assert info["last_write"]
        assert info["last_write"] in html


def test_fresh_mock():
    data = client.get("/__mock/status.json").json()
    assert data["documents"] == [] and data["contractors"] == []
    assert data["counters"]["calls_total"] == 0
    html = page()
    assert "No documents stored." in html and "No contractors stored." in html


# ------------------------------ documents table ------------------------------ #
def test_documents_listed_newest_first():
    for name in ("First", "Second", "Third"):
        seed(name=name)
    assert redirect(post("/__mock/ui/documents/2/delete"))["msg"]
    data = client.get("/__mock/status.json").json()
    assert [d["FakturaId"] for d in data["documents"]] == [3, 2, 1]
    assert [d["deleted"] for d in data["documents"]] == [False, True, False]
    html = page()
    assert html.index("Third") < html.index("Second") < html.index("First")
    assert html.count('class="marker">deleted<') == 1


def test_values_are_escaped():
    payload = "<script>alert(1)</script>"
    seed(name=payload, identifier="X<1>", email=payload)
    html = page()
    assert payload not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script" not in html.lower()


# --------------------------------- switches ---------------------------------- #
def test_legacy_mode_switched_on_from_the_page():
    assert redirect(post("/__mock/ui/switch/legacy-mode", value="true"))["msg"]
    assert store.legacy_mode() is True
    assert client.get("/__mock/legacy-mode").json() == {"enabled": True}
    assert client.get("/__mock/status.json").json()["switches"]["legacy_mode"] is True


def test_pdf_not_ready_set_from_the_page():
    doc = seed()
    assert redirect(post("/__mock/ui/switch/pdf-not-ready", value="2"))["msg"]
    assert client.get("/__mock/status.json").json()["switches"]["pdf_not_ready_remaining"] == 2
    path = f"/iapi/fakturaparagon/{doc.ifirma_id}.pdf"
    for _ in range(2):
        resp = client.get(path, headers={"Authentication": sign(path, "faktura", "")})
        assert resp.json()["response"]["Kod"] == 404
    resp = client.get(path, headers={"Authentication": sign(path, "faktura", "")})
    assert resp.content.startswith(b"%PDF")


def test_contractor_mode_switch_and_invalid_values():
    assert redirect(post("/__mock/ui/switch/contractor-mode", value="rename"))["msg"]
    assert store.contractor_create_mode() == "rename"
    assert "error" in redirect(post("/__mock/ui/switch/contractor-mode", value="bogus"))
    assert "error" in redirect(post("/__mock/ui/switch/pdf-not-ready", value="-1"))
    assert "error" in redirect(post("/__mock/ui/switch/legacy-mode", value="maybe"))
    assert "error" in redirect(post("/__mock/ui/switch/unknown", value="x"))
    assert store.contractor_create_mode() == "rename"
    assert store.pdf_not_ready_remaining() == 0 and store.legacy_mode() is False


# ---------------------------------- seeding ---------------------------------- #
def test_sample_receipt_seeded():
    result = redirect(post("/__mock/ui/seed", doc_type="fakturaparagon", name="Jan Kowalski",
                           identifier="", nip="", email="", gross="123.45", paid=""))
    assert "msg" in result
    doc = store.all_documents()[-1]
    assert doc.created_by == "external"
    top = client.get("/__mock/status.json").json()["documents"][0]
    assert top["PelnyNumer"] == f"{doc.ifirma_id}/{doc.issue_date[:4]}"
    assert top["Brutto"] == 123.45 and top["Zaplacono"] == 0.0
    assert top["statuses"] == ["nieoplacone"]
    html = page()
    assert top["PelnyNumer"] in html and "123.45" in html and "0.00" in html
    assert any(row["FakturaId"] == doc.ifirma_id and row["Brutto"] == 123.45 for row in listing())


def test_seed_with_contractor_stores_it():
    seed(name="ACME", identifier="ACME1", nip="1234567890", email="a@example.com")
    assert [c["Identyfikator"] for c in store.all_contractors()] == ["ACME1"]
    assert "ACME1" in page()


@pytest.mark.parametrize("fields,named", [
    ({"gross": "abc"}, "gross"),
    ({"gross": ""}, "gross"),
    ({"gross": "10", "paid": "x"}, "paid"),
])
def test_invalid_amount(fields, named):
    base = {"doc_type": "fakturaparagon", "name": "Jan", "paid": ""}
    base.update(fields)
    result = redirect(post("/__mock/ui/seed", **base))
    assert named in result["error"]
    assert store.all_documents() == []
    assert named in client.get("/__mock/status", params=result).text


# ---------------------------------- KSeF ------------------------------------- #
def test_ksef_accepted_from_the_page():
    doc = seed(doc_type="fakturakraj", nip="1234567890")
    assert redirect(post(f"/__mock/ui/documents/{doc.ifirma_id}/ksef-accept"))["msg"]
    assert "PRZYJETA" in page()
    row = next(r for r in listing() if r["FakturaId"] == doc.ifirma_id)
    assert row["KsefStatus"] == "PRZYJETA" and row["KsefNumer"]
    assert row["KsefNumer"] in page()
    assert redirect(post(f"/__mock/ui/documents/{doc.ifirma_id}/ksef-reject"))["msg"]
    assert store.get(doc.ifirma_id).ksef_status == "ODRZUCONA"


def test_document_deleted_from_the_page():
    doc = seed()
    assert redirect(post(f"/__mock/ui/documents/{doc.ifirma_id}/delete"))["msg"]
    assert store.get(doc.ifirma_id).deleted is True
    assert '>deleted<' in page()
    assert all(r["FakturaId"] != doc.ifirma_id for r in listing())


def test_unknown_document_id_redirects_with_error():
    for action in ("delete", "ksef-accept", "ksef-reject"):
        assert "error" in redirect(post(f"/__mock/ui/documents/999/{action}"))
        assert "error" in redirect(post(f"/__mock/ui/documents/abc/{action}"))
    assert "error" in redirect(post("/__mock/ui/documents/1/explode"))


def test_reset_clicked():
    seed()
    post("/__mock/ui/switch/legacy-mode", value="true")
    assert redirect(post("/__mock/ui/reset"))["msg"]
    data = client.get("/__mock/status.json").json()
    assert data["documents"] == [] and data["switches"]["legacy_mode"] is False


# --------------------------------- JSON twin --------------------------------- #
def test_script_reads_the_state():
    seed()
    data = client.get("/__mock/status.json").json()
    # Deliberate change (add-rate-limit-queue-and-ksef-confirmed-delivery): the twin also carries the
    # day limit, the new counters, the `rate_limit` object and the KSeF auto-send switch.
    assert set(data) == {"service", "version", "settings", "persistence", "counters", "switches",
                         "documents", "contractors", "mail", "rate_limit"}
    assert data["version"] == app.version
    assert set(data["settings"]) == {"user", "base_url", "strict_auth", "rate_limit", "rate_limit_day",
                                     "strict_list_window", "test_endpoints", "smtp_port"}
    assert set(data["counters"]) == {"calls_total", "calls_this_minute", "calls_today", "rejected_total",
                                     "peak_minute", "limit_granted"}
    assert set(data["rate_limit"]) == {"per_minute", "per_day", "defaults", "calls_this_minute",
                                       "calls_today", "calls_total", "rejected_total", "peak_minute"}
    assert set(data["switches"]) == {"contractor_mode", "pdf_not_ready_remaining", "legacy_mode",
                                    "mail_reject", "ksef_auto"}
    assert set(data["persistence"]) == {"mode", "path", "last_write", "clean_start", "loaded_from_file"}
    doc = data["documents"][0]
    assert {"deleted", "created_by", "statuses", "KsefStatus", "PelnyNumer"} <= set(doc)
    assert doc["statuses"] == sorted(doc["statuses"])


def test_watching_the_page_while_testing(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 5)
    path = "/iapi/abonent/limit.json"

    def limit_used() -> int:
        resp = client.get(path, headers={"Authentication": sign(path, "abonent", "")})
        return resp.json()["response"]["LimitWykorzystany"]

    assert limit_used() == 1
    for _ in range(5):
        client.get("/__mock/status")
        client.get("/__mock/status.json")
    resp = client.get(path, headers={"Authentication": sign(path, "abonent", "")})
    assert resp.json()["response"]["Kod"] == 0
    assert resp.json()["response"]["LimitWykorzystany"] == 2
    assert store.calls_total() == 2


# --------------------------------------------------------------------------- #
# Requirement: Page shows the mailbox (SMTP sink)                              #
# --------------------------------------------------------------------------- #
@pytest.fixture
def sink():
    from app import smtp
    bound = smtp.start(0, host="127.0.0.1")
    yield bound
    smtp.stop()


def send_mail(port: int, subject: str = "Hello", rcpts=None, with_pdf: bool = False) -> None:
    import smtplib
    from email.message import EmailMessage
    from email.policy import SMTP

    msg = EmailMessage()
    msg["From"] = "shop@example.test"
    msg["To"] = "customer@example.test"
    msg["Subject"] = subject
    msg.set_content("body")
    if with_pdf:
        msg.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="a.pdf")
    with smtplib.SMTP("127.0.0.1", port, local_hostname="localhost") as conn:
        conn.sendmail("shop@example.test", rcpts or ["customer@example.test"], msg.as_bytes(policy=SMTP))


def test_empty_mailbox(monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", 1025)
    html = page()
    assert "The mailbox is empty." in html
    assert "SMTP listener on port 1025" in html
    assert "/__mock/ui/switch/mail-reject" in html
    assert "Current: <strong>false</strong>" in html.split("Reject mail")[1]


def test_smtp_listener_off(monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", 0)
    assert "SMTP listener off" in page()


def test_messages_listed(sink):
    send_mail(sink, subject="First message")
    send_mail(sink, subject="Second message", rcpts=["customer@example.test", "blind@example.test"],
              with_pdf=True)
    html = page()
    assert "The mailbox is empty." not in html
    assert html.index("Second message") < html.index("First message")
    row = next(r for r in html.split("<tr>") if "Second message" in r)
    assert "customer@example.test" in row and "blind@example.test" in row
    assert "<td>1</td>" in row
    newest = client.get("/__mock/mail").json()["mail"][0]["id"]
    assert f'href="/__mock/mail/{newest}/raw"' in html


def test_reject_switched_from_the_page():
    assert redirect(post("/__mock/ui/switch/mail-reject", value="true"))["msg"]
    assert client.get("/__mock/mail/reject").json() == {"enabled": True}
    assert "Current: <strong>true</strong>" in page().split("Reject mail")[1]
    assert redirect(post("/__mock/ui/switch/mail-reject", value="false"))["msg"]
    assert client.get("/__mock/mail/reject").json() == {"enabled": False}
    assert "error" in redirect(post("/__mock/ui/switch/mail-reject", value="maybe"))


def test_mailbox_cleared_from_the_page(sink):
    send_mail(sink)
    assert redirect(post("/__mock/ui/mail/clear")) == {"msg": "Mailbox cleared"}
    assert "The mailbox is empty." in page()
    assert client.get("/__mock/mail").json() == {"mail": []}


def test_subject_is_escaped(sink):
    send_mail(sink, subject="<img src=x onerror=alert(1)>")
    html = page()
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_reset_clicked_also_clears_mail(sink):
    send_mail(sink)
    client.post("/__mock/mail/reject", json={"enabled": True})
    assert redirect(post("/__mock/ui/reset"))["msg"]
    data = client.get("/__mock/status.json").json()
    assert data["mail"] == [] and data["switches"]["mail_reject"] is False
    assert "The mailbox is empty." in page()


def test_script_reads_the_mailbox(sink, monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", sink)
    send_mail(sink, subject="For scripts")
    data = client.get("/__mock/status.json").json()
    assert len(data["mail"]) == 1
    assert data["mail"][0] == client.get("/__mock/mail").json()["mail"][0]
    assert data["mail"][0]["subject"] == "For scripts"
    assert data["switches"]["mail_reject"] is False
    assert data["settings"]["smtp_port"] == sink


def test_mail_form_routes_are_404_when_test_endpoints_are_off(monkeypatch):
    monkeypatch.setattr(settings, "test_endpoints", False)
    assert post("/__mock/ui/mail/clear").status_code == 404
    assert post("/__mock/ui/switch/mail-reject", value="true").status_code == 404
    assert store.mail_reject() is False


# --------------------------------------------------------------------------- #
# Requirement: limits, refusals, KSeF auto-send and KSeF send calls            #
# --------------------------------------------------------------------------- #
def refuse_calls(count: int) -> None:
    """Make `count` authenticated calls over the per-minute limit currently set."""
    path = "/iapi/faktury.json"
    for _ in range(count):
        client.get(path + "?dataOd=2020-01-01", headers={"Authentication": sign(path, "faktura", "")})


def test_limits_and_refusals_are_shown(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    store.set_rate_limits(per_minute=1)
    refuse_calls(3)
    store.set_rate_limits(per_minute=30)
    html = page()
    assert "<dt>Rate limit per minute</dt><dd>30 (environment default 100)</dd>" in html
    assert "<dt>Refused calls</dt><dd>2</dd>" in html
    assert "<dt>Busiest minute</dt><dd>3</dd>" in html


def test_the_page_says_off_for_a_limit_that_is_off(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_day", 0)
    assert "<dt>Rate limit per day</dt><dd>off (environment default off)</dd>" in page()


def test_ksef_send_calls_are_shown_on_the_page_and_in_the_json():
    doc = seed(doc_type="fakturakraj")
    store.count_ksef_send(doc.ifirma_id)
    store.count_ksef_send(doc.ifirma_id)
    assert "<th>KSeF send calls</th>" in page()
    assert "<td>2</td>" in page()
    entry = client.get("/__mock/status.json").json()["documents"][0]
    assert entry["ksef_send_calls"] == 2


def test_ksef_auto_send_set_from_the_page():
    assert "msg" in redirect(post("/__mock/ui/switch/ksef-auto", value="send"))
    assert client.get("/__mock/ksef-auto").json() == {"mode": "send"}
    assert "<legend>KSeF auto-send</legend><p>Current: <strong>send</strong></p>" in page()


def test_an_invalid_ksef_auto_value_from_the_page_is_an_error():
    assert "error" in redirect(post("/__mock/ui/switch/ksef-auto", value="always"))
    assert client.get("/__mock/ksef-auto").json() == {"mode": "off"}


def test_rate_limits_set_from_the_page():
    assert "msg" in redirect(post("/__mock/ui/rate-limit", per_minute="30", per_day="500"))
    report = client.get("/__mock/rate-limit").json()
    assert (report["per_minute"], report["per_day"]) == (30, 500)


def test_a_blank_field_leaves_that_limit_alone():
    store.set_rate_limits(per_minute=7, per_day=9)
    redirect(post("/__mock/ui/rate-limit", per_minute="", per_day="11"))
    assert (store.limit_minute(), store.limit_day()) == (7, 11)


@pytest.mark.parametrize("fields", [
    {"per_minute": "-1", "per_day": ""},
    {"per_minute": "abc", "per_day": ""},
    {"per_minute": "1.5", "per_day": ""},
    {"per_minute": "", "per_day": ""},
])
def test_an_invalid_rate_limit_form_shows_an_error_and_changes_nothing(fields):
    store.set_rate_limits(per_minute=7, per_day=9)
    assert "error" in redirect(post("/__mock/ui/rate-limit", **fields))
    assert (store.limit_minute(), store.limit_day()) == (7, 9)


def test_next_day_from_the_page():
    store.set_rate_limits(per_day=2)
    for _ in range(3):
        store.register_call()
    assert "msg" in redirect(post("/__mock/ui/rate-limit/next-window", window="day"))
    assert store.calls_today() == 0
    assert store.register_call().allowed is True


def test_next_minute_from_the_page():
    store.set_rate_limits(per_minute=2)
    for _ in range(3):
        store.register_call()
    redirect(post("/__mock/ui/rate-limit/next-window", window="minute"))
    assert store.calls_this_minute() == 0


def test_an_unknown_window_from_the_page_is_an_error():
    assert "error" in redirect(post("/__mock/ui/rate-limit/next-window", window="hour"))


def test_the_page_offers_the_rate_limit_controls():
    html = page()
    assert 'action="/__mock/ui/rate-limit"' in html
    assert 'name="per_minute"' in html and 'name="per_day"' in html
    assert 'action="/__mock/ui/rate-limit/next-window"' in html
    assert 'value="minute"' in html and 'value="day"' in html


def test_script_reads_the_limits():
    store.set_rate_limits(per_minute=1)
    refuse_calls(2)
    rate = client.get("/__mock/status.json").json()["rate_limit"]
    assert rate["rejected_total"] == 1
    assert rate["per_minute"] == 1


def test_the_new_form_routes_answer_404_when_test_endpoints_are_off(monkeypatch):
    monkeypatch.setattr(settings, "test_endpoints", False)
    for route in ("/__mock/ui/switch/ksef-auto", "/__mock/ui/rate-limit", "/__mock/ui/rate-limit/next-window"):
        assert client.post(route, data={"value": "send"}, follow_redirects=False).status_code == 404
