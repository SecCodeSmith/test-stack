# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the persisted mock state: the store's snapshot/restore/listener contract and the
state-file scenarios (restart, clean start, corrupt file, write failure). A "restart" is a new
``with TestClient(app)`` block: entering it runs the FastAPI lifespan, which calls
``state.start(settings)`` and reloads the file, replacing whatever is in memory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import state, store
from app.config import settings
from app.main import app

ABONENT_KEY = "2" * 64
LOGGER = "ifirma-mock"


def sign(path: str, key_name: str, body: str) -> str:
    signed_url = settings.base_url + path
    message = (signed_url + settings.user + key_name + body).encode("utf-8")
    digest = hmac.new(bytes.fromhex(settings.keys[key_name]), message, hashlib.sha1).hexdigest()
    return f"IAPIS user={settings.user}, hmac-sha1={digest}"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No listener, no file and empty memory before and after every test in this module."""
    monkeypatch.setitem(settings.keys, "abonent", ABONENT_KEY)
    monkeypatch.setattr(settings, "rate_limit", 0)
    monkeypatch.setattr(settings, "test_endpoints", True)
    monkeypatch.setattr(settings, "state_file", "")
    monkeypatch.setattr(settings, "clean_start", False)
    state.start(settings)
    store.reset_all()
    yield
    monkeypatch.setattr(settings, "state_file", "")
    monkeypatch.setattr(settings, "clean_start", False)
    state.start(settings)
    store.reset_all()


def use_file(monkeypatch, path, clean: bool = False) -> str:
    monkeypatch.setattr(settings, "state_file", str(path))
    monkeypatch.setattr(settings, "clean_start", clean)
    return str(path)


def get_json(client, path: str, query: str = "", key: str = "faktura") -> dict:
    headers = {"Authentication": sign(path, key, "")}
    return client.get(path + (("?" + query) if query else ""), headers=headers).json()["response"]


def post_json(client, path: str, payload: dict, key: str = "faktura") -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    headers = {"Authentication": sign(path, key, body), "Content-Type": "application/json"}
    return client.post(path, content=body, headers=headers).json()["response"]


def invoice_payload() -> dict:
    return {
        "Zaplacono": 0,
        "LiczOd": "BRT",
        "Kontrahent": {"Identyfikator": "JK1", "Nazwa": "Jan Kowalski", "Email": "jan@example.com",
                       "KodPocztowy": "00-001", "Miejscowosc": "Warszawa"},
        "Pozycje": [{"NazwaPelna": "Produkt", "Ilosc": 1, "CenaJednostkowa": 100.0,
                     "StawkaVat": 0.23, "TypStawkiVat": "PRC"}],
    }


def create_invoice(client) -> dict:
    # A stored contractor keeps the generated-identifier sequence untouched.
    client.post("/__mock/contractors", json={"Nazwa": "Jan Kowalski", "Identyfikator": "JK1"})
    resp = post_json(client, "/iapi/fakturakraj.json", invoice_payload())
    assert resp["Kod"] == 0, resp
    return resp


def seed(client, **kwargs) -> dict:
    kwargs.setdefault("doc_type", "fakturaparagon")
    kwargs.setdefault("contractor", {"Nazwa": "Jan", "Identyfikator": "JAN1", "NIP": "1234567890"})
    kwargs.setdefault("gross", 100)
    kwargs.setdefault("issue_date", date.today().isoformat())
    resp = client.post("/__mock/documents", json=kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def listing(client) -> list[dict]:
    return get_json(client, "/iapi/faktury.json", "dataOd=2020-01-01")["Wynik"]


# --------------------------------------------------------------------------- #
# Store unit tests                                                             #
# --------------------------------------------------------------------------- #
def populate() -> None:
    store.create("fakturakraj", {"Kontrahent": {"Nazwa": "A", "NIP": "1234567890"},
                                 "Pozycje": [{"CenaJednostkowa": 10, "Ilosc": 2}]})
    store.create("fakturaparagon", {"Pozycje": [{"CenaJednostkowa": 5, "Ilosc": 1}]})
    store.add_payment(1, 20.0)
    store.set_ksef_status(2, "PRZYJETA", "KSEF-1")
    store.mark_sent(1, "a@example.com")
    store.contractor_create({"Nazwa": "Generated"})
    store.set_legacy_mode(True)
    store.set_pdf_not_ready(3)
    store.set_contractor_create_mode("rename")
    store.register_call()
    store.register_call()


def test_snapshot_restore_round_trip():
    populate()
    first = store.snapshot()
    json.dumps(first)  # plain JSON-serialisable data
    store.reset_all()
    assert store.snapshot()["documents"] == []
    store.restore(first)
    assert store.snapshot() == first
    assert store.calls_total() == 2
    assert store.legacy_mode() is True
    assert store.pdf_not_ready_remaining() == 3
    assert store.contractor_create_mode() == "rename"
    assert store.get(1).sent_to == ["a@example.com"]
    assert store.get(2).ksef_number == "KSEF-1"
    # sequences continue after the restore
    assert store.create("fakturakraj", {}).ifirma_id == 3
    store.set_contractor_create_mode("normal")
    assert store.contractor_create({"Nazwa": "Next"})["Identyfikator"] == "K000002"


def test_snapshot_excludes_the_per_minute_window():
    store.register_call()
    assert store.calls_this_minute() == 1
    snap = store.snapshot()
    # Deliberate change (add-rate-limit-queue-and-ksef-confirmed-delivery): the snapshot also keeps
    # the day count, the refused-call total and the runtime limits. The minute count stays out.
    assert set(snap) == {"next_document_id", "next_contractor_seq", "documents", "contractors",
                         "switches", "calls_total", "calls_day", "rejected_total",
                         "limit_minute", "limit_day"}
    store.restore(snap)
    assert store.calls_total() == 1
    assert store.calls_this_minute() == 0


def test_restore_tolerates_missing_and_unknown_document_keys():
    store.restore({
        "next_document_id": 5,
        "documents": [
            {"ifirma_id": 4, "doc_type": "fakturakraj", "number": "4/2026", "payload": {},
             "gross": 12.5, "from_the_future": "dropped"},
            {"doc_type": "no id, skipped"},
        ],
    })
    doc = store.get(4)
    assert doc is not None
    assert doc.gross == 12.5
    assert doc.ksef_status is None and doc.currency == "PLN" and doc.sent_to == []
    assert not hasattr(doc, "from_the_future")
    assert len(store.all_documents()) == 1
    assert store.create("fakturakraj", {}).ifirma_id == 5


def test_restore_never_reuses_an_existing_document_id():
    store.restore({"next_document_id": 1,
                   "documents": [{"ifirma_id": 7, "doc_type": "x", "number": "7/2026", "payload": {}}]})
    assert store.create("fakturakraj", {}).ifirma_id == 8


def test_listener_is_called_once_per_mutation():
    seen: list[dict] = []
    store.on_change(seen.append)
    mutations = [
        lambda: store.create("fakturakraj", {}),
        lambda: store.add_payment(1, 1.0),
        lambda: store.set_ksef_status(1, "WYSLANA"),
        lambda: store.mark_sent(1, "a@example.com"),
        lambda: store.mark_deleted(1),
        lambda: store.contractor_create({"Identyfikator": "C1"}),
        lambda: store.contractor_upsert({"Identyfikator": "C2"}),
        lambda: store.contractor_update("C1", {"Nazwa": "x"}),
        lambda: store.set_contractor_create_mode("rename"),
        lambda: store.set_pdf_not_ready(1),
        lambda: store.pdf_not_ready_consume(),
        lambda: store.set_legacy_mode(True),
        lambda: store.register_call(),
        lambda: store.set_rate_limits(per_minute=5),
        lambda: store.next_window("day"),
        lambda: store.reset_test_switches(),
        lambda: store.contractors_reset(),
        lambda: store.reset(),
        lambda: store.restore({}),
        lambda: store.reset_all(),
    ]
    for index, mutate in enumerate(mutations, start=1):
        mutate()
        assert len(seen) == index, f"mutation #{index} fired {len(seen)} listener calls"
    assert all(isinstance(s, dict) and "documents" in s for s in seen)


def test_listener_is_not_called_for_failed_or_read_only_calls():
    store.create("fakturakraj", {})
    store.contractor_create({"Identyfikator": "C1"})
    seen: list[dict] = []
    store.on_change(seen.append)
    assert store.contractor_create({"Identyfikator": "C1"}) is None  # duplicate
    assert store.set_ksef_status(99, "WYSLANA") is None
    assert store.mark_sent(99, "a@example.com") is None
    assert store.add_payment(99, 1.0) is None
    assert store.mark_deleted(99) is False
    assert store.contractor_update("nope", {}) is None
    assert store.pdf_not_ready_consume() is False
    store.snapshot(), store.get(1), store.all_documents(), store.calls_total()
    assert seen == []


def test_listener_receives_the_state_after_the_change():
    seen: list[dict] = []
    store.on_change(seen.append)
    store.create("fakturakraj", {})
    assert [d["ifirma_id"] for d in seen[-1]["documents"]] == [1]
    assert seen[-1]["next_document_id"] == 2
    store.on_change(None)
    store.create("fakturakraj", {})
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# Requirement: State survives a restart                                        #
# --------------------------------------------------------------------------- #
def test_issued_document_outlives_a_restart(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        created = create_invoice(client)
        seeded = seed(client, doc_type="fakturakraj")
        before = {row["FakturaId"]: row["PelnyNumer"] for row in listing(client)}
    assert str(created["Identyfikator"]) in {str(k) for k in before}

    with TestClient(app) as client:
        after = {row["FakturaId"]: row["PelnyNumer"] for row in listing(client)}
        assert after == before
        assert seeded["FakturaId"] in after
        ident = created["Identyfikator"]
        pdf = client.get(f"/iapi/fakturakraj/{ident}.pdf",
                         headers={"Authentication": sign(f"/iapi/fakturakraj/{ident}.pdf", "faktura", "")})
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        detail = get_json(client, f"/iapi/fakturakraj/{ident}.json")
        assert detail["Kod"] == 0


def test_identifier_sequences_continue(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        create_invoice(client)
        create_invoice(client)
        contractor = client.post("/__mock/contractors", json={"Nazwa": "Generated"}).json()
        assert contractor["Identyfikator"] == "K000001"

    with TestClient(app) as client:
        third = create_invoice(client)
        assert str(third["Identyfikator"]) == "3"
        nxt = client.post("/__mock/contractors", json={"Nazwa": "Second generated"}).json()
        assert nxt["Identyfikator"] == "K000002"


def test_contractors_and_switches_are_kept(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        client.post("/__mock/contractors", json={"Nazwa": "Kept", "Identyfikator": "KEPT1", "NIP": "1234567890"})
        assert client.post("/__mock/legacy-mode", json={"enabled": True}).status_code == 200
        client.post("/__mock/pdf-not-ready", json={"calls": 4})
        client.post("/__mock/contractor-mode", json={"create": "rename"})

    with TestClient(app) as client:
        found = get_json(client, "/iapi/kontrahenci/id/KEPT1.json")
        assert found["Kod"] == 0
        assert found["Wynik"][0]["Nazwa"] == "Kept"
        assert client.get("/__mock/legacy-mode").json() == {"enabled": True}
        assert client.get("/__mock/pdf-not-ready").json() == {"remaining": 4}
        assert client.get("/__mock/contractor-mode").json() == {"create": "rename"}


def test_served_call_counter_continues(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        for _ in range(3):
            listing(client)
        first = get_json(client, "/iapi/abonent/limit.json", key="abonent")
        used_before = first["LimitWykorzystany"]
    assert used_before == 4

    with TestClient(app) as client:
        again = get_json(client, "/iapi/abonent/limit.json", key="abonent")
        assert again["LimitWykorzystany"] == used_before + 1


def test_payments_and_deletions_are_kept(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        paid = create_invoice(client)
        gone = create_invoice(client)
        assert post_json(client, f"/iapi/faktury/wplaty/fakturakraj/{paid['Identyfikator']}.json",
                         {"Kwota": 123.0})["Kod"] == 0
        assert client.delete(f"/__mock/documents/{gone['Identyfikator']}").status_code == 200

    with TestClient(app) as client:
        rows = {str(r["FakturaId"]): r for r in listing(client)}
        assert str(gone["Identyfikator"]) not in rows
        assert rows[str(paid["Identyfikator"])]["Zaplacono"] == 123.0
        detail = get_json(client, f"/iapi/fakturakraj/{gone['Identyfikator']}.json")
        assert detail["Kod"] != 0


# --------------------------------------------------------------------------- #
# Requirement: Memory-only mode without a state file                           #
# --------------------------------------------------------------------------- #
def test_test_suite_and_ad_hoc_runs_write_no_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        create_invoice(client)
        seed(client)
        client.post("/__mock/reset")
        create_invoice(client)
    assert list(tmp_path.iterdir()) == []
    assert state.info()["mode"] == "memory"
    assert state.info()["path"] == ""
    assert state.info()["last_write"] is None
    # in memory the state is empty after a reset and the default switches apply
    store.reset_all()
    assert listing(TestClient(app)) == []
    assert store.legacy_mode() is False


# --------------------------------------------------------------------------- #
# Requirement: Durable writes and tolerant loading                             #
# --------------------------------------------------------------------------- #
def test_first_start_on_an_empty_volume(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    use_file(monkeypatch, path)
    with TestClient(app) as client:
        assert listing(client) == []
        create_invoice(client)
    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()
    assert len(json.loads(path.read_text(encoding="utf-8"))["documents"]) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    assert state.info()["mode"] == "file"
    assert state.info()["loaded_from_file"] is False


@pytest.mark.parametrize("content", ["{ this is not json", '{"version": 99, "documents": []}', "[1, 2]"])
def test_corrupt_or_foreign_file(monkeypatch, tmp_path, caplog, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    use_file(monkeypatch, path)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with TestClient(app) as client:
            assert listing(client) == []
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("state.json.corrupt-")]
    assert len(aside) == 1
    assert aside[0].read_text(encoding="utf-8") == content
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == LOGGER]
    assert warnings and str(path) in warnings[0].getMessage()
    assert state.info()["loaded_from_file"] is False
    # the mock then writes a fresh, valid file
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_write_failure_is_visible(monkeypatch, tmp_path, caplog):
    path = tmp_path / "missing-dir" / "state.json"
    use_file(monkeypatch, path)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with TestClient(app) as client:
            caplog.clear()
            first = create_invoice(client)
            second = create_invoice(client)
    assert first["Kod"] == 0 and second["Kod"] == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.name == LOGGER]
    assert len(errors) >= 2
    assert all(str(path) in r.getMessage() for r in errors)
    assert not path.exists()


# --------------------------------------------------------------------------- #
# Requirement: Clean-start argument                                            #
# --------------------------------------------------------------------------- #
def test_clean_start_discards_the_stored_state(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    use_file(monkeypatch, path)
    with TestClient(app) as client:
        create_invoice(client)
        client.post("/__mock/contractors", json={"Nazwa": "Old", "Identyfikator": "OLD1"})
        client.post("/__mock/legacy-mode", json={"enabled": True})
        listing(client)
    assert len(json.loads(path.read_text(encoding="utf-8"))["documents"]) == 1

    monkeypatch.setattr(settings, "clean_start", True)
    with TestClient(app) as client:
        assert listing(client) == []
        assert get_json(client, "/iapi/kontrahenci/id/OLD1.json")["Kod"] != 0
        assert client.get("/__mock/legacy-mode").json() == {"enabled": False}
        # the call counter was zero at start: only the two calls above plus this one are counted
        assert get_json(client, "/iapi/abonent/limit.json", key="abonent")["LimitWykorzystany"] == 3
        on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["documents"] == [] and on_disk["contractors"] == {}
    assert on_disk["switches"]["legacy_mode"] is False
    assert state.info()["clean_start"] is True


def test_clean_start_without_a_file(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    use_file(monkeypatch, path, clean=True)
    with TestClient(app) as client:
        assert listing(client) == []
    assert path.exists()


# --------------------------------------------------------------------------- #
# Requirement: Reset clears the persisted state                                #
# --------------------------------------------------------------------------- #
def test_reset_then_restart(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        create_invoice(client)
        create_invoice(client)
        client.post("/__mock/contractors", json={"Nazwa": "Generated"})
        assert client.post("/__mock/reset").json() == {"reset": True}

    with TestClient(app) as client:
        assert listing(client) == []
        assert str(create_invoice(client)["Identyfikator"]) == "1"
        assert client.post("/__mock/contractors", json={"Nazwa": "G"}).json()["Identyfikator"] == "K000001"


# --------------------------------------------------------------------------- #
# Requirement: State file format is versioned                                  #
# --------------------------------------------------------------------------- #
def test_inspecting_the_file(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    use_file(monkeypatch, path)
    with TestClient(app) as client:
        create_invoice(client)
        client.post("/__mock/contractors", json={"Nazwa": "C", "Identyfikator": "C1"})
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))  # UTF-8 JSON
    assert data["version"] == 1
    assert isinstance(data["saved_at"], str) and data["saved_at"]
    assert data["next_document_id"] == 2 and data["next_contractor_seq"] == 1
    assert isinstance(data["documents"], list) and data["documents"][0]["ifirma_id"] == 1
    assert "C1" in data["contractors"]
    assert set(data["switches"]) == {"contractor_create_mode", "pdf_not_ready_remaining", "legacy_mode",
                                     "ksef_auto"}
    assert isinstance(data["calls_total"], int) and data["calls_total"] >= 1
    assert state.info()["last_write"] == data["saved_at"]


# --------------------------------------------------------------------------- #
# Day count, refusals and runtime limits survive a restart                     #
# --------------------------------------------------------------------------- #
def test_day_count_and_limits_are_kept():
    for _ in range(40):
        store.register_call()
    store.set_rate_limits(per_minute=30, per_day=500)
    snap = store.snapshot()
    store.reset_all()
    store.restore(snap)
    assert store.calls_today() == 40
    assert (store.limit_minute(), store.limit_day()) == (30, 500)


def test_refused_calls_are_kept():
    store.set_rate_limits(per_minute=1)
    store.register_call()
    store.register_call()
    snap = store.snapshot()
    store.reset_all()
    store.restore(snap)
    assert store.rejected_total() == 1


def test_day_count_from_an_earlier_day_starts_from_zero():
    snap = store.snapshot()
    snap["calls_day"] = {"day_key": "2020-01-01", "count": 40}
    store.restore(snap)
    assert store.calls_today() == 0


def test_state_file_without_the_new_keys_loads():
    store.restore({"next_document_id": 3, "calls_total": 9, "switches": {}})
    assert store.calls_total() == 9
    assert store.calls_today() == 0
    assert store.rejected_total() == 0
    assert (store.limit_minute(), store.limit_day()) == (settings.rate_limit, settings.rate_limit_day)


def test_runtime_limits_survive_a_restart_through_the_state_file(monkeypatch, tmp_path):
    use_file(monkeypatch, tmp_path / "state.json")
    with TestClient(app) as client:
        client.post("/__mock/rate-limit", json={"per_day": 500})
    with TestClient(app) as client:
        assert client.get("/__mock/rate-limit").json()["per_day"] == 500


def test_ksef_auto_and_ksef_send_calls_are_kept():
    store.create("fakturakraj", {})
    store.count_ksef_send(1)
    store.count_ksef_send(1)
    store.set_ksef_auto("send")
    snap = store.snapshot()
    store.reset_all()
    store.restore(snap)
    assert store.ksef_auto() == "send"
    assert store.get(1).ksef_send_calls == 2


def test_state_file_without_ksef_keys_starts_with_auto_off_and_no_send_calls():
    store.restore({"next_document_id": 2, "switches": {"legacy_mode": True},
                   "documents": [{"ifirma_id": 1, "doc_type": "fakturakraj", "number": "1/2026", "payload": {}}]})
    assert store.ksef_auto() == "off"
    assert store.get(1).ksef_send_calls == 0
    assert store.legacy_mode() is True


def test_an_unknown_saved_ksef_auto_mode_falls_back_to_off():
    store.restore({"switches": {"ksef_auto": "sometimes"}})
    assert store.ksef_auto() == "off"
