# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tests for the defaults the compose file gives the mock (spec mock-rate-limit "Compose defaults follow iFirma").

The code default of the mock is "no limit"; the stack turns the limits on with iFirma's real numbers. The compose file
is read as text, so no YAML package is needed.
"""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def default_of(variable: str) -> str:
    """The value after ``:-`` of ``VARIABLE: ${VARIABLE:-value}``."""
    match = re.search(rf"^\s*{variable}:\s*\$\{{{variable}:-([^}}]*)\}}\s*$", compose_text(), re.MULTILINE)
    assert match, f"{variable} has no default in {COMPOSE.name}"
    return match.group(1)


def test_stack_started_with_defaults_limits_the_mock_to_ifirmas_100_per_minute():
    assert default_of("IFIRMA_MOCK_RATE_LIMIT") == "100"


def test_stack_started_with_defaults_limits_the_mock_to_ifirmas_15000_per_day():
    assert default_of("IFIRMA_MOCK_RATE_LIMIT_DAY") == "15000"


def test_the_prestashop_image_takes_its_tag_from_ps_image_tag_with_a_pinned_default():
    match = re.search(r"^\s*image:\s*prestashop/prestashop:\$\{PS_IMAGE_TAG:-([^}]+)\}\s*$", compose_text(), re.MULTILINE)

    assert match, "the PrestaShop image line must read its tag from PS_IMAGE_TAG"
    assert match.group(1) != "latest"
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[\w.]+)?", match.group(1)), "the default tag is a pinned version"
