#!/usr/bin/env python3
"""
License-header checker/adder for the iFirma mock (test stack).

The mock ships under the MIT license, independently of the PrestaShop module it
tests. This script verifies that every Python source file carries an MIT notice
(the default --check mode) and can insert it where missing (--add).

    python tools/check_license_headers.py            # report files missing the header
    python tools/check_license_headers.py --add      # insert the header where missing
    python tools/check_license_headers.py FILE...    # only look at the given files

Check mode also fails on any file containing a string from FORBIDDEN (known
header typos; none are listed for the mock, the module's copy lists its own).

Exit status is 0 when every file has the header and none contains a FORBIDDEN
string, and 1 otherwise, so the default mode drops straight into CI or a
pre-commit hook. The file list comes from `git ls-files`, so ignored paths
(e.g. .venv) are never touched.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Per-repo policy — the only part that differs from the module's copy.
# --------------------------------------------------------------------------- #

# A file counts as compliant if any of these strings appears near its top.
MARKERS = (
    "SPDX-License-Identifier: MIT",
    "MIT License",
)

# The notice text, without any comment syntax; rendered per file type below.
NOTICE = [
    "SPDX-License-Identifier: MIT",
    "Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>",
    "See the LICENCE file in this directory for the full MIT terms.",
]

# Which tracked files to police.
EXTENSIONS = {".py"}

# Strings that must never appear in a policed file (known header typos), so a
# copy-pasted bad header fails the check instead of spreading.
FORBIDDEN = ()

# --------------------------------------------------------------------------- #
# Engine — identical in both repos.
# --------------------------------------------------------------------------- #

# (opening line, line prefix, closing line); None means "no delimiter".
COMMENT_STYLES = {
    ".php": ("/**", " * ", " */"),
    ".css": ("/*", " * ", " */"),
    ".js": ("/*", " * ", " */"),
    ".tpl": ("{*", " * ", " *}"),
    ".py": (None, "# ", None),
}


def render_header(ext: str) -> list[str]:
    open_, prefix, close = COMMENT_STYLES[ext]
    lines: list[str] = []
    if open_:
        lines.append(open_)
    for line in NOTICE:
        lines.append((prefix + line).rstrip())
    if close:
        lines.append(close)
    return lines


def insert_index(ext: str, lines: list[str]) -> int:
    """Line index at which the header should go, after any required preamble."""
    i = 0
    n = len(lines)
    if ext == ".py":
        if i < n and lines[i].startswith("#!"):
            i += 1
        if i < n and re.match(r"#.*coding[:=]", lines[i]):
            i += 1
        return i
    if ext == ".php":
        if i < n and lines[i].lstrip().startswith("<?php"):
            i += 1
            if i < n and lines[i].lstrip().startswith("declare("):
                i += 1
        return i
    return 0  # css / js / tpl -> very top


def read_file(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    eol = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return text, eol


def write_file(path: Path, text: str, eol: str) -> None:
    if eol != "\n":
        text = text.replace("\n", eol)
    path.write_bytes(text.encode("utf-8"))


def has_marker(text: str) -> bool:
    head = "\n".join(text.split("\n")[:60])
    return any(marker in head for marker in MARKERS)


def forbidden_hits(path: Path, text: str) -> list[str]:
    # This script lists the forbidden strings, so it never counts as tainted.
    if path.resolve() == Path(__file__).resolve():
        return []
    return [s for s in FORBIDDEN if s in text]


def add_header(path: Path, ext: str) -> bool:
    text, eol = read_file(path)
    if has_marker(text):
        return False
    had_trailing_nl = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines = lines[:-1]
    idx = insert_index(ext, lines)
    header = render_header(ext)
    tail = lines[idx:]
    separator = [] if (not tail or tail[0] == "") else [""]
    new_lines = lines[:idx] + header + separator + tail
    new_text = "\n".join(new_lines)
    if had_trailing_nl and not new_text.endswith("\n"):
        new_text += "\n"
    write_file(path, new_text, eol)
    return True


def repo_root() -> Path:
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=here, capture_output=True, check=True,
        )
        return Path(out.stdout.decode().strip())
    except Exception:
        return here.parent


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root, capture_output=True, check=True,
        )
        names = out.stdout.decode().split("\0")
    except Exception:
        names = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    files = []
    for name in names:
        if not name:
            continue
        path = root / name
        if path.suffix.lower() in EXTENSIONS and path.is_file():
            files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--add", "--fix", action="store_true", dest="add",
        help="insert the license header into files that lack it",
    )
    parser.add_argument(
        "paths", nargs="*",
        help="explicit files to check (default: all tracked source files)",
    )
    args = parser.parse_args()

    root = repo_root()
    if args.paths:
        files = [Path(p).resolve() for p in args.paths]
    else:
        files = tracked_files(root)

    missing: list[Path] = []
    changed: list[Path] = []
    tainted: list[tuple[Path, list[str]]] = []
    scanned = 0
    for path in files:
        ext = path.suffix.lower()
        if ext not in EXTENSIONS or not path.exists():
            continue
        scanned += 1
        text, _ = read_file(path)
        hits = forbidden_hits(path, text)
        if hits:
            tainted.append((path, hits))
        if has_marker(text):
            continue
        if args.add:
            if add_header(path, ext):
                changed.append(path)
        else:
            missing.append(path)

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    if args.add:
        for path in changed:
            print(f"added header: {rel(path)}")
        print(f"\n{len(changed)} file(s) updated, {scanned} scanned.")
        return 0

    for path in missing:
        print(f"missing header: {rel(path)}")
    for path, hits in tainted:
        print(f"forbidden text {', '.join(repr(h) for h in hits)}: {rel(path)}")
    if missing:
        print(f"\n{len(missing)} of {scanned} file(s) lack the license header.")
        print("Run: python tools/check_license_headers.py --add")
    if tainted:
        print(f"\n{len(tainted)} of {scanned} file(s) contain forbidden text (see FORBIDDEN).")
    if missing or tainted:
        return 1
    print(f"All {scanned} source file(s) carry the license header.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
