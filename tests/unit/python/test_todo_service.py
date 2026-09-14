"""Tests for the huskeliste service's HTTP surface: the phone page, the API,
and the guards that keep other web pages (and DNS rebinding) away from it."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sources.todo.service import TodoService
from todo_security import HostPolicy, hostname_of

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTS = {"localhost", "127.0.0.1", "::1", "beosound5c", "beosound5c.local", "192.168.0.62"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = TodoService(store_path=str(tmp_path / "todo.json"), host_resolver=lambda: set(HOSTS))
    svc.broadcasts = []

    async def fake_broadcast(event_type, data):
        svc.broadcasts.append((event_type, data))

    monkeypatch.setattr(svc, "broadcast", fake_broadcast)
    return svc


@pytest_asyncio.fixture
async def client(service):
    app = web.Application()
    service.add_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


async def _add(client, text, **headers):
    return await client.post("/api/items", json={"text": text}, headers=headers)


# ── Phone page ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phone_page_is_served_with_strict_headers(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"

    js = await client.get("/app.js")
    assert js.status == 200 and js.content_type == "application/javascript"


def test_list_text_is_never_rendered_as_html():
    """Items come from phones; both views must insert them as text."""
    phone_js = (REPO_ROOT / "services/sources/todo/phone/app.js").read_text()
    phone_html = (REPO_ROOT / "services/sources/todo/phone/index.html").read_text()
    arc_page = (REPO_ROOT / "web/softarc/todo.html").read_text()
    assert "innerHTML" not in phone_js and "insertAdjacentHTML" not in phone_js
    assert "innerHTML" not in arc_page
    assert "<script>" not in phone_html and "style=" not in phone_html, "CSP forbids inline code"


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_tick_rename_delete_roundtrip(client, service):
    resp = await _add(client, "  Mælk ")
    assert resp.status == 201
    item = (await resp.json())["item"]
    assert item["text"] == "Mælk"

    resp = await client.patch(f"/api/items/{item['id']}", json={"done": True})
    assert (await resp.json())["item"]["done"] is True
    resp = await client.patch(f"/api/items/{item['id']}", json={"text": "Havremælk"})
    assert (await resp.json())["item"]["text"] == "Havremælk"

    listing = await (await client.get("/api/items")).json()
    assert listing["items"] == [{"id": item["id"], "text": "Havremælk", "done": True}]

    resp = await client.delete(f"/api/items/{item['id']}")
    assert resp.status == 200
    assert (await (await client.get("/api/items")).json())["items"] == []


@pytest.mark.asyncio
async def test_clear_done(client):
    ids = [(await (await _add(client, t)).json())["item"]["id"] for t in ("a", "b")]
    await client.patch(f"/api/items/{ids[0]}", json={"done": True})
    resp = await client.post("/api/clear-done", json={})
    assert (await resp.json())["removed"] == 1
    items = (await (await client.get("/api/items")).json())["items"]
    assert [i["id"] for i in items] == [ids[1]]


@pytest.mark.asyncio
async def test_changes_are_broadcast_to_the_arc(client, service):
    await _add(client, "x")
    await asyncio.sleep(0.05)
    assert service.broadcasts and service.broadcasts[-1][0] == "todo_update"


@pytest.mark.asyncio
async def test_unknown_item_is_404(client):
    resp = await client.patch("/api/items/" + "f" * 32, json={"done": True})
    assert resp.status == 404
    resp = await client.delete("/api/items/not-an-id")
    assert resp.status == 404


# ── Input handling ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                          "multipart/form-data; boundary=x"])
async def test_changes_require_a_json_body(client, content_type):
    """HTML forms can only send these types — no JSON, no change."""
    resp = await client.post("/api/items", data='{"text": "x"}',
                             headers={"Content-Type": content_type})
    assert resp.status == 415
    assert (await (await client.get("/api/items")).json())["items"] == []


@pytest.mark.asyncio
async def test_oversized_body_is_refused(client):
    resp = await client.post("/api/items", json={"text": "x" * 5000})
    assert resp.status == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["{", "[1]", '"text"', '{"text": "x", "admin": true}',
                                  '{"text": ""}', '{"text": 42}'])
async def test_malformed_requests_are_400(client, body):
    resp = await client.post("/api/items", data=body,
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert "error" in await resp.json()


@pytest.mark.asyncio
async def test_update_rejects_non_boolean_done(client):
    item_id = (await (await _add(client, "x")).json())["item"]["id"]
    resp = await client.patch(f"/api/items/{item_id}", json={"done": "yes"})
    assert resp.status == 400
    resp = await client.patch(f"/api/items/{item_id}", json={})
    assert resp.status == 400


# ── Origin and Host guards ───────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "file://", "http://192.168.0.99"])
async def test_other_sites_can_neither_read_nor_change_the_list(client, origin):
    resp = await _add(client, "injected", Origin=origin)
    assert resp.status == 403
    assert "Access-Control-Allow-Origin" not in resp.headers

    resp = await client.get("/api/items", headers={"Origin": origin})
    assert resp.status == 403

    resp = await client.options("/api/items", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST"})
    assert resp.status == 403

    assert (await (await client.get("/api/items")).json())["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["evil.example", "evil.example:8793", "192.168.0.99:8793", ""])
async def test_dns_rebinding_is_refused(client, host):
    """A hostile name pointed at the device's IP still carries that name in Host."""
    for method, path in (("GET", "/"), ("GET", "/api/items"), ("POST", "/api/clear-done")):
        resp = await client.request(method, path, json={}, headers={"Host": host})
        assert resp.status == 403, (method, path)


@pytest.mark.asyncio
async def test_the_kiosk_and_the_phone_are_allowed(client):
    # The arc view is served from port 80 on the same device.
    resp = await _add(client, "fra skærmen", Origin="http://localhost")
    assert resp.status == 201
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"

    preflight = await client.options("/api/items/" + "a" * 32, headers={
        "Origin": "http://localhost", "Access-Control-Request-Method": "PATCH"})
    assert preflight.status == 204
    assert "PATCH" in preflight.headers["Access-Control-Allow-Methods"]

    # A phone on the LAN, same origin as the page it loaded.
    resp = await _add(client, "fra mobilen", Origin="http://192.168.0.62:8793",
                      Host="192.168.0.62:8793")
    assert resp.status == 201
    resp = await _add(client, "via mDNS", Origin="http://beosound5c.local:8793",
                      Host="BeoSound5c.local:8793")
    assert resp.status == 201


@pytest.mark.asyncio
async def test_router_command_route_changes_nothing(service):
    service._store.add("x")
    before = service._store.snapshot()
    assert await service.handle_command("select", {"action": "go"}) == {}
    assert service._store.snapshot() == before


def test_hostname_parsing():
    assert hostname_of("BeoSound5c.local.:8793") == "beosound5c.local"
    assert hostname_of("[::1]:8793") == "::1"
    assert hostname_of("192.168.0.62") == "192.168.0.62"
    assert hostname_of("") == ""
    assert hostname_of("[broken") == ""


def test_host_policy_rereads_addresses_when_due(monkeypatch):
    calls = []

    def resolver():
        calls.append(1)
        return {"localhost", "10.0.0.5"} if len(calls) > 1 else {"localhost"}

    policy = HostPolicy(resolver)
    assert not policy.knows("10.0.0.5")
    assert not policy.refresh_due()
    monkeypatch.setattr(HostPolicy, "REFRESH_AFTER_S", 0)
    assert policy.refresh_due()
    policy.refresh()
    assert policy.knows("10.0.0.5:8793")
