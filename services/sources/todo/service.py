#!/usr/bin/env python3
"""
BeoSound 5c — To-do list with tick-off (TO-DO on the arc, HUSKELISTE in Danish).

The list is edited from a phone, on a small web page this service serves at
http://<device>:8793/, and shown on the arc under TO-DO, where GO ticks an
item off and "Clear ticked" removes the ticked ones.

Both the page and the arc view follow the device's "language" setting; with
"auto" the page follows the phone's own language (Accept-Language), just as
the arc view follows the browser's. The API answers errors with short codes
that the page translates.

Like every other service on the device the page has no login, so it is meant
for the home network only — do not port-forward 8793. What it does guard
against is the browser-borne attacks a home network is still open to (see
todo_security.py): requests must be addressed to this device (DNS rebinding),
may not come from another site (Origin), and changes need a small JSON body.
The page ships a strict CSP and never renders list text as HTML.

The page and its API live together on this one port so that access from
outside can later be added by putting exactly this port behind an
authenticating tunnel, without exposing anything else on the device.

Storage: ~/.beosound5c_todo.json (0600, rewritten atomically). Override with
the BEO_TODO_FILE environment variable.

Port: 8793
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
from todo_security import PAGE_CSP, HostPolicy, apply_security_headers  # noqa: E402
from todo_store import NotFound, TodoError, TodoStore  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TODO] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_STORE_PATH = os.path.join(os.path.expanduser("~"), ".beosound5c_todo.json")
PHONE_DIR = os.path.join(HERE, "phone")
PHONE_ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "application/javascript"),
    "/app.css": ("app.css", "text/css"),
}
MAX_BODY_BYTES = 2048

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
        except TodoError as e:
            return web.json_response({"error": str(e)}, status=400)
        except NotFound:
            return web.json_response({"error": "not_found"}, status=404)
        except OSError:
            log.exception("Could not save the list")
            return web.json_response({"error": "save_failed"}, status=500)
    return wrapper


class TodoService(SourceBase):
    id = "todo"
    name = "To-do"
    port = 8793
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
        path = store_path or os.environ.get("BEO_TODO_FILE") or DEFAULT_STORE_PATH
        self._store = TodoStore(path)
        self._hosts = HostPolicy(host_resolver)
        self._assets = {}
        for route, (filename, content_type) in PHONE_ASSETS.items():
            with open(os.path.join(PHONE_DIR, filename), "rb") as f:
                self._assets[route] = (f.read(), content_type)
        if _PAGE_LANG_MARKER not in self._assets["/"][0]:
            raise RuntimeError(f"phone/index.html must start with {_PAGE_LANG_MARKER!r}")

    async def on_start(self):
        count = len(self._store.snapshot()["items"])
        log.info("To-do list with %d item(s); phone page on port %d", count, self.port)
        await self.register("available")

    # ── Routes ──

    def add_routes(self, app):
        app.middlewares.append(self._make_guard())
        for route in self._assets:
            app.router.add_get(route, self._handle_asset)
        app.router.add_get("/api/items", self._handle_list)
        app.router.add_post("/api/items", self._handle_add)
        app.router.add_patch("/api/items/{item_id}", self._handle_update)
        app.router.add_delete("/api/items/{item_id}", self._handle_delete)
        app.router.add_post("/api/clear-done", self._handle_clear_done)
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
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600",
        })

    @_api
    async def _handle_list(self, request):
        return web.json_response(self._store.snapshot())

    @_api
    async def _handle_add(self, request):
        data = await self._read_json(request, allowed={"text"})
        item, version = await asyncio.to_thread(self._store.add, data.get("text"))
        self._changed(version)
        return web.json_response({"item": item, "version": version}, status=201)

    @_api
    async def _handle_update(self, request):
        data = await self._read_json(request, allowed={"text", "done"})
        item, version = await asyncio.to_thread(
            self._store.update, request.match_info["item_id"],
            data.get("text"), data.get("done"))
        self._changed(version)
        return web.json_response({"item": item, "version": version})

    @_api
    async def _handle_delete(self, request):
        version = await asyncio.to_thread(self._store.delete, request.match_info["item_id"])
        self._changed(version)
        return web.json_response({"version": version})

    @_api
    async def _handle_clear_done(self, request):
        await self._read_json(request, allowed=set())
        removed, version = await asyncio.to_thread(self._store.clear_done)
        if removed:
            self._changed(version)
        return web.json_response({"removed": removed, "version": version})

    # ── Helpers ──

    @staticmethod
    async def _read_json(request, allowed: set[str]) -> dict:
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
        if set(data) - allowed:
            raise ApiError(400, "unknown_fields")
        return data

    def _changed(self, version: int) -> None:
        # Lets the arc view reload straight away instead of on next visit.
        self._spawn(self.broadcast("todo_update", {"version": version}),
                    name="todo-broadcast")

    # ── SourceBase hooks ──

    async def handle_status(self):
        items = self._store.snapshot()["items"]
        return {
            "source": self.id,
            "name": self.name,
            "item_count": len(items),
            "open_count": sum(1 for item in items if not item["done"]),
        }

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        # Arc and remote actions are handled by the view (softarc/todo.html),
        # which calls the guarded API above. /command answers any origin, so
        # it deliberately changes nothing.
        return {}


if __name__ == "__main__":
    service = TodoService()
    asyncio.run(service.run())
