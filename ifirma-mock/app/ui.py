# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""HTML rendering and form reading for the mock's status page (``/__mock/status``).

Pure functions: they take the dictionary built by ``main.status_data()`` and return a string.
Every dynamic value goes through ``html.escape``; no dependency beyond the standard library.
"""
from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request

README_URL = "https://github.com/SecCodeSmith/test-stack#readme"

STYLE = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:1.5rem;color:#1b1f23;background:#fafbfc}
h1{margin:0 0 .5rem}h2{margin-top:1.6rem;border-bottom:1px solid #d0d7de;padding-bottom:.2rem}
nav a{margin-right:1rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border:1px solid #d0d7de;padding:.3rem .5rem;text-align:left;vertical-align:top}
th{background:#eef1f4}
.flash{padding:.6rem .9rem;border-radius:4px;margin:1rem 0}
.msg{background:#dafbe1;border:1px solid #4ac26b}
.error{background:#ffebe9;border:1px solid #ff8182}
.deleted{background:#f3f3f3;color:#777}
.marker{background:#cf222e;color:#fff;border-radius:3px;padding:0 .35rem;font-size:.75rem}
form.inline{display:inline}
fieldset{border:1px solid #d0d7de;margin:.5rem 0;padding:.5rem .8rem}
label{margin-right:.8rem}
button{cursor:pointer}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:0}
dt{font-weight:600}dd{margin:0}
"""


def _e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


async def read_form(request: Request) -> dict[str, str]:
    """The submitted form as a flat dict of strings.

    Uses ``request.form()``; when python-multipart is not installed that raises, so the raw
    urlencoded body is parsed with the standard library instead.
    """
    try:
        form = await request.form()
        return {str(key): str(value) for key, value in form.items()}
    except Exception:  # noqa: BLE001 - missing python-multipart (or unreadable form)
        raw = (await request.body()).decode("utf-8", errors="replace")
        return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def _kv(items: list[tuple[str, Any]]) -> str:
    rows = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in items)
    return f"<dl>{rows}</dl>"


def _persistence(info: dict[str, Any]) -> str:
    if info.get("mode") == "memory":
        return "<p>Persistence: memory only (nothing is written to disk).</p>"
    return _kv([
        ("Persistence", "state file"),
        ("Path", info.get("path")),
        ("Last write", info.get("last_write") or "not written yet"),
        ("Clean start", info.get("clean_start")),
        ("Loaded from file", info.get("loaded_from_file")),
    ])


def _switch_form(name: str, label: str, current: str, control: str) -> str:
    return (
        f'<form method="post" action="/__mock/ui/switch/{_e(name)}">'
        f"<fieldset><legend>{_e(label)}</legend><p>Current: <strong>{_e(current)}</strong></p>"
        f"{control}<button type=\"submit\">Set</button></fieldset></form>"
    )


def _switches(sw: dict[str, Any]) -> str:
    mode = sw["contractor_mode"]
    options = "".join(
        f'<option value="{m}"{" selected" if m == mode else ""}>{m}</option>' for m in ("normal", "rename")
    )
    legacy = "true" if sw["legacy_mode"] else "false"
    legacy_options = "".join(
        f'<option value="{v}"{" selected" if v == legacy else ""}>{v}</option>' for v in ("true", "false")
    )
    return (
        _switch_form("contractor-mode", "Contractor create mode", mode,
                     f'<label>value <select name="value">{options}</select></label>')
        + _switch_form("pdf-not-ready", "PDF not ready (number of downloads answering 404)",
                       str(sw["pdf_not_ready_remaining"]),
                       f'<label>value <input type="number" name="value" min="0" '
                       f'value="{_e(sw["pdf_not_ready_remaining"])}"></label>')
        + _switch_form("legacy-mode", "Legacy mode", legacy,
                       f'<label>value <select name="value">{legacy_options}</select></label>')
    )


def _seed_form() -> str:
    return (
        '<form method="post" action="/__mock/ui/seed"><fieldset><legend>Seed a document</legend>'
        '<label>type <select name="doc_type"><option value="fakturaparagon">fakturaparagon (receipt)</option>'
        '<option value="fakturakraj">fakturakraj (invoice)</option></select></label>'
        '<label>name <input type="text" name="name" value="Jan Kowalski"></label>'
        '<label>identifier <input type="text" name="identifier"></label>'
        '<label>NIP <input type="text" name="nip"></label>'
        '<label>email <input type="text" name="email"></label>'
        '<label>gross <input type="text" name="gross" value="123.45"></label>'
        '<label>paid <input type="text" name="paid" value="0"></label>'
        '<button type="submit">Seed document</button></fieldset></form>'
    )


def _doc_buttons(doc: dict[str, Any]) -> str:
    if doc["deleted"]:
        return ""
    base = f"/__mock/ui/documents/{_e(doc['FakturaId'])}"
    return (
        f'<form class="inline" method="post" action="{base}/ksef-accept"><button type="submit">KSeF accept</button></form> '
        f'<form class="inline" method="post" action="{base}/ksef-reject"><button type="submit">KSeF reject</button></form> '
        f'<form class="inline" method="post" action="{base}/delete" '
        ">"
        f'<button type="submit" onclick="return confirm(\'Delete this document?\')">Delete</button></form>'
    )


