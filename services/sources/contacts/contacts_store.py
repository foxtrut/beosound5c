"""Storage for the contacts (CONTACTS on the arc, KONTAKTER in Danish).

One small JSON file, edited from the phone page and rewritten atomically on
every change: the new state goes to a private temp file in the same directory,
is fsynced, and replaces the old file in one rename — so a power cut leaves
either the old list or the new one, never half of each. The in-memory list
only changes once the write succeeded.

    {"version": 3, "contacts": [
        {"id": "<32 hex>", "name": "Anna Jensen", "phone": "+45 12 34 56 78",
         "email": "anna@example.com", "address": "Vestergade 1, 8000 Aarhus",
         "note": "Nabo"}
    ]}

Only "name" is required. Everything that comes from a phone passes through
:func:`clean_field` before it is stored, and a file that fails to parse is set
aside rather than trusted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid

log = logging.getLogger(__name__)

# Field → longest text accepted (after whitespace is collapsed).
FIELD_LIMITS = {
    "name": 80,
    "phone": 40,
    "email": 120,
    "address": 160,
    "note": 200,
}
FIELDS = tuple(FIELD_LIMITS)
MAX_CONTACTS = 500

_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Bidirectional overrides and isolates can make a harmless text render as
# something else ("gnirts" read backwards). Zero-width joiners stay: emoji
# sequences need them.
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


class ContactError(ValueError):
    """Rejected input. ``str(error)`` is a short code (``"name_missing"``,
    ``"too_long"``, ``"full"``…) that the phone page translates."""


class NotFound(KeyError):
    """No contact with that id (or the id is not one this store could have made)."""


def clean_field(field: str, raw) -> str:
    """Normalise one field's text ("" when absent), or raise :class:`ContactError`."""
    limit = FIELD_LIMITS[field]
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ContactError("invalid")
    if len(raw) > limit * 4:
        raise ContactError("too_long")
    text = unicodedata.normalize("NFC", raw)
    text = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch
        for ch in text if ch not in _BIDI_CONTROLS
    )
    text = " ".join(text.split())
    if len(text) > limit:
        raise ContactError("too_long")
    return text


def clean_contact(raw: dict, partial: bool = False) -> dict:
    """Clean the given fields. With ``partial``, only the fields present are
    returned (for an update); otherwise every field is, and a name is required."""
    fields = [f for f in FIELDS if f in raw] if partial else FIELDS
    cleaned = {f: clean_field(f, raw.get(f)) for f in fields}
    if "name" in cleaned and not cleaned["name"]:
        raise ContactError("name_missing")
    return cleaned


def _sort_key(contact: dict):
    return (contact["name"].casefold(), contact["id"])


def _loaded_contact(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        contact = clean_contact(raw)
    except ContactError:
        return None
    item_id = raw.get("id")
    contact["id"] = item_id if isinstance(item_id, str) and _ID_RE.match(item_id) else uuid.uuid4().hex
    return contact


class ContactsStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._version, self._contacts = self._load()

    # ── Reading ──

    def snapshot(self) -> dict:
        """All contacts, sorted by name."""
        with self._lock:
            return {"version": self._version,
                    "contacts": [dict(c) for c in sorted(self._contacts, key=_sort_key)]}

    # ── Changes — each returns (result, new version) ──

    def add(self, raw: dict) -> tuple[dict, int]:
        contact = clean_contact(raw)

        def apply(contacts):
            if len(contacts) >= MAX_CONTACTS:
                raise ContactError("full")
            contact["id"] = uuid.uuid4().hex
            contacts.append(contact)
            return dict(contact)

        return self._mutate(apply)

    def update(self, contact_id, raw: dict) -> tuple[dict, int]:
        changes = clean_contact(raw, partial=True)
        if not changes:
            raise ContactError("nothing_to_change")

        def apply(contacts):
            contact = self._find(contacts, contact_id)
            contact.update(changes)
            return dict(contact)

        return self._mutate(apply)

    def delete(self, contact_id) -> int:
        def apply(contacts):
            contacts.remove(self._find(contacts, contact_id))

        return self._mutate(apply)[1]

    # ── Internals ──

    @staticmethod
    def _find(contacts, contact_id) -> dict:
        if isinstance(contact_id, str) and _ID_RE.match(contact_id):
            for contact in contacts:
                if contact["id"] == contact_id:
                    return contact
        raise NotFound(contact_id)

    def _mutate(self, apply):
        with self._lock:
            contacts = [dict(c) for c in self._contacts]
            result = apply(contacts)
            version = self._version + 1
            self._write(version, contacts)
            self._contacts, self._version = contacts, version
            return result, version

    def _write(self, version: int, contacts: list) -> None:
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(prefix=".contacts-", suffix=".tmp", dir=directory)  # 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": version, "contacts": contacts}, f,
                          ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self) -> tuple[int, list]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0, []
        except (OSError, ValueError) as e:
            self._set_aside(e)
            return 0, []
        if not isinstance(data, dict):
            self._set_aside("top level is not an object")
            return 0, []

        contacts, seen = [], set()
        raw_contacts = data.get("contacts")
        for raw in raw_contacts if isinstance(raw_contacts, list) else []:
            contact = _loaded_contact(raw)
            if contact and contact["id"] not in seen:
                seen.add(contact["id"])
                contacts.append(contact)
        dropped = (len(raw_contacts) if isinstance(raw_contacts, list) else 0) - len(contacts)
        if dropped:
            log.warning("Ignored %d invalid contact(s) in %s", dropped, self.path)

        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            version = 0
        return version, contacts[:MAX_CONTACTS]

    def _set_aside(self, reason) -> None:
        target = f"{self.path}.corrupt-{int(time.time())}"
        try:
            os.replace(self.path, target)
            log.error("Could not read %s (%s) — moved it to %s and started empty",
                      self.path, reason, target)
        except OSError as e:
            log.error("Could not read %s (%s) and could not move it aside: %s",
                      self.path, reason, e)
