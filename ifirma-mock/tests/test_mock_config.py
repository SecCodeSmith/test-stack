# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""Parsing of IFIRMA_MOCK_MONTHLY_LIMIT: a whole number of at least 1, otherwise the 1000 default."""
from __future__ import annotations

import pytest

from app.config import DEFAULT_MONTHLY_LIMIT, monthly_limit_from_env


def test_a_configured_monthly_limit_is_read():
    assert monthly_limit_from_env("2000") == 2000


def test_the_default_monthly_limit_is_ifirmas_1000():
    assert DEFAULT_MONTHLY_LIMIT == 1000


@pytest.mark.parametrize("raw", [None, "", "   ", "0", "-5", "abc", "2.5"])
def test_an_unusable_monthly_limit_falls_back_to_1000(raw):
    assert monthly_limit_from_env(raw) == 1000
