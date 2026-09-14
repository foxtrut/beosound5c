"""Storage for the to-do list (TO-DO on the arc, HUSKELISTE in Danish).

One small JSON file, rewritten atomically on every change: the new state goes
to a private temp file in the same directory, is fsynced, and replaces the old
file in one rename — so a power cut leaves either the old list or the new one,
never half of each. The in-memory list only changes once the write succeeded.

Everything that comes from a phone passes through :func:`clean_text` before it
is stored, and a file that fails to parse is set aside rather than trusted.
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

MAX_TEXT = 120
MAX_ITEMS = 300

_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Bidirectional overrides and isolates can make a harmless item render as
# something else ("gnirts" read backwards). Zero-width joiners stay: emoji
# sequences need them.
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


class TodoError(ValueError):
    """Rejected input. ``str(error)`` is a short code (``"empty"``,
    ``"too_long"``, ``"full"``…) that the phone page translates."""


class NotFound(KeyError):
    """No item with that id (or the id is not one this store could have made)."""


def clean_text(raw) -> str:
    """Normalise an item's text, or raise :class:`TodoError`."""
    if not isinstance(raw, str):
        raise TodoError("missing")
    if len(raw) > MAX_TEXT * 4:
        raise TodoError("too_long")
    text = unicodedata.normalize("NFC", raw)
    text = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch
        for ch in text if ch not in _BIDI_CONTROLS
    )
    text = " ".join(text.split())
    if not text:
        raise TodoError("empty")
    if len(text) > MAX_TEXT:
        raise TodoError("too_long")
    return text


def _public(item: dict) -> dict:
    return {"id": item["id"], "text": item["text"], "done": item["done"]}


def _loaded_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        return None
    if not isinstance(raw.get("done"), bool):
        return None
    try:
        text = clean_text(raw.get("text"))
    except TodoError:
        return None
    return {"id": item_id, "text": text, "done": raw["done"]}


class TodoStore:
    def __init__(self, path: str):
        self._path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._version, self._items = self._load()

    # ── Reading ──

    def snapshot(self) -> dict:
        """The list as the views show it: open items first, then ticked ones,
        each group in the order it was added."""
        with self._lock:
            ordered = sorted(self._items, key=lambda item: item["done"])
            return {"version": self._version, "items": [_public(i) for i in ordered]}

    # ── Changes — each returns (result, new version) ──

    def add(self, text) -> tuple[dict, int]:
        text = clean_text(text)

        def apply(items):
            if len(items) >= MAX_ITEMS:
                raise TodoError("full")
            item = {"id": uuid.uuid4().hex, "text": text, "done": False}
            items.append(item)
            return _public(item)

        return self._mutate(apply)

    def update(self, item_id, text=None, done=None) -> tuple[dict, int]:
        if text is None and done is None:
            raise TodoError("nothing_to_change")
        if text is not None:
            text = clean_text(text)
        if done is not None and not isinstance(done, bool):
            raise TodoError("invalid_done")

        def apply(items):
            item = self._find(items, item_id)
            if text is not None:
                item["text"] = text
            if done is not None:
                item["done"] = done
            return _public(item)

        return self._mutate(apply)

    def delete(self, item_id) -> int:
        def apply(items):
            items.remove(self._find(items, item_id))

        return self._mutate(apply)[1]

    def clear_done(self) -> tuple[int, int]:
        with self._lock:
            if not any(item["done"] for item in self._items):
                return 0, self._version

        def apply(items):
            before = len(items)
            items[:] = [item for item in items if not item["done"]]
            return before - len(items)

        return self._mutate(apply)

    # ── Internals ──

    @staticmethod
    def _find(items, item_id) -> dict:
        if isinstance(item_id, str) and _ID_RE.match(item_id):
            for item in items:
                if item["id"] == item_id:
                    return item
        raise NotFound(item_id)

    def _mutate(self, apply):
        with self._lock:
            items = [dict(item) for item in self._items]
            result = apply(items)
            version = self._version + 1
            self._write(version, items)
            self._items, self._version = items, version
            return result, version

    def _write(self, version: int, items: list) -> None:
        directory = os.path.dirname(self._path)
        fd, tmp = tempfile.mkstemp(prefix=".todo-", suffix=".tmp", dir=directory)  # 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": version, "items": items}, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self) -> tuple[int, list]:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0, []
        except (OSError, ValueError) as e:
            self._set_aside(e)
            return 0, []
        if not isinstance(data, dict):
            self._set_aside("top level is not an object")
            return 0, []

        items, seen = [], set()
        raw_items = data.get("items")
        for raw in raw_items if isinstance(raw_items, list) else []:
            item = _loaded_item(raw)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        dropped = (len(raw_items) if isinstance(raw_items, list) else 0) - len(items)
        if dropped:
            log.warning("Ignored %d invalid item(s) in %s", dropped, self._path)

        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            version = 0
        return version, items[:MAX_ITEMS]

    def _set_aside(self, reason) -> None:
        target = f"{self._path}.corrupt-{int(time.time())}"
        try:
            os.replace(self._path, target)
            log.error("Could not read %s (%s) — moved it to %s and started empty",
                      self._path, reason, target)
        except OSError as e:
            log.error("Could not read %s (%s) and could not move it aside: %s",
                      self._path, reason, e)
