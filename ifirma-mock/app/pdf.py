# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""
Tiny, dependency-free PDF generator.

Builds a valid single-page PDF (with correct xref offsets) so the mock can return
a real application/pdf body that the PrestaShop module streams to the browser,
without pulling in a PDF library.
"""
from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(title: str, lines: list[str]) -> bytes:
    """Return the bytes of a minimal one-page PDF containing the given text."""
    content = "BT\n/F1 18 Tf\n72 790 Td\n(%s) Tj\n/F1 12 Tf\n" % _escape(title)
    first = True
    for line in lines:
        content += "0 -%d Td\n(%s) Tj\n" % (30 if first else 18, _escape(line))
        first = False
    content += "ET"
    content_bytes = content.encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content_bytes), content_bytes),
    ]

    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + obj + b"\nendobj\n"

    xref_pos = len(pdf)
    size = len(objects) + 1
    pdf += b"xref\n0 %d\n" % size
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (size, xref_pos)
    return pdf
