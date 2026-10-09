#!/usr/bin/env python3
"""
BeoSound 5c — Contacts (CONTACTS on the arc, KONTAKTER in Danish).

Shows a list of contacts on the arc; GO on a contact opens its details
(phone, e-mail, address, note). This first version is read-only: the list is
the JSON file described in contacts_store.py, re-read whenever it changes.

Contacts are personal data, so unlike the other sources' endpoints
/api/contacts is only readable cross-origin by the device's own UI
(http://localhost), not by any web page a phone on the network happens to
open.

Storage: ~/.beosound5c_contacts.json. Override with the BEO_CONTACTS_FILE
environment variable.

Port: 8794
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from urllib.parse import urlsplit

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from lib.source_base import SourceBase  # noqa: E402
from contacts_store import ContactsStore  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CONTACTS] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_STORE_PATH = os.path.join(os.path.expanduser("~"), ".beosound5c_contacts.json")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def local_origin(origin: str) -> bool:
    """True for the device's own UI (any port on localhost)."""
    try:
        return urlsplit(origin).hostname in LOCAL_HOSTS
    except ValueError:
        return False


class ContactsService(SourceBase):
    id = "contacts"
    name = "Contacts"
    port = 8794
    player = "local"
    action_map = {
        "go": "select",
        "up": "up",
        "down": "down",
        "left": "back",
        "right": "select",
    }

    def __init__(self, store_path: str | None = None):
        super().__init__()
        path = store_path or os.environ.get("BEO_CONTACTS_FILE") or DEFAULT_STORE_PATH
        self._store = ContactsStore(path)

    async def on_start(self):
        count = len(await asyncio.to_thread(self._store.contacts))
        log.info("%d contact(s) in %s", count, self._store.path)
        await self.register("available")

    def add_routes(self, app):
        app.router.add_get("/api/contacts", self._handle_list)

    async def _handle_list(self, request):
        contacts = await asyncio.to_thread(self._store.contacts)
        response = web.json_response({"contacts": contacts})
        origin = request.headers.get("Origin", "")
        if local_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    # ── SourceBase hooks ──

    async def handle_status(self):
        contacts = await asyncio.to_thread(self._store.contacts)
        return {"source": self.id, "name": self.name, "contact_count": len(contacts)}

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        # Navigation is handled by the view (softarc/contacts.html).
        return {}


if __name__ == "__main__":
    service = ContactsService()
    asyncio.run(service.run())
