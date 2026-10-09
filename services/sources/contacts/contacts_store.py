"""
Contacts store for the CONTACTS menu item (KONTAKTER in Danish).

The contacts live in one JSON file:

    {"contacts": [
        {"name": "Anna Jensen", "phone": "+45 12 34 56 78",
         "email": "anna@example.com", "address": "Vestergade 1, 8000 Aarhus",
         "note": "Nabo"}
    ]}

Only "name" is required. The file is read again whenever it changes on disk,
so it can be edited by hand without restarting the service. A missing file is
an empty list; a broken one keeps the last good list and is logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

log = logging.getLogger(__name__)

FIELDS = ("name", "phone", "email", "address", "note")
MAX_FIELD_CHARS = 200
MAX_CONTACTS = 1000


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_FIELD_CHARS]


def normalise(raw) -> list[dict]:
    """Validate and sort a parsed file. Entries without a name are dropped."""
    entries = raw.get("contacts") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("expected a list of contacts")
    contacts = []
    for entry in entries[:MAX_CONTACTS]:
        if not isinstance(entry, dict):
            continue
        contact = {field: _clean(entry.get(field)) for field in FIELDS}
        if not contact["name"]:
            continue
        # Stable across reloads as long as the contact itself is unchanged.
        digest = hashlib.sha1(
            "\x1f".join(contact[f] for f in FIELDS).encode("utf-8")).hexdigest()
        contact["id"] = digest[:16]
        contacts.append(contact)
    contacts.sort(key=lambda c: c["name"].casefold())
    return contacts


class ContactsStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._contacts: list[dict] = []

    def contacts(self) -> list[dict]:
        with self._lock:
            self._reload_if_changed()
            return [dict(c) for c in self._contacts]

    def _reload_if_changed(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            self._mtime, self._contacts = None, []
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self._contacts = normalise(json.load(f))
        except (OSError, ValueError) as e:
            log.warning("Could not read %s (%s) — keeping the last good list", self.path, e)
        self._mtime = mtime
