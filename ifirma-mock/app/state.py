# SPDX-License-Identifier: MIT
# Copyright (c) 2026 SCM Jakub Berechowski <admin@seccodesmith.pl>
# See the LICENCE file in this directory for the full MIT terms.

"""Optional persistence of the mock's state in one versioned JSON file.

With an empty ``state_file`` setting nothing happens and nothing is written (memory only).
With a path, ``start`` loads the file (tolerantly) and registers ``persist`` as the store's
change listener, so the file is rewritten atomically after every change.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import store

FORMAT_VERSION = 1
logger = logging.getLogger("ifirma-mock")
if not logger.handlers:
    # uvicorn configures only its own loggers, so without a handler the INFO lines ("Clean start",
    # "loaded ...") would never reach `docker compose logs`. Same format as uvicorn's.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_path = ""
_clean_start = False
_loaded_from_file = False
_last_write: str | None = None


def _quarantine(path: str, reason: str) -> None:
    target = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        os.replace(path, target)
    except OSError as exc:
        logger.error("State file %s is unusable (%s) and could not be moved aside: %s", path, reason, exc)
        return
    logger.warning("State file %s is unusable (%s); moved to %s and starting empty", path, reason, target)


def _load(path: str) -> bool:
    """Restore the store from `path`; True when a file was loaded."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        _quarantine(path, f"not readable JSON: {exc}")
        return False
    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        version = data.get("version") if isinstance(data, dict) else None
        _quarantine(path, f"unknown format version {version!r}")
        return False
    try:
        store.restore(data)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        store.reset_all()
        _quarantine(path, f"invalid content: {exc}")
        return False
    return True


def start(settings: Any) -> None:
    """Prepare persistence for one application start; safe to call repeatedly."""
    global _path, _clean_start, _loaded_from_file, _last_write
    store.on_change(None)
    _path = ""
    _clean_start = False
    _loaded_from_file = False
    _last_write = None
    path = str(getattr(settings, "state_file", "") or "")
    if not path:
        return
    _path = path
    _clean_start = bool(getattr(settings, "clean_start", False))
    store.reset_all()
    if _clean_start:
        try:
            os.remove(path)
            logger.info("Clean start: deleted state file %s", path)
        except FileNotFoundError:
            logger.info("Clean start: no state file %s to delete", path)
        except OSError as exc:
            logger.error("Clean start: could not delete state file %s: %s", path, exc)
    else:
        _loaded_from_file = _load(path)
    store.on_change(persist)
    persist(store.snapshot())


def persist(snapshot: dict[str, Any]) -> None:
    """Write the snapshot atomically; a failure is logged and swallowed."""
    global _last_write
    if not _path:
        return
    saved_at = datetime.now(timezone.utc).isoformat()
    document = {"version": FORMAT_VERSION, "saved_at": saved_at, **snapshot}
    tmp = _path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, _path)
    except OSError as exc:
        logger.error("Could not write state file %s: %s", _path, exc)
        return
    _last_write = saved_at


def info() -> dict[str, Any]:
    return {
        "mode": "file" if _path else "memory",
        "path": _path,
        "last_write": _last_write,
        "clean_start": _clean_start,
        "loaded_from_file": _loaded_from_file,
    }
