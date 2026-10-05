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


# ── Clock handling ───────────────────────────────────────────────────────────
#
# The device has no battery-backed clock. At boot it briefly believes it is
# whatever day it was last switched off, and an agenda built in that window
# is weeks stale — which is exactly what the user sees, because they look at
# the screen right after switching on. These pin both halves of the fix:
# waiting for NTP before the first fetch, and noticing afterwards when the
# clock is corrected or midnight passes.

import types
from datetime import date, timedelta


class _FakeClock:
    """A clock that only moves when the service sleeps."""

    def __init__(self, jump_at_sleep=None, jump=0.0):
        self.wall = 1_800_000_000.0
        self.mono = 1_000.0
        self.sleeps = 0
        self._jump_at = jump_at_sleep
        self._jump = jump

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def shim_asyncio(self):
        """An `asyncio` stand-in whose sleep advances this clock instantly."""
        async def sleep(duration):
            self.sleeps += 1
            self.mono += duration
            self.wall += duration
            if self._jump_at is not None and self.sleeps == self._jump_at:
                self.wall += self._jump      # NTP correcting the clock
            await asyncio.sleep(0)
        return types.SimpleNamespace(sleep=sleep,
                                     CancelledError=asyncio.CancelledError)


def _service_with_clock(monkeypatch, clock):
    svc = CalendarService()
    monkeypatch.setattr(calendar_service, "time", clock)
    monkeypatch.setattr(calendar_service, "asyncio", clock.shim_asyncio())
    return svc


class TestClockSynced:
    def test_synced_once_the_marker_appears(self, monkeypatch, tmp_path):
        marker = tmp_path / "synchronized"
        monkeypatch.setattr(calendar_service, "TIMESYNC_MARKER", str(marker))
        svc = CalendarService()
        assert svc._clock_is_synced() is False
        marker.touch()
        assert svc._clock_is_synced() is True

    def test_host_without_timesyncd_is_trusted(self, monkeypatch, tmp_path):
        """A developer Mac has no /run/systemd — nothing to wait for."""
        monkeypatch.setattr(calendar_service, "TIMESYNC_MARKER",
                            str(tmp_path / "absent" / "synchronized"))
        assert CalendarService()._clock_is_synced() is True


@pytest.mark.asyncio
async def test_wait_for_clock_returns_when_marker_appears(monkeypatch, tmp_path):
    marker = tmp_path / "synchronized"
    monkeypatch.setattr(calendar_service, "TIMESYNC_MARKER", str(marker))
    clock = _FakeClock()
    svc = _service_with_clock(monkeypatch, clock)

    original_sleep = calendar_service.asyncio.sleep

    async def sleep_then_sync(duration):
        await original_sleep(duration)
        if clock.sleeps == 2:
            marker.touch()          # NTP arrives on the second poll
    monkeypatch.setattr(calendar_service.asyncio, "sleep", sleep_then_sync)

    await svc._wait_for_clock()
    assert clock.sleeps == 2


@pytest.mark.asyncio
async def test_wait_for_clock_gives_up_rather_than_hanging(monkeypatch, tmp_path):
    """No internet must not mean no calendar at all."""
    monkeypatch.setattr(calendar_service, "TIMESYNC_MARKER",
                        str(tmp_path / "synchronized"))
    clock = _FakeClock()
    svc = _service_with_clock(monkeypatch, clock)
    await svc._wait_for_clock()
    assert clock.mono - 1_000.0 >= calendar_service.CLOCK_WAIT_TIMEOUT


@pytest.mark.asyncio
async def test_refresh_waits_the_full_interval_when_nothing_changes(monkeypatch):
    clock = _FakeClock()
    svc = _service_with_clock(monkeypatch, clock)
    svc._agenda_date = date.today()
    start = clock.mono
    await svc._wait_for_refresh()
    assert clock.mono - start >= calendar_service.REFRESH_INTERVAL


@pytest.mark.asyncio
async def test_refresh_wakes_early_when_the_clock_is_corrected(monkeypatch):
    """The boot-time NTP jump: wall time moves, elapsed time does not."""
    clock = _FakeClock(jump_at_sleep=2, jump=3 * 7 * 24 * 3600)
    svc = _service_with_clock(monkeypatch, clock)
    svc._agenda_date = date.today()
    start = clock.mono
    await svc._wait_for_refresh()
    assert clock.mono - start < calendar_service.REFRESH_INTERVAL
    assert clock.sleeps == 2


@pytest.mark.asyncio
async def test_refresh_wakes_early_when_the_date_rolls_over(monkeypatch):
    """Left on past midnight, 'today' and 'tomorrow' are both wrong."""
    clock = _FakeClock()
    svc = _service_with_clock(monkeypatch, clock)
    svc._agenda_date = date.today() - timedelta(days=1)
    start = clock.mono
    await svc._wait_for_refresh()
    assert clock.mono - start < calendar_service.REFRESH_INTERVAL
