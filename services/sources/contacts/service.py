#!/usr/bin/env python3
"""
BeoSound 5c — Contacts (CONTACTS on the arc, KONTAKTER in Danish).

The contacts are edited from a phone, on a small web page this service serves
at http://<device>:8794/ (linked from the CONTACTS entry on the config page),
and shown on the arc under CONTACTS — with the contact's picture on its tile
when one has been added — where GO opens the details (phone, e-mail,
address, birthday, note).

Both the page and the arc view follow the device's "language" setting; with
"auto" the page follows the phone's own language (Accept-Language), just as
the arc view follows the browser's. The API answers errors with short codes
that the page translates.

Like every other service on the device the page has no login, so it is meant
for the home network only — do not port-forward 8794. What it does guard
against is the browser-borne attacks a home network is still open to (see
contacts_security.py): requests must be addressed to this device (DNS
rebinding), may not come from another site (Origin), and changes need a small
JSON body. Contacts are personal data, so unlike the other sources this one
never answers another site's page with a CORS grant — only the device's own UI
and the phone page may read them. The page ships a strict CSP and never
renders contact text as HTML. Uploaded pictures are re-encoded before they are
stored (contacts_photo.py), which also strips the phone's EXIF/GPS data.

Storage: ~/.beosound5c_contacts.json (0600, rewritten atomically) and the
pictures in ~/.beosound5c_contacts_photos/. Override with the
BEO_CONTACTS_FILE environment variable (the photos directory follows it).

Port: 8794
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import sys

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from lib.config import cfg  # noqa: E402
from lib.source_base import SourceBase  # noqa: E402
from contacts_photo import MAX_UPLOAD_BYTES, PhotoError, normalise_photo  # noqa: E402
from contacts_security import PAGE_CSP, HostPolicy, apply_security_headers  # noqa: E402
from contacts_store import FIELDS, ContactError, ContactsStore, NotFound  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CONTACTS] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_STORE_PATH = os.path.join(os.path.expanduser("~"), ".beosound5c_contacts.json")
PHONE_DIR = os.path.join(HERE, "phone")
PHONE_ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "application/javascript"),
    "/app.css": ("app.css", "text/css"),
}
MAX_BODY_BYTES = 4096

SUPPORTED_LANGUAGES = ("da", "en")
_PAGE_LANG_MARKER = b'<html lang="en">'


def page_language(configured, accept_language: str) -> str:
    """Language for the phone page: the device setting when it names one the
    page has, otherwise the phone's first supported preference, otherwise
    English — the same rule the arc views apply with navigator.language."""
    configured = str(configured or "auto").lower()
    if configured in SUPPORTED_LANGUAGES:
        return configured
    for part in (accept_language or "").split(","):
        tag = part.split(";", 1)[0].strip().lower().split("-", 1)[0]
        if tag in SUPPORTED_LANGUAGES:
            return tag
    return "en"


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _api(handler):
    """Turn expected failures into short JSON error codes without internals."""
    @functools.wraps(handler)
    async def wrapper(self, request):
        try:
            return await handler(self, request)
        except ApiError as e:
            return web.json_response({"error": e.code}, status=e.status)
        except ContactError as e:
            return web.json_response({"error": str(e)}, status=400)
        except PhotoError as e:
            status = 413 if str(e) == "photo_too_large" else 400
            return web.json_response({"error": str(e)}, status=status)
        except NotFound:
            return web.json_response({"error": "not_found"}, status=404)
        except OSError:
            log.exception("Could not save the contacts")
            return web.json_response({"error": "save_failed"}, status=500)
    return wrapper


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


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

    def __init__(self, store_path: str | None = None, host_resolver=None):
        super().__init__()
        path = store_path or os.environ.get("BEO_CONTACTS_FILE") or DEFAULT_STORE_PATH
        self._store = ContactsStore(path)
        self._hosts = HostPolicy(host_resolver)
        self._assets = {}
        for route, (filename, content_type) in PHONE_ASSETS.items():
            with open(os.path.join(PHONE_DIR, filename), "rb") as f:
                self._assets[route] = (f.read(), content_type)
        if _PAGE_LANG_MARKER not in self._assets["/"][0]:
            raise RuntimeError(f"phone/index.html must start with {_PAGE_LANG_MARKER!r}")

    async def on_start(self):
        count = len(self._store.snapshot()["contacts"])
        log.info("%d contact(s) in %s; phone page on port %d", count, self._store.path, self.port)
        await self.register("available")

    # ── Routes ──

    def add_routes(self, app):
        app.middlewares.append(self._make_guard())
        for route in self._assets:
            app.router.add_get(route, self._handle_asset)
        app.router.add_get("/api/contacts", self._handle_list)
        app.router.add_post("/api/contacts", self._handle_add)
        app.router.add_patch("/api/contacts/{contact_id}", self._handle_update)
        app.router.add_delete("/api/contacts/{contact_id}", self._handle_delete)
        app.router.add_get("/api/contacts/{contact_id}/photo", self._handle_photo_get)
        app.router.add_put("/api/contacts/{contact_id}/photo", self._handle_photo_put)
        app.router.add_delete("/api/contacts/{contact_id}/photo", self._handle_photo_delete)
        app.router.add_route("OPTIONS", "/api/{tail:.*}", self._handle_preflight)

    def _make_guard(self):
        hosts = self._hosts

        @web.middleware
        async def guard(request, handler):
            host = request.headers.get("Host", "")
            if not hosts.knows(host) and hosts.refresh_due():
                await asyncio.to_thread(hosts.refresh)
            if not hosts.knows(host):
                log.warning("Rejected %s %s: unknown Host %r", request.method, request.path, host)
                return self._reject("unknown_host")

            origin = request.headers.get("Origin", "").strip()
            if not hosts.origin_ok(origin):
                log.warning("Rejected %s %s: cross-site Origin %r",
                            request.method, request.path, origin)
                return self._reject("cross_origin")

            try:
                response = await handler(request)
            except web.HTTPException as exc:
                apply_security_headers(exc, origin)
                raise
            apply_security_headers(response, origin)
            return response

        return guard

    @staticmethod
    def _reject(code: str):
        response = web.json_response({"error": code}, status=403)
        apply_security_headers(response, "")
        return response

    async def _handle_asset(self, request):
        body, content_type = self._assets[request.path]
        headers = {
            "Content-Security-Policy": PAGE_CSP,
            "X-Frame-Options": "DENY",
        }
        if request.path == "/":
            lang = page_language(cfg("language", default="auto"),
                                 request.headers.get("Accept-Language", ""))
            body = body.replace(_PAGE_LANG_MARKER, f'<html lang="{lang}">'.encode(), 1)
            headers["Content-Language"] = lang
            headers["Vary"] = "Accept-Language"
        response = web.Response(body=body, content_type=content_type, charset="utf-8")
        response.headers.update(headers)
        return response

    async def _handle_preflight(self, request):
        return web.Response(status=204, headers={
            "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600",
        })

    @_api
    async def _handle_list(self, request):
        return web.json_response(self._store.snapshot())

    @_api
    async def _handle_add(self, request):
        data = await self._read_json(request)
        contact, version = await asyncio.to_thread(self._store.add, data)
        self._changed(version)
        return web.json_response({"contact": contact, "version": version}, status=201)

    @_api
    async def _handle_update(self, request):
        data = await self._read_json(request)
        contact, version = await asyncio.to_thread(
            self._store.update, request.match_info["contact_id"], data)
        self._changed(version)
        return web.json_response({"contact": contact, "version": version})

    @_api
    async def _handle_delete(self, request):
        version = await asyncio.to_thread(self._store.delete, request.match_info["contact_id"])
        self._changed(version)
        return web.json_response({"version": version})

    @_api
    async def _handle_photo_get(self, request):
        contact_id = request.match_info["contact_id"]
        token = self._store.photo_token(contact_id)
        if not token:
            raise NotFound(contact_id)
        try:
            body = await asyncio.to_thread(_read_file, self._store.photo_path(contact_id))
        except FileNotFoundError:
            raise NotFound(contact_id) from None
        # The views ask for ?v=<token>, which changes with every new picture.
        return web.Response(body=body, content_type="image/jpeg", headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    @_api
    async def _handle_photo_put(self, request):
        if not request.content_type.startswith("image/"):
            raise ApiError(415, "not_image")
        length = request.content_length
        if length is None:
            raise ApiError(411, "length_required")
        if length > MAX_UPLOAD_BYTES:
            raise ApiError(413, "photo_too_large")
        contact_id = request.match_info["contact_id"]
        self._store.photo_token(contact_id)        # 404 before reading the body
        try:
            data = await request.content.readexactly(length)
        except asyncio.IncompleteReadError:
            raise ApiError(400, "photo_invalid") from None
        jpeg = await asyncio.to_thread(normalise_photo, data)
        contact, version = await asyncio.to_thread(self._store.set_photo, contact_id, jpeg)
        self._changed(version)
        return web.json_response({"contact": contact, "version": version})

    @_api
    async def _handle_photo_delete(self, request):
        contact, version = await asyncio.to_thread(
            self._store.clear_photo, request.match_info["contact_id"])
        self._changed(version)
        return web.json_response({"contact": contact, "version": version})

    # ── Helpers ──

    @staticmethod
    async def _read_json(request) -> dict:
        if request.content_type != "application/json":
            raise ApiError(415, "not_json")
        length = request.content_length
        if length is None:
            raise ApiError(411, "length_required")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "too_large")
        try:
            raw = await request.content.readexactly(length)
            data = json.loads(raw.decode("utf-8"))
        except (asyncio.IncompleteReadError, UnicodeDecodeError, ValueError):
            raise ApiError(400, "invalid_json") from None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json")
        if set(data) - set(FIELDS):
            raise ApiError(400, "unknown_fields")
        return data

    def _changed(self, version: int) -> None:
        # Lets the arc view reload straight away instead of on next visit.
        self._spawn(self.broadcast("contacts_update", {"version": version}),
                    name="contacts-broadcast")

    # ── SourceBase hooks ──

    async def handle_status(self):
        return {"source": self.id, "name": self.name,
                "contact_count": len(self._store.snapshot()["contacts"])}

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        # Navigation is handled by the view (softarc/contacts.html). /command
        # answers any origin, so it deliberately changes nothing.
        return {}


if __name__ == "__main__":
    service = ContactsService()
    asyncio.run(service.run())
