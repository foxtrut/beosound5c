#!/usr/bin/env python3
# BeoSound 5c
# Copyright (C) 2024-2026 Markus Kirsten
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Attribution required — see LICENSE, Section 7(b).

"""
BeoSound 5c — Calendar source (Google Calendar, read-only).

Reads one or more calendars from their iCal URLs and serves an agenda of
upcoming events to the frontend. Display only: nothing is ever written
back, which is why this uses the plain iCal export rather than the Google
Calendar API — no OAuth client, no consent screen on a device with no
keyboard, no tokens to refresh. In Google Calendar:

    Settings → Settings for my calendars → <calendar> →
    Integrate calendar → "Secret address in iCal format"

That URL is a bearer credential: anyone holding it can read the calendar.
See docs/google-calendar.md.

Config (config.json):
    "calendar": {
        "url": "https://calendar.google.com/calendar/ical/.../basic.ics",
        "days_ahead": 14
    }

Several calendars, each with a label shown next to its events:
    "calendar": {
        "calendars": [
            { "name": "Family",  "url": "https://..." },
            { "name": "Work",    "url": "https://..." }
        ]
    }

Port: 8791
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from lib.config import cfg
from lib.source_base import SourceBase

from calendar_ics import occurrences, parse_ics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CALENDAR] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REFRESH_INTERVAL = 15 * 60      # Google's export is cached upstream anyway
FETCH_TIMEOUT = 30              # a year of a busy calendar is a big file
MAX_ICS_BYTES = 8 * 1024 * 1024  # refuse to buffer a runaway response
DEFAULT_DAYS_AHEAD = 14
MAX_EVENTS = 200                # more than fits on screen by a wide margin


class CalendarService(SourceBase):
    id = "calendar"
    name = "Calendar"
    port = 8791
    player = "local"
    action_map = {
        "go": "refresh",
        "up": "up",
        "down": "down",
        "left": "back",
        "right": "select",
    }

    def __init__(self):
        super().__init__()
        self._agenda = {}
        # Timed events paired with their epoch window, so the "happening
        # now" marker can be refreshed per request instead of going stale
        # between fetches. The dicts are the same objects the agenda holds.
        self._live_windows = []
        self._last_fetch = 0
        self._last_error = None
        self._calendars = []
        self._days_ahead = DEFAULT_DAYS_AHEAD
        self._tz = ZoneInfo("UTC")

    # ── Lifecycle ──

    async def on_start(self):
        self._calendars = self._read_calendar_config()
        if not self._calendars:
            log.info("No calendar.url in config — calendar source disabled")
            raise SystemExit(0)

        self._days_ahead = self._read_days_ahead()
        self._tz = self._read_timezone()
        log.info("%d calendar(s) configured, %d days ahead, timezone %s",
                 len(self._calendars), self._days_ahead, self._tz)

        await self.register("available")
        self._spawn(self._refresh_loop(), name="refresh_loop")

    async def on_stop(self):
        await self.register("gone")

    # ── Config ──

    def _read_calendar_config(self):
        """Collect configured calendars as [{name, url}, ...].

        Accepts a single "url" (what the config UI writes) or a "calendars"
        list for people who keep more than one. A bare string in the list
        is treated as a URL with no label.
        """
        calendars = []

        single = (cfg("calendar", "url", default="") or "").strip()
        if single:
            calendars.append({"name": cfg("calendar", "name", default="")
                              or "", "url": single})

        for entry in cfg("calendar", "calendars", default=[]) or []:
            if isinstance(entry, str) and entry.strip():
                calendars.append({"name": "", "url": entry.strip()})
            elif isinstance(entry, dict):
                url = (entry.get("url") or "").strip()
                if url:
                    calendars.append({"name": entry.get("name") or "",
                                      "url": url})

        seen, unique = set(), []
        for cal in calendars:
            if cal["url"] not in seen:
                seen.add(cal["url"])
                unique.append(cal)
        return unique

    def _read_days_ahead(self):
        try:
            days = int(cfg("calendar", "days_ahead",
                           default=DEFAULT_DAYS_AHEAD))
        except (TypeError, ValueError):
            return DEFAULT_DAYS_AHEAD
        return max(1, min(days, 365))

    def _read_timezone(self):
        """Zone the agenda is rendered in.

        Defaults to the host zone, which on a device is set during install —
        the calendar should read in the time the clock on the wall shows.
        """
        name = (cfg("calendar", "timezone", default="") or "").strip()
        candidates = [name] if name else []
        candidates.append(os.environ.get("TZ", "").strip())
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("Unknown timezone %r — ignoring", candidate)
        try:
            return ZoneInfo(os.readlink("/etc/localtime").split("zoneinfo/")[-1])
        except (OSError, ValueError, ZoneInfoNotFoundError):
            return ZoneInfo("UTC")

    # ── Fetch ──

    async def _refresh_loop(self):
        while True:
            try:
                await self._fetch_all()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("Refresh failed: %s", e)
            await asyncio.sleep(REFRESH_INTERVAL)

    async def _fetch_all(self):
        now = datetime.now(self._tz)
        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=self._days_ahead)

        collected, failures = [], []
        for cal in self._calendars:
            label = cal["name"]
            try:
                text = await self._fetch_ics(cal["url"])
            except Exception as e:
                log.error("Fetch failed for calendar %r: %s", label or "?", e)
                failures.append(label or "calendar")
                continue

            try:
                events, _ = parse_ics(text)
                for occ in occurrences(events, window_start, window_end,
                                       self._tz):
                    occ["calendar"] = label
                    collected.append(occ)
            except Exception as e:
                log.error("Parse failed for calendar %r: %s", label or "?", e)
                failures.append(label or "calendar")

        if failures and len(failures) == len(self._calendars):
            # Every calendar failed — keep the last good agenda on screen
            # rather than replacing it with an empty one.
            self._last_error = "Could not reach Google Calendar"
            log.warning("All %d calendar(s) failed — keeping previous agenda",
                        len(self._calendars))
            return

        self._last_error = (f"{len(failures)} of {len(self._calendars)} "
                            "calendars unavailable") if failures else None
        self._agenda = self._build_agenda(collected, now)
        self._last_fetch = time.time()
        log.info("Agenda updated: %d event(s) across %d day(s)",
                 self._agenda["count"], len(self._agenda["days"]))

    async def _fetch_ics(self, url):
        """Download one .ics, following Google's redirect to its CDN."""
        async with self._http_session.get(
                url, timeout=FETCH_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            body = await resp.content.read(MAX_ICS_BYTES + 1)
            if len(body) > MAX_ICS_BYTES:
                raise RuntimeError("response larger than "
                                   f"{MAX_ICS_BYTES} bytes")
        return body.decode("utf-8", errors="replace")

    # ── Agenda shaping ──

    def _build_agenda(self, events, now):
        """Group occurrences into days, formatted for display.

        Times are pre-formatted here so the view does no timezone work: it
        only ever renders what this produced. Dates stay ISO so the view
        can still name the weekday in the browser's locale.
        """
        events.sort(key=lambda e: (
            e["start"] if e["all_day"] else e["start"].date(),
            0 if e["all_day"] else 1,
            "" if e["all_day"] else e["start"].strftime("%H:%M"),
            e["summary"].lower(),
        ))
        events = events[:MAX_EVENTS]

        today = now.date()
        days, index = [], {}
        self._live_windows = []
        for occ in events:
            start_date = occ["start"] if occ["all_day"] else occ["start"].date()
            # An event already under way belongs on today's list, not on the
            # day it happened to start.
            day_key = max(start_date, today)
            if day_key not in index:
                index[day_key] = {
                    "date": day_key.isoformat(),
                    "is_today": day_key == today,
                    "is_tomorrow": day_key == today + timedelta(days=1),
                    "events": [],
                }
                days.append(index[day_key])
            index[day_key]["events"].append(self._format_event(occ, now))

        return {
            "updated": time.time(),
            "timezone": str(self._tz),
            "days_ahead": self._days_ahead,
            "count": len(events),
            "error": self._last_error,
            "days": days,
        }

    def _format_event(self, occ, now):
        item = {
            "summary": occ["summary"] or "(no title)",
            "location": occ["location"],
            "calendar": occ.get("calendar", ""),
            "all_day": occ["all_day"],
        }
        if occ["all_day"]:
            item["time"] = ""
            if occ["end"] > occ["start"]:
                item["until"] = occ["end"].isoformat()
        else:
            item["time"] = occ["start"].strftime("%H:%M")
            item["end_time"] = occ["end"].strftime("%H:%M")
            item["ongoing"] = occ["start"] <= now <= occ["end"]
            self._live_windows.append(
                (item, occ["start"].timestamp(), occ["end"].timestamp()))
            if occ["end"].date() > occ["start"].date():
                item["until"] = occ["end"].date().isoformat()
        return item

    # ── HTTP ──

    def add_routes(self, app):
        app.router.add_get("/events", self._handle_events)

    async def _handle_events(self, request):
        self._refresh_ongoing()
        return web.json_response(self._agenda, headers=self._cors_headers())

    def _refresh_ongoing(self):
        """Re-evaluate which event is under way right now.

        The agenda itself only changes every REFRESH_INTERVAL, but whether
        a meeting is currently running changes by the minute — so this is
        recomputed per request rather than baked in at fetch time.
        """
        now = time.time()
        for item, start_ts, end_ts in self._live_windows:
            item["ongoing"] = start_ts <= now <= end_ts

    async def handle_status(self):
        return {
            "source": self.id,
            "name": self.name,
            "calendars": len(self._calendars),
            "last_fetch": self._last_fetch,
            "last_error": self._last_error,
            "has_data": bool(self._agenda),
        }

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        if cmd == "refresh":
            await self._fetch_all()
            return {"refreshed": True}
        return {}


if __name__ == "__main__":
    service = CalendarService()
    asyncio.run(service.run())
