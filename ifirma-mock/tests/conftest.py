# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""Shared fixtures: keep the SMTP sink off unless a test starts it on purpose."""
from __future__ import annotations

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _no_smtp_listener(monkeypatch):
    """``with TestClient(app)`` runs the lifespan, which would bind the default port 1025."""
    monkeypatch.setattr(settings, "smtp_port", 0)
