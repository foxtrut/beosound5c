"""Tests for the contacts source: reading the contacts file, and keeping the
list readable cross-origin only by the device's own UI."""
from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sources.contacts.service import ContactsService, local_origin
from contacts_store import ContactsStore, normalise


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


# ── Store ────────────────────────────────────────────────────────────────────

def test_missing_file_is_an_empty_list(tmp_path):
    assert ContactsStore(str(tmp_path / "none.json")).contacts() == []


def test_contacts_are_cleaned_and_sorted_by_name():
    contacts = normalise({"contacts": [
        {"name": "  Søren   Larsen ", "phone": "+45 60 12 12 12"},
        {"name": "anna", "email": "anna@example.com", "extra": "ignored"},
        {"phone": "no name"},
        "not a contact",
    ]})
    assert [c["name"] for c in contacts] == ["anna", "Søren Larsen"]
    assert contacts[0]["email"] == "anna@example.com"
    assert contacts[0]["phone"] == ""
    assert "extra" not in contacts[0]


def test_a_bare_list_is_accepted():
    assert [c["name"] for c in normalise([{"name": "Bo"}])] == ["Bo"]


def test_ids_are_stable_and_follow_content():
    a = normalise([{"name": "Bo", "phone": "1"}])[0]["id"]
    assert a == normalise([{"name": "Bo", "phone": "1"}])[0]["id"]
    assert a != normalise([{"name": "Bo", "phone": "2"}])[0]["id"]


def test_store_rereads_a_changed_file_and_keeps_last_good_on_error(tmp_path):
    path = tmp_path / "contacts.json"
    _write(path, {"contacts": [{"name": "Bo"}]})
    store = ContactsStore(str(path))
    assert [c["name"] for c in store.contacts()] == ["Bo"]

    _write(path, {"contacts": [{"name": "Anna"}, {"name": "Bo"}]})
    os.utime(path, (1, 1))
    assert [c["name"] for c in store.contacts()] == ["Anna", "Bo"]

    path.write_text("{ broken", encoding="utf-8")
    os.utime(path, (2, 2))
    assert [c["name"] for c in store.contacts()] == ["Anna", "Bo"]


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin, ok", [
    ("http://localhost", True),
    ("http://localhost:8000", True),
    ("http://127.0.0.1", True),
    ("http://[::1]:80", True),
    ("http://192.168.0.62", False),
    ("https://evil.example.com", False),
    ("null", False),
    ("", False),
])
def test_local_origin(origin, ok):
    assert local_origin(origin) is ok


@pytest_asyncio.fixture
async def client(tmp_path):
    path = tmp_path / "contacts.json"
    _write(path, {"contacts": [{"name": "Bo", "phone": "+45 40 11 22 33"}]})
    svc = ContactsService(store_path=str(path))
    app = web.Application()
    svc.add_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_device_ui_may_read_the_list(client):
    resp = await client.get("/api/contacts", headers={"Origin": "http://localhost"})
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"
    data = await resp.json()
    assert [c["name"] for c in data["contacts"]] == ["Bo"]


@pytest.mark.asyncio
async def test_other_sites_get_no_cors_grant(client):
    resp = await client.get("/api/contacts", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in resp.headers
