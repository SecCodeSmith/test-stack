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
    assert set(data) == {"service", "version", "settings", "persistence", "counters", "switches",
                         "documents", "contractors"}
    assert data["version"] == app.version
    assert set(data["settings"]) == {"user", "base_url", "strict_auth", "rate_limit",
                                     "strict_list_window", "test_endpoints"}
    assert set(data["counters"]) == {"calls_total", "calls_this_minute", "limit_granted"}
    assert set(data["switches"]) == {"contractor_mode", "pdf_not_ready_remaining", "legacy_mode"}
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