def _documents(docs: list[dict[str, Any]]) -> str:
    if not docs:
        return "<p>No documents stored.</p>"
    head = ("<tr><th>Id</th><th>Number</th><th>Type</th><th>Contractor</th><th>NIP</th><th>Issued</th>"
            "<th>Due</th><th>Gross</th><th>Paid</th><th>Buckets</th><th>KSeF</th><th>Created by</th>"
            "<th>Sent</th><th>Actions</th></tr>")
    rows = []
    for d in docs:
        marker = ' <span class="marker">deleted</span>' if d["deleted"] else ""
        ksef = " ".join(x for x in (d["KsefStatus"], d["KsefNumer"]) if x)
        rows.append(
            f'<tr class="{"deleted" if d["deleted"] else ""}">'
            f"<td>{_e(d['FakturaId'])}{marker}</td><td>{_e(d['PelnyNumer'])}</td><td>{_e(d['Rodzaj'])}</td>"
            f"<td>{_e(d['KontrahentNazwa'])}</td><td>{_e(d['NIPKontrahenta'])}</td>"
            f"<td>{_e(d['DataWystawienia'])}</td><td>{_e(d['TerminPlatnosci'])}</td>"
            f"<td>{d['Brutto']:.2f}</td><td>{d['Zaplacono']:.2f}</td>"
            f"<td>{_e(', '.join(d['statuses']))}</td><td>{_e(ksef)}</td><td>{_e(d['created_by'])}</td>"
            f"<td>{_e(d['CzyWyslano'])}</td><td>{_doc_buttons(d)}</td></tr>"
        )
    return f"<table>{head}{''.join(rows)}</table>"


def _contractors(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p>No contractors stored.</p>"
    head = "<tr><th>Identifier</th><th>Name</th><th>NIP</th><th>Email</th><th>City</th></tr>"
    rows = "".join(
        f"<tr><td>{_e(c.get('Identyfikator'))}</td><td>{_e(c.get('Nazwa'))}</td><td>{_e(c.get('NIP'))}</td>"
        f"<td>{_e(c.get('Email'))}</td><td>{_e(c.get('Miejscowosc'))}</td></tr>"
        for c in items
    )
    return f"<table>{head}{rows}</table>"


def _mail(items: list[dict[str, Any]], port: int, reject: bool) -> str:
    port_line = f"<p>SMTP listener on port {_e(port)}</p>" if port else "<p>SMTP listener off</p>"
    current = "true" if reject else "false"
    options = "".join(
        f'<option value="{v}"{" selected" if v == current else ""}>{v}</option>' for v in ("true", "false")
    )
    form = _switch_form("mail-reject", "Reject mail (answer DATA with 554)", current,
                        f'<label>value <select name="value">{options}</select></label>')
    if not items:
        table = "<p>The mailbox is empty.</p>"
    else:
        head = ("<tr><th>Received</th><th>From</th><th>To</th><th>Bcc</th><th>Subject</th>"
                "<th>Attachments</th><th>Raw</th></tr>")
        rows = "".join(
            f"<tr><td>{_e(m['received_at'])}</td><td>{_e(m['from'])}</td><td>{_e(', '.join(m['to']))}</td>"
            f"<td>{_e(', '.join(m['bcc']))}</td><td>{_e(m['subject'])}</td><td>{_e(m['attachments'])}</td>"
            f'<td><a href="/__mock/mail/{_e(m["id"])}/raw">raw</a></td></tr>'
            for m in items
        )
        table = f"<table>{head}{rows}</table>"
    clear = (
        '<form method="post" action="/__mock/ui/mail/clear">'
        '<button type="submit">Clear mailbox</button></form>'
    )
    return port_line + form + table + clear


def render_page(data: dict[str, Any], msg: str | None = None, error: str | None = None) -> str:
    s = data["settings"]
    c = data["counters"]
    flash = ""
    if error:
        flash += f'<div class="flash error" role="alert">{_e(error)}</div>'
    if msg:
        flash += f'<div class="flash msg" role="status">{_e(msg)}</div>'
    body = (
        f"<h1>{_e(data['service'])} <small>v{_e(data['version'])}</small></h1>"
        '<nav><a href="/docs">API docs (/docs)</a><a href="/health">/health</a>'
        '<a href="/__mock/status.json">/__mock/status.json</a>'
        f'<a href="{_e(README_URL)}">Readme</a></nav>'
        f"{flash}"
        "<h2>Settings</h2>"
        + _kv([
            ("User", s["user"]), ("Base URL", s["base_url"]), ("Strict auth", s["strict_auth"]),
            ("Rate limit", s["rate_limit"]), ("Strict list window", s["strict_list_window"]),
            ("Test endpoints", s["test_endpoints"]),
        ])
        + "<h2>Persistence</h2>" + _persistence(data["persistence"])
        + "<h2>Counters</h2>"
        + _kv([
            ("Calls total", c["calls_total"]), ("Calls this minute", c["calls_this_minute"]),
            ("Limit granted", c["limit_granted"]),
        ])
        + "<h2>Switches</h2>" + _switches(data["switches"])
        + "<h2>Seed</h2>" + _seed_form()
        + "<h2>Documents</h2>" + _documents(data["documents"])
        + "<h2>Contractors</h2>" + _contractors(data["contractors"])
        + "<h2>Mail</h2>" + _mail(data["mail"], s["smtp_port"], data["switches"]["mail_reject"])
        + "<h2>Reset</h2>"
        "<p>Resets documents, contractors, switches, counters, the stored state, the mailbox and the mail reject switch.</p>"
        '<form method="post" action="/__mock/ui/reset">'
        '<button type="submit" onclick="return confirm(\'Reset all mock state?\')">Reset everything</button></form>'
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>iFirma mock status</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )
