"""Tests for the contacts source: the store, the phone page and its API, and
the guards that keep other web pages (and DNS rebinding) away from the
contacts — which are personal data."""
from __future__ import annotations

import datetime
import io
import json
import re
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import sources.contacts.service as service_module
from sources.contacts.service import ContactsService, page_language
from contacts_photo import PHOTO_SIZE, PhotoError, normalise_photo
from contacts_store import ContactError, ContactsStore, NotFound, clean_field

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTS = {"localhost", "127.0.0.1", "::1", "beosound5c", "beosound5c.local", "192.168.0.62"}


# ── Store ────────────────────────────────────────────────────────────────────

def test_missing_file_is_an_empty_list(tmp_path):
    assert ContactsStore(str(tmp_path / "none.json")).snapshot() == {"version": 0, "contacts": []}


def test_add_update_delete_persist_and_sort_by_name(tmp_path):
    path = tmp_path / "contacts.json"
    store = ContactsStore(str(path))
    bo, _ = store.add({"name": "  Bo   Nielsen ", "phone": "+45 40 11 22 33"})
    anna, _ = store.add({"name": "anna"})
    assert bo["name"] == "Bo Nielsen" and bo["email"] == ""
    assert re.fullmatch(r"[0-9a-f]{32}", bo["id"])

    store.update(anna["id"], {"name": "Anna Jensen", "note": "Nabo"})
    reloaded = ContactsStore(str(path)).snapshot()
    assert reloaded["version"] == 3
    assert [(c["name"], c["note"]) for c in reloaded["contacts"]] == [
        ("Anna Jensen", "Nabo"), ("Bo Nielsen", "")]
    assert path.stat().st_mode & 0o777 == 0o600

    store.delete(bo["id"])
    assert [c["name"] for c in ContactsStore(str(path)).snapshot()["contacts"]] == ["Anna Jensen"]


def test_a_contact_needs_a_name(tmp_path):
    store = ContactsStore(str(tmp_path / "c.json"))
    with pytest.raises(ContactError, match="name_missing"):
        store.add({"phone": "123"})
    contact, _ = store.add({"name": "Bo"})
    with pytest.raises(ContactError, match="name_missing"):
        store.update(contact["id"], {"name": "   "})
    with pytest.raises(NotFound):
        store.update("f" * 32, {"note": "x"})


def test_field_cleaning():
    assert clean_field("note", "a‮b\tc") == "ab c"
    assert clean_field("phone", None) == ""
    with pytest.raises(ContactError, match="too_long"):
        clean_field("phone", "1" * 41)
    with pytest.raises(ContactError, match="invalid"):
        clean_field("email", 42)


def test_a_hand_written_file_gets_ids_and_a_broken_one_is_set_aside(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps({"contacts": [{"name": "Bo"}, {"phone": "no name"}]}))
    contacts = ContactsStore(str(path)).snapshot()["contacts"]
    assert [c["name"] for c in contacts] == ["Bo"]
    assert re.fullmatch(r"[0-9a-f]{32}", contacts[0]["id"])

    path.write_text("{ broken")
    assert ContactsStore(str(path)).snapshot()["contacts"] == []
    assert list(tmp_path.glob("contacts.json.corrupt-*"))


def test_birthday_must_be_a_real_past_date(tmp_path):
    store = ContactsStore(str(tmp_path / "c.json"))
    contact, _ = store.add({"name": "Bo", "birthday": "1980-03-12"})
    assert contact["birthday"] == "1980-03-12"
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    for bad in ("1980-02-30", "12-03-1980", "1899-12-31", tomorrow, 19800312):
        with pytest.raises(ContactError, match="invalid_birthday"):
            store.update(contact["id"], {"birthday": bad})
    store.update(contact["id"], {"birthday": ""})
    assert store.snapshot()["contacts"][0]["birthday"] == ""


def test_a_stored_future_birthday_survives_a_boot_with_an_old_clock(tmp_path):
    """The device has no RTC and can start with last shutdown's date."""
    path = tmp_path / "c.json"
    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    path.write_text(json.dumps({"contacts": [{"name": "Bo", "birthday": future}]}))
    assert ContactsStore(str(path)).snapshot()["contacts"][0]["birthday"] == future


# ── Photos ───────────────────────────────────────────────────────────────────

def _image_bytes(fmt="JPEG", size=(1200, 800), exif_gps=False):
    img = Image.new("RGB", size, (200, 30, 30))
    out = io.BytesIO()
    kwargs = {}
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (55.0, 40.0, 0.0)}   # GPSInfo
        exif[0x010F] = "PhoneMaker"
        kwargs["exif"] = exif
    img.save(out, fmt, **kwargs)
    return out.getvalue()


def test_photos_are_reencoded_square_without_metadata():
    jpeg = normalise_photo(_image_bytes(exif_gps=True))
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG"
        assert img.size == (PHOTO_SIZE, PHOTO_SIZE)
        assert not img.getexif()
    assert normalise_photo(_image_bytes("PNG", (300, 500)))[:3] == b"\xff\xd8\xff"


@pytest.mark.parametrize("data", [b"", b"not an image", b"<svg xmlns='http://www.w3.org/2000/svg'/>"])
def test_non_pictures_are_refused(data):
    with pytest.raises(PhotoError, match="photo_invalid"):
        normalise_photo(data)


def test_photo_files_follow_the_contact(tmp_path):
    store = ContactsStore(str(tmp_path / "c.json"))
    contact, _ = store.add({"name": "Bo"})
    assert contact["photo"] == ""
    saved, _ = store.set_photo(contact["id"], b"jpeg")
    path = Path(store.photo_path(contact["id"]))
    assert path.read_bytes() == b"jpeg" and re.fullmatch(r"[0-9a-f]{16}", saved["photo"])
    assert path.stat().st_mode & 0o777 == 0o600
    again, _ = store.set_photo(contact["id"], b"jpeg2")
    assert again["photo"] != saved["photo"], "a new picture must get a new cache token"

    store.clear_photo(contact["id"])
    assert not path.exists() and store.photo_token(contact["id"]) == ""

    store.set_photo(contact["id"], b"jpeg")
    store.delete(contact["id"])
    assert not path.exists()
    with pytest.raises(NotFound):
        store.photo_path("../../etc/passwd")


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = ContactsService(store_path=str(tmp_path / "contacts.json"),
                          host_resolver=lambda: set(HOSTS))
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


async def _add(client, body, **headers):
    return await client.post("/api/contacts", json=body, headers=headers)


@pytest.mark.asyncio
async def test_phone_page_is_served_with_strict_headers(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Cache-Control"] == "no-store"
    for asset in ("/app.js", "/app.css"):
        assert (await client.get(asset)).status == 200


def test_contact_text_is_never_rendered_as_html():
    """Contacts come from phones; both views must insert them as text."""
    phone_js = (REPO_ROOT / "services/sources/contacts/phone/app.js").read_text()
    phone_html = (REPO_ROOT / "services/sources/contacts/phone/index.html").read_text()
    arc_page = (REPO_ROOT / "web/softarc/contacts.html").read_text()
    assert "innerHTML" not in phone_js and "insertAdjacentHTML" not in phone_js
    assert "innerHTML" not in arc_page
    assert "<script>" not in phone_html and "style=" not in phone_html, "CSP forbids inline code"


@pytest.mark.asyncio
async def test_add_edit_delete_roundtrip(client, service):
    resp = await _add(client, {"name": "Bo", "phone": "+45 40 11 22 33"})
    assert resp.status == 201
    contact = (await resp.json())["contact"]

    resp = await client.patch(f"/api/contacts/{contact['id']}", json={"email": "bo@example.com"})
    assert resp.status == 200
    listed = (await (await client.get("/api/contacts")).json())["contacts"]
    assert listed == [{**contact, "email": "bo@example.com"}]

    assert (await client.delete(f"/api/contacts/{contact['id']}")).status == 200
    assert (await (await client.get("/api/contacts")).json())["contacts"] == []
    assert [event for event, _ in service.broadcasts] == ["contacts_update"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
async def test_changes_require_a_json_body(client, content_type):
    resp = await client.post("/api/contacts", data='{"name": "x"}',
                             headers={"Content-Type": content_type})
    assert resp.status == 415


@pytest.mark.asyncio
async def test_malformed_and_oversized_requests_are_refused(client):
    assert (await _add(client, {"name": "x", "is_admin": True})).status == 400
    assert (await _add(client, ["x"])).status == 400
    resp = await client.post("/api/contacts", data="x" * 5000,
                             headers={"Content-Type": "application/json"})
    assert resp.status == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "file://", "http://192.168.0.99"])
async def test_other_sites_can_neither_read_nor_change_the_contacts(client, origin):
    resp = await _add(client, {"name": "injected"}, Origin=origin)
    assert resp.status == 403
    assert "Access-Control-Allow-Origin" not in resp.headers

    resp = await client.get("/api/contacts", headers={"Origin": origin})
    assert resp.status == 403

    assert (await (await client.get("/api/contacts")).json())["contacts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["evil.example", "evil.example:8794", "192.168.0.99:8794", ""])
async def test_dns_rebinding_is_refused(client, host):
    for method, path in (("GET", "/"), ("GET", "/api/contacts"), ("POST", "/api/contacts")):
        resp = await client.request(method, path, json={}, headers={"Host": host})
        assert resp.status == 403, (method, path)


@pytest.mark.asyncio
async def test_the_kiosk_and_the_phone_are_allowed(client):
    # The arc view is served from port 80 on the same device.
    resp = await client.get("/api/contacts", headers={"Origin": "http://localhost"})
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"

    # A phone on the LAN, same origin as the page it loaded.
    resp = await _add(client, {"name": "fra mobilen"}, Origin="http://192.168.0.62:8794",
                      Host="192.168.0.62:8794")
    assert resp.status == 201
    resp = await _add(client, {"name": "via mDNS"}, Origin="http://beosound5c.local:8794",
                      Host="BeoSound5c.local:8794")
    assert resp.status == 201


@pytest.mark.asyncio
async def test_router_command_route_changes_nothing(service):
    service._store.add({"name": "x"})
    before = service._store.snapshot()
    assert await service.handle_command("select", {"action": "go"}) == {}
    assert service._store.snapshot() == before


# ── Language ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("configured, accept, expected", [
    ("da", "en-US,en;q=0.9", "da"),
    ("en", "da-DK,da;q=0.9", "en"),
    ("auto", "da-DK,da;q=0.9,en;q=0.8", "da"),
    ("auto", "sv-SE,sv;q=0.9", "en"),
    (None, "da", "da"),
])
def test_page_language(configured, accept, expected):
    assert page_language(configured, accept) == expected


