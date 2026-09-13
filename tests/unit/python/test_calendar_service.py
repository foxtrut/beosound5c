"""Tests for the Calendar source service's .ics download.

The parser has its own suite (test_calendar_ics.py); this pins the fetch.
aiohttp's StreamReader.read(n) returns one network chunk, not n bytes, so a
single read truncated real Google calendars (a few hundred KB) mid-file.
The server here streams the body in several flushed chunks to reproduce
that on a local socket.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sources.calendar_source import service as calendar_service
from sources.calendar_source.service import CalendarService


# ~300 KB — the size of an ordinary personal calendar, many chunks long.
BODY = ("BEGIN:VCALENDAR\r\n"
        + ("X-FILLER:" + "x" * 60 + "\r\n") * 4000
        + "END:VCALENDAR\r\n")


async def _chunked(request):
    resp = web.StreamResponse()
    await resp.prepare(request)
    data = BODY.encode()
    step = 16 * 1024
    for i in range(0, len(data), step):
        await resp.write(data[i:i + step])
        await asyncio.sleep(0)   # let each chunk reach the client separately
    await resp.write_eof()
    return resp


async def _fetch(path="/cal.ics"):
    app = web.Application()
    app.router.add_get("/cal.ics", _chunked)
    server = TestServer(app)
    await server.start_server()
    svc = CalendarService()
    svc._http_session = aiohttp.ClientSession()
    try:
        return await svc._fetch_ics(str(server.make_url(path)))
    finally:
        await svc._http_session.close()
        await server.close()


@pytest.mark.asyncio
async def test_fetch_returns_whole_body_across_chunks():
    text = await _fetch()
    assert len(text) == len(BODY)
    assert text.endswith("END:VCALENDAR\r\n")


@pytest.mark.asyncio
async def test_fetch_refuses_oversized_body(monkeypatch):
    monkeypatch.setattr(calendar_service, "MAX_ICS_BYTES", len(BODY) // 2)
    with pytest.raises(RuntimeError, match="larger than"):
        await _fetch()
