"""Storage for the contacts (CONTACTS on the arc, KONTAKTER in Danish).

One small JSON file, edited from the phone page and rewritten atomically on
every change: the new state goes to a private temp file in the same directory,
is fsynced, and replaces the old file in one rename — so a power cut leaves
either the old list or the new one, never half of each. The in-memory list
only changes once the write succeeded.

    {"version": 3, "contacts": [
        {"id": "<32 hex>", "name": "Anna Jensen", "phone": "+45 12 34 56 78",
         "email": "anna@example.com", "address": "Vestergade 1, 8000 Aarhus",
         "note": "Nabo", "birthday": "1980-03-12", "photo": "<16 hex>"}
    ]}

Only "name" is required. "birthday" is an ISO date (YYYY-MM-DD) or "".
"photo" is "" or a token that changes with every new picture — the picture
itself is a JPEG in the photos directory next to the file (see
:meth:`ContactsStore.photo_path`), and the token lets the views cache it. Everything that comes from a phone passes through
:func:`clean_field` before it is stored, and a file that fails to parse is set
aside rather than trusted.
"""

from __future__ import annotations

import json
import logging
import os
import datetime
import re
import secrets
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
# Text fields plus the birthday, which is validated as a date.
FIELDS = (*FIELD_LIMITS, "birthday")
_PHOTO_RE = re.compile(r"^[0-9a-f]{16}$")
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


def clean_birthday(raw, strict: bool = True) -> str:
    """An ISO date (YYYY-MM-DD) from 1900 on, or "". ``strict`` also refuses
    dates after today — only for input: the device can boot with an old date
    (no RTC), and a stored birthday must never be dropped because of that."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.strip()):
        raise ContactError("invalid_birthday")
    try:
        day = datetime.date.fromisoformat(raw.strip())
    except ValueError:
        raise ContactError("invalid_birthday") from None
    if day < datetime.date(1900, 1, 1) or (strict and day > datetime.date.today()):
        raise ContactError("invalid_birthday")
    return day.isoformat()


def clean_field(field: str, raw, strict: bool = True) -> str:
    """Normalise one field's text ("" when absent), or raise :class:`ContactError`."""
    if field == "birthday":
        return clean_birthday(raw, strict)
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


def clean_contact(raw: dict, partial: bool = False, strict: bool = True) -> dict:
    """Clean the given fields. With ``partial``, only the fields present are
    returned (for an update); otherwise every field is, and a name is required."""
    fields = [f for f in FIELDS if f in raw] if partial else FIELDS
    cleaned = {f: clean_field(f, raw.get(f), strict) for f in fields}
    if "name" in cleaned and not cleaned["name"]:
        raise ContactError("name_missing")
    return cleaned


def _sort_key(contact: dict):
    return (contact["name"].casefold(), contact["id"])


def _loaded_contact(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        contact = clean_contact(raw, strict=False)
    except ContactError:
        return None
    item_id = raw.get("id")
    contact["id"] = item_id if isinstance(item_id, str) and _ID_RE.match(item_id) else uuid.uuid4().hex
    photo = raw.get("photo")
    contact["photo"] = photo if isinstance(photo, str) and _PHOTO_RE.match(photo) else ""
    return contact


class ContactsStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.photos_dir = os.path.splitext(self.path)[0] + "_photos"
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
            contact["photo"] = ""
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

        version = self._mutate(apply)[1]
        self._remove_photo_file(contact_id)
        return version

    # ── Photos ──

    def photo_path(self, contact_id) -> str:
        """Where a contact's picture lives. Only ever built from a valid id."""
        if not (isinstance(contact_id, str) and _ID_RE.match(contact_id)):
            raise NotFound(contact_id)
        return os.path.join(self.photos_dir, f"{contact_id}.jpg")

    def photo_token(self, contact_id) -> str:
        with self._lock:
            return self._find(self._contacts, contact_id)["photo"]

    def set_photo(self, contact_id, jpeg: bytes) -> tuple[dict, int]:
        """Store a (already re-encoded) JPEG for the contact."""
        with self._lock:
            self._find(self._contacts, contact_id)
        target = self.photo_path(contact_id)
        os.makedirs(self.photos_dir, mode=0o700, exist_ok=True)
        self._atomic_write(target, lambda f: f.write(jpeg), binary=True)
        token = secrets.token_hex(8)

        def apply(contacts):
            contact = self._find(contacts, contact_id)
            contact["photo"] = token
            return dict(contact)

        return self._mutate(apply)

    def clear_photo(self, contact_id) -> tuple[dict, int]:
        def apply(contacts):
            contact = self._find(contacts, contact_id)
            contact["photo"] = ""
            return dict(contact)

        result = self._mutate(apply)
        self._remove_photo_file(contact_id)
        return result

    def _remove_photo_file(self, contact_id) -> None:
        try:
            os.unlink(self.photo_path(contact_id))
        except (FileNotFoundError, NotFound):
            pass
        except OSError as e:
            log.warning("Could not remove the photo of %s: %s", contact_id, e)

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
        self._atomic_write(self.path, lambda f: json.dump(
            {"version": version, "contacts": contacts}, f, ensure_ascii=False, indent=1))

    @staticmethod
    def _atomic_write(path: str, write, binary: bool = False) -> None:
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(prefix=".contacts-", suffix=".tmp", dir=directory)  # 0600
        try:
            with (os.fdopen(fd, "wb") if binary else os.fdopen(fd, "w", encoding="utf-8")) as f:
                write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
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