@pytest.mark.asyncio
async def test_phone_page_is_served_in_the_device_language(client, monkeypatch):
    monkeypatch.setattr(service_module, "cfg",
                        lambda *keys, default=None: "da" if keys == ("language",) else default)
    resp = await client.get("/", headers={"Accept-Language": "en-US"})
    assert '<html lang="da">' in await resp.text()
    assert resp.headers["Content-Language"] == "da"


@pytest.mark.asyncio
async def test_errors_are_codes_for_the_page_to_translate(client):
    assert await (await _add(client, {"name": "  "})).json() == {"error": "name_missing"}
    assert await (await _add(client, {"name": "x" * 81})).json() == {"error": "too_long"}
    resp = await client.patch("/api/contacts/" + "f" * 32, json={"note": "x"})
    assert await resp.json() == {"error": "not_found"}


def test_every_error_the_page_shows_is_translated_in_both_languages():
    js = (REPO_ROOT / "services/sources/contacts/phone/app.js").read_text()
    blocks = re.findall(r"errors: \{(.*?)\n\s*\},", js, re.DOTALL)
    assert len(blocks) == 2, "expected an errors table for en and da"
    for code in ("name_missing", "too_long", "invalid", "full", "not_found", "save_failed",
                 "invalid_birthday", "photo_invalid", "photo_too_large", "not_image"):
        for block in blocks:
            assert re.search(rf"\b{code}: ", block), code


def test_arc_view_has_both_languages():
    page = (REPO_ROOT / "web/softarc/contacts.html").read_text()
    assert "en: { empty:" in page and "da: { empty:" in page


def test_config_page_links_to_the_phone_page():
    page = (REPO_ROOT / "web/softarc/config.html").read_text()
    entry = next(line for line in page.splitlines() if "key: 'CONTACTS'" in line)
    assert "${location.hostname}:8794/" in entry


@pytest.mark.asyncio
async def test_photo_upload_serve_and_remove(client, service):
    contact = (await (await _add(client, {"name": "Bo"})).json())["contact"]
    url = f"/api/contacts/{contact['id']}/photo"
    assert (await client.get(url)).status == 404

    resp = await client.put(url, data=_image_bytes(), headers={"Content-Type": "image/jpeg"})
    assert resp.status == 200
    token = (await resp.json())["contact"]["photo"]
    assert token

    resp = await client.get(f"{url}?v={token}")
    assert resp.status == 200 and resp.content_type == "image/jpeg"
    with Image.open(io.BytesIO(await resp.read())) as img:
        assert img.size == (PHOTO_SIZE, PHOTO_SIZE)

    listed = (await (await client.get("/api/contacts")).json())["contacts"][0]
    assert listed["photo"] == token

    assert (await client.delete(url)).status == 200
    assert (await client.get(url)).status == 404
    assert service.broadcasts[-1][0] == "contacts_update"


@pytest.mark.asyncio
async def test_bad_photo_uploads_are_refused(client):
    contact = (await (await _add(client, {"name": "Bo"})).json())["contact"]
    url = f"/api/contacts/{contact['id']}/photo"
    resp = await client.put(url, data=b"<html>", headers={"Content-Type": "text/html"})
    assert resp.status == 415
    resp = await client.put(url, data=b"garbage", headers={"Content-Type": "image/jpeg"})
    assert await resp.json() == {"error": "photo_invalid"}
    resp = await client.put("/api/contacts/" + "f" * 32 + "/photo", data=_image_bytes(),
                            headers={"Content-Type": "image/jpeg"})
    assert resp.status == 404
    resp = await client.put(url, data=_image_bytes(), headers={
        "Content-Type": "image/jpeg", "Origin": "https://evil.example"})
    assert resp.status == 403


@pytest.mark.asyncio
async def test_birthday_goes_through_the_api(client):
    resp = await _add(client, {"name": "Bo", "birthday": "1980-03-12"})
    assert (await resp.json())["contact"]["birthday"] == "1980-03-12"
    resp = await _add(client, {"name": "Bo", "birthday": "1980-13-01"})
    assert await resp.json() == {"error": "invalid_birthday"}
