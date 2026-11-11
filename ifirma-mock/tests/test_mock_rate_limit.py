# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the simulated per-minute and per-day request limits (spec mock-rate-limit).

The store takes its clock from ``store._now``, so these tests move time without sleeping.
"""
from __future__ import annotations

import time

import pytest

from app import store
from app.config import settings


def local_epoch(hour: int, minute: int, second: int, day: int = 10) -> float:
    """Epoch seconds of a local wall-clock time, so day and minute keys do not depend on the host zone."""
    return time.mktime((2026, 3, day, hour, minute, second, 0, 0, -1))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = [local_epoch(10, 0, 20)]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    monkeypatch.setattr(settings, "rate_limit", 0)
    monkeypatch.setattr(settings, "rate_limit_day", 0)
    store.reset_all()
    yield now
    store.reset_all()


def call_n(times: int) -> list[store.CallVerdict]:
    return [store.register_call() for _ in range(times)]


def test_minute_limit_reached(clock):
    store.set_rate_limits(per_minute=5)
    verdicts = call_n(6)
    assert [v.allowed for v in verdicts] == [True] * 5 + [False]
    assert verdicts[-1].window == "minute"
    assert verdicts[-1].limit == 5


def test_next_minute(clock):
    store.set_rate_limits(per_minute=5)
    call_n(5)
    clock[0] += 60
    assert store.register_call().allowed is True


def test_day_limit_reached(clock):
    store.set_rate_limits(per_day=20)
    for _ in range(20):
        assert store.register_call().allowed is True
        clock[0] += 60  # a new minute every time, so only the day window can refuse
    verdict = store.register_call()
    assert verdict.allowed is False
    assert verdict.window == "day"
    assert verdict.limit == 20


def test_limits_off(clock):
    store.set_rate_limits(per_minute=0, per_day=0)
    assert all(v.allowed for v in call_n(200))


def test_minute_window_is_checked_before_the_day_window(clock):
    store.set_rate_limits(per_minute=2, per_day=2)
    call_n(2)
    assert store.register_call().window == "minute"


def test_busiest_minute(clock):
    call_n(12)
    clock[0] += 60
    call_n(7)
    assert store.peak_minute() == 12


def test_refused_calls_counted(clock):
    store.set_rate_limits(per_minute=2)
    call_n(5)
    assert store.rejected_total() == 3


def test_every_call_counts_in_total_minute_and_day(clock):
    store.set_rate_limits(per_minute=2)
    call_n(4)
    assert (store.calls_total(), store.calls_this_minute(), store.calls_today()) == (4, 4, 4)


def test_retry_after_is_at_least_one_at_the_last_second_of_a_minute(clock):
    store.set_rate_limits(per_minute=1)
    clock[0] = local_epoch(10, 0, 59) + 0.9
    call_n(1)
    verdict = store.register_call()
    assert verdict.allowed is False
    assert verdict.retry_after == 1


def test_minute_retry_after_is_the_time_left_in_the_minute(clock):
    store.set_rate_limits(per_minute=1)
    clock[0] = local_epoch(10, 0, 40)
    call_n(1)
    assert store.register_call().retry_after == 20


def test_day_retry_after_is_the_time_until_local_midnight(clock):
    store.set_rate_limits(per_day=1)
    clock[0] = local_epoch(23, 0, 0)
    call_n(1)
    verdict = store.register_call()
    assert verdict.window == "day"
    assert verdict.retry_after == 3600


def test_day_count_restarts_on_a_new_day(clock):
    store.set_rate_limits(per_day=3)
    call_n(3)
    clock[0] = local_epoch(0, 5, 0, day=11)
    assert store.register_call().allowed is True
    assert store.calls_today() == 1


def test_limits_come_from_the_environment_until_set_at_runtime(clock, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    monkeypatch.setattr(settings, "rate_limit_day", 15000)
    assert (store.limit_minute(), store.limit_day()) == (100, 15000)
    store.set_rate_limits(per_minute=30)
    assert (store.limit_minute(), store.limit_day()) == (30, 15000)


def test_runtime_zero_overrides_a_non_zero_environment_limit(clock, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    store.set_rate_limits(per_minute=0)
    assert store.limit_minute() == 0


def test_reset_restores_the_environment_limits(clock, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    store.set_rate_limits(per_minute=30, per_day=500)
    store.reset_all()
    assert (store.limit_minute(), store.limit_day()) == (100, 0)


def test_next_window_minute_clears_only_the_minute_count(clock):
    store.set_rate_limits(per_minute=2)
    call_n(3)
    store.next_window("minute")
    assert store.calls_this_minute() == 0
    assert store.calls_today() == 3
    assert store.calls_total() == 3
    assert store.register_call().allowed is True


def test_next_window_day_clears_only_the_day_count(clock):
    store.set_rate_limits(per_day=2)
    call_n(3)
    store.next_window("day")
    assert store.calls_today() == 0
    assert store.calls_total() == 3
    verdict = store.register_call()
    assert verdict.allowed is True
    assert store.calls_today() == 1


def test_next_window_refuses_an_unknown_window(clock):
    call_n(1)
    with pytest.raises(ValueError):
        store.next_window("hour")
    assert store.calls_today() == 1


# --------------------------------------------------------------------------- #
# The limit answer over HTTP                                                   #
# --------------------------------------------------------------------------- #
import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from tests.test_mock_extensions import sign  # noqa: E402

http = TestClient(app)
LIST_PATH = "/iapi/faktury.json"
CREATE_PATH = "/iapi/fakturaparagon.json"


@pytest.fixture(autouse=True)
def _http_state(monkeypatch):
    monkeypatch.setattr(settings, "test_endpoints", True)
    monkeypatch.setattr(settings, "strict_auth", True)
    http.post("/__mock/reset")
    yield
    http.post("/__mock/reset")


def signed_get(path: str, query: str = "dataOd=2020-01-01"):
    return http.get(f"{path}?{query}" if query else path, headers={"Authentication": sign(path, "faktura", "")})


def signed_post(path: str, payload: dict):
    body = json.dumps(payload, ensure_ascii=False)
    headers = {"Authentication": sign(path, "faktura", body), "Content-Type": "application/json"}
    return http.post(path, content=body, headers=headers)


RECEIPT = {"Kontrahent": {"Nazwa": "Jan", "Identyfikator": "JAN1"},
           "Pozycje": [{"NazwaPelna": "x", "Ilosc": 1, "CenaJednostkowa": 10.0,
                        "StawkaVat": 0.23, "TypStawkiVat": "PRC"}]}


def test_minute_refusal_answer(clock):
    store.set_rate_limits(per_minute=100)
    for _ in range(100):
        store.register_call()
    clock[0] = local_epoch(10, 0, 40)
    resp = signed_get(LIST_PATH)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "20"
    assert resp.json()["response"] == {"Kod": 100, "Informacja": "Przekroczono limit zapytań (limit minutowy: 100)"}


def test_day_refusal_answer(clock):
    store.set_rate_limits(per_day=15000)
    clock[0] = local_epoch(23, 0, 0)
    for _ in range(15000):
        store.register_call()
    resp = signed_get(LIST_PATH)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3600"
    assert resp.json()["response"]["Informacja"] == "Przekroczono limit zapytań (limit dzienny: 15000)"


def test_a_refused_create_has_no_other_effect(clock):
    store.set_rate_limits(per_minute=1)
    store.register_call()
    resp = signed_post(CREATE_PATH, RECEIPT)
    assert resp.status_code == 429
    assert store.all_documents() == []


def test_unauthenticated_calls_do_not_count(clock):
    resp = http.get(LIST_PATH + "?dataOd=2020-01-01", headers={"Authentication": "IAPIS user=x, hmac-sha1=00"})
    assert resp.status_code == 401
    assert (store.calls_total(), store.calls_this_minute(), store.calls_today()) == (0, 0, 0)


def test_a_call_served_under_the_limit_is_unchanged(clock):
    store.set_rate_limits(per_minute=5)
    resp = signed_get(LIST_PATH)
    assert resp.status_code == 200
    assert resp.json()["response"]["Kod"] == 0
    assert "Retry-After" not in resp.headers


# --------------------------------------------------------------------------- #
# Runtime control routes                                                       #
# --------------------------------------------------------------------------- #
def test_limits_changed_at_runtime(clock, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    monkeypatch.setattr(settings, "rate_limit_day", 15000)
    resp = http.post("/__mock/rate-limit", json={"per_minute": 30})
    assert resp.status_code == 200
    report = http.get("/__mock/rate-limit").json()
    assert (report["per_minute"], report["per_day"]) == (30, 15000)
    assert report["defaults"] == {"per_minute": 100, "per_day": 15000}


@pytest.mark.parametrize("body", [
    {"per_minute": -1},
    {"per_minute": 1.5},
    {"per_minute": "5"},
    {"per_minute": True},
    {"per_week": 3},
    {},
    [],
])
def test_invalid_value_refused(clock, body):
    store.set_rate_limits(per_minute=7)
    resp = http.post("/__mock/rate-limit", json=body)
    assert resp.status_code == 422
    assert store.limit_minute() == 7


def test_reset_restores_the_environment_over_http(clock, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    http.post("/__mock/rate-limit", json={"per_minute": 30})
    http.post("/__mock/reset")
    assert http.get("/__mock/rate-limit").json()["per_minute"] == 100


def test_rate_limit_report_carries_the_counters(clock):
    store.set_rate_limits(per_minute=2)
    for _ in range(3):
        store.register_call()
    report = http.get("/__mock/rate-limit").json()
    assert (report["calls_this_minute"], report["calls_today"], report["calls_total"]) == (3, 3, 3)
    assert (report["rejected_total"], report["peak_minute"]) == (1, 3)


def test_test_endpoints_disabled_hide_the_rate_limit_routes(clock, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit", 100)
    monkeypatch.setattr(settings, "test_endpoints", False)
    assert http.get("/__mock/rate-limit").status_code == 404
    assert http.post("/__mock/rate-limit", json={"per_minute": 1}).status_code == 404
    assert http.post("/__mock/rate-limit/next-window", json={"window": "day"}).status_code == 404
    assert store.limit_minute() == 100


def test_next_window_day_over_http(clock):
    store.set_rate_limits(per_day=2)
    for _ in range(3):
        store.register_call()
    resp = http.post("/__mock/rate-limit/next-window", json={"window": "day"})
    assert resp.status_code == 200
    assert store.calls_today() == 0
    assert signed_get(LIST_PATH).status_code == 200
    assert store.calls_today() == 1


@pytest.mark.parametrize("body", [{"window": "hour"}, {"window": 1}, {}, []])
def test_unknown_window_refused(clock, body):
    store.register_call()
    resp = http.post("/__mock/rate-limit/next-window", json=body)
    assert resp.status_code == 422
    assert store.calls_today() == 1


# --------------------------------------------------------------------------- #
# abonent/limit.json                                                           #
# --------------------------------------------------------------------------- #
ABONENT_PATH = "/iapi/abonent/limit.json"


def abonent_limit() -> dict:
    headers = {"Authentication": sign(ABONENT_PATH, "abonent", "")}
    return http.get(ABONENT_PATH, headers=headers).json()["response"]


@pytest.fixture
def abonent_key(monkeypatch):
    monkeypatch.setitem(settings.keys, "abonent", "2" * 64)


def test_daily_usage_on_the_limit_endpoint(clock, abonent_key):
    for _ in range(40):
        store.register_call()
    answer = abonent_limit()
    assert (answer["LimitWykorzystany"], answer["LimitPrzyznany"]) == (41, 15000)


def test_the_limit_endpoint_reports_the_day_limit_in_force(clock, abonent_key):
    store.set_rate_limits(per_day=500)
    assert abonent_limit()["LimitPrzyznany"] == 500


def test_the_limit_endpoint_counts_today_and_not_the_all_time_total(clock, abonent_key):
    for _ in range(5):
        store.register_call()
    clock[0] = local_epoch(9, 0, 0, day=11)
    assert abonent_limit()["LimitWykorzystany"] == 1
