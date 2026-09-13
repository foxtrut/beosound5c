#!/usr/bin/env python3
# BeoSound 5c
# Copyright (C) 2024-2026 Markus Kirsten
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Attribution required — see LICENSE, Section 7(b).

"""
Minimal iCalendar (RFC 5545) reader — just enough to display a calendar.

Google's "secret address in iCal format" export is the only input this has
to handle, so the scope is deliberately narrow: VEVENT blocks, timed and
all-day, with recurrence expanded through dateutil.rrule. Everything a
display doesn't need (VTODO, VALARM, attendees, free/busy) is ignored.

Kept separate from service.py so the fiddly parts — line unfolding, TZID
resolution, UNTIL normalisation, RECURRENCE-ID overrides — are unit
testable without standing up an HTTP service.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")

# A recurring event with no COUNT/UNTIL is infinite; every expansion is
# bounded by the caller's window, but a pathological rule (SECONDLY) could
# still generate absurd numbers inside it. Cap per event.
MAX_OCCURRENCES_PER_EVENT = 500


# ── Line handling ────────────────────────────────────────────────────────────

def unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding.

    A line beginning with a space or tab is a continuation of the previous
    one. Google folds at 75 octets, so long SUMMARY/DESCRIPTION values
    arrive split — unfolding has to happen before anything is parsed, or
    every long event title comes out truncated.
    """
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """Split "NAME;PARAM=V:value" into (name, params, value).

    The colon search has to skip colons inside quoted parameter values
    (TZID="GMT+01:00" appears in some exports).
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None

    parts = head.split(";")
    name = parts[0].strip().upper()
    params = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _unescape(value: str) -> str:
    """Decode escaped TEXT values (RFC 5545 §3.3.11)."""
    out = []
    it = iter(range(len(value)))
    skip = False
    for i in it:
        if skip:
            skip = False
            continue
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            skip = True
        else:
            out.append(ch)
    return "".join(out)


# ── Date / time handling ─────────────────────────────────────────────────────

def _resolve_tz(tzid: str | None, default_tz: ZoneInfo) -> ZoneInfo:
    """Map a TZID to a zoneinfo zone, falling back to the calendar default.

    Google emits IANA names ("Europe/Copenhagen"), which resolve directly.
    Other producers emit Windows names ("Romance Standard Time") that don't
    — those fall back rather than raising, because one odd event must not
    take down the whole calendar.
    """
    if not tzid:
        return default_tz
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        log.debug("Unknown TZID %r — using %s", tzid, default_tz)
        return default_tz


def parse_dt(value: str, params: dict[str, str], default_tz: ZoneInfo):
    """Parse a DATE or DATE-TIME property value.

    Returns a `date` for all-day values, an aware `datetime` otherwise.
    A trailing Z means UTC; a TZID names the zone; a bare local time
    ("floating") is interpreted in the calendar's own zone.
    """
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        return datetime.strptime(value, "%Y%m%d").date()

    if value.endswith("Z"):
        naive = datetime.strptime(value[:-1], "%Y%m%dT%H%M%S")
        return naive.replace(tzinfo=UTC)

    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return naive.replace(tzinfo=_resolve_tz(params.get("TZID"), default_tz))


_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(value: str) -> timedelta | None:
    """Parse an RFC 5545 DURATION ("PT1H", "P1DT2H30M", "-PT15M")."""
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items()
             if v and k != "sign"}
    if not parts:
        return timedelta(0)
    delta = timedelta(
        weeks=parts.get("weeks", 0),
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
    return -delta if match.group("sign") == "-" else delta


def normalize_until(rrule_text: str, dtstart_aware: bool) -> str:
    """Make an RRULE's UNTIL comparable with its DTSTART.

    dateutil raises (or worse, compares wrongly) when UNTIL and DTSTART
    disagree about awareness. Google pairs an all-day DTSTART with a DATE
    UNTIL and a timed DTSTART with a UTC UNTIL, but plenty of other
    producers mix them, so rewrite the token to match DTSTART:
      aware DTSTART  → UNTIL must be a UTC date-time (…Z)
      naive DTSTART  → UNTIL must be naive
    """
    def fix(match: re.Match) -> str:
        value = match.group(1).strip()
        if dtstart_aware:
            if len(value) == 8:              # DATE → end of that day, UTC
                value = value + "T235959Z"
            elif not value.endswith("Z"):
                value = value + "Z"
        elif value.endswith("Z"):
            value = value[:-1]
        return "UNTIL=" + value

    return re.sub(r"UNTIL=([^;]*)", fix, rrule_text, flags=re.IGNORECASE)


# ── VEVENT parsing ───────────────────────────────────────────────────────────

# Properties carried straight through as text.
_TEXT_PROPS = {"UID": "uid", "SUMMARY": "summary", "LOCATION": "location",
               "DESCRIPTION": "description", "STATUS": "status"}


def parse_ics(text: str) -> tuple[list[dict], ZoneInfo | None]:
    """Parse an .ics document into raw VEVENT dicts.

    Returns (events, calendar_tz). calendar_tz comes from Google's
    X-WR-TIMEZONE and is used as the fallback zone for floating times.
    """
    events: list[dict] = []
    current: dict | None = None
    depth_other = 0          # inside VALARM/VTIMEZONE — ignore its properties
    calendar_tz: ZoneInfo | None = None

    for line in unfold(text):
        if not line.strip():
            continue
        parsed = _split_property(line)
        if not parsed:
            continue
        name, params, value = parsed

        if name == "BEGIN":
            block = value.strip().upper()
            if block == "VEVENT":
                current = {"rrule": None, "exdates": [], "recurrence_id": None}
            elif current is not None or block in ("VALARM", "VTIMEZONE"):
                depth_other += 1
            continue

        if name == "END":
            block = value.strip().upper()
            if block == "VEVENT" and current is not None:
                if current.get("dtstart") is not None:
                    events.append(current)
                current = None
            elif depth_other:
                depth_other -= 1
            continue

        if depth_other:
            continue

        if current is None:
            if name == "X-WR-TIMEZONE":
                calendar_tz = _resolve_tz(value.strip(), UTC)
            continue

        # VTIMEZONE is skipped above, so any TZID here is the event's own.
        default_tz = calendar_tz or UTC

        if name in _TEXT_PROPS:
            current[_TEXT_PROPS[name]] = _unescape(value).strip()
        elif name == "DTSTART":
            current["dtstart"] = parse_dt(value, params, default_tz)
        elif name == "DTEND":
            current["dtend"] = parse_dt(value, params, default_tz)
        elif name == "DURATION":
            current["duration"] = parse_duration(value)
        elif name == "RRULE":
            current["rrule"] = value.strip()
        elif name == "EXDATE":
            for piece in value.split(","):
                if piece.strip():
                    current["exdates"].append(
                        parse_dt(piece, params, default_tz))
        elif name == "RECURRENCE-ID":
            current["recurrence_id"] = parse_dt(value, params, default_tz)

    return events, calendar_tz


# ── Recurrence expansion ─────────────────────────────────────────────────────

def _event_duration(event: dict) -> timedelta:
    """How long one occurrence lasts.

    DTEND wins over DURATION (RFC 5545 forbids both). An all-day event with
    neither lasts a day; a timed one lasts no time at all.
    """
    start, end = event["dtstart"], event.get("dtend")
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    if end is not None:
        try:
            return end - start
        except TypeError:
            # DTSTART and DTEND disagree about all-day-ness — malformed.
            log.debug("Mismatched DTSTART/DTEND for %r", event.get("uid"))
    if event.get("duration") is not None:
        return event["duration"]
    return timedelta(days=1) if all_day else timedelta(0)


def _instant_key(value):
    """A hashable identity for an occurrence start.

    EXDATE and RECURRENCE-ID may be written in a different zone than
    DTSTART, so matching on the raw datetime misses. Compare the instant
    (all-day values compare as plain dates).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC)
    return value


def _to_naive(value):
    """All-day dates and naive datetimes share one expansion space."""
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _expand_one(event: dict, after, before) -> list:
    """Occurrence start values for one master event within [after, before].

    `after`/`before` must match the event's own flavour: naive for all-day,
    aware for timed.
    """
    dtstart = event["dtstart"]
    all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
    base = _to_naive(dtstart) if all_day else dtstart

    if not event.get("rrule"):
        return [dtstart] if after <= base <= before else []

    rule_text = normalize_until(event["rrule"], dtstart_aware=not all_day)
    try:
        rule = rrulestr(rule_text, dtstart=base)
        starts = rule.between(after, before, inc=True)
    except Exception as e:
        # A single unparseable rule must not blank the whole calendar.
        log.warning("Could not expand RRULE for %r (%s): %s",
                    event.get("summary"), event.get("uid"), e)
        return [dtstart] if after <= base <= before else []

    starts = starts[:MAX_OCCURRENCES_PER_EVENT]
    return [s.date() for s in starts] if all_day else starts


def occurrences(events: list[dict], window_start: datetime,
                window_end: datetime, display_tz: ZoneInfo) -> list[dict]:
    """Expand VEVENTs into concrete occurrences overlapping the window.

    Handles the three things a real Google calendar always contains:
    plain events, recurring series (RRULE/EXDATE), and single edited
    instances of a series (RECURRENCE-ID), which replace the occurrence
    they point at rather than doubling it.

    Occurrences already under way at `window_start` are included — an
    all-day holiday or a meeting in progress should still be on screen.
    """
    masters = [e for e in events if e.get("recurrence_id") is None]
    overrides = [e for e in events if e.get("recurrence_id") is not None]

    replaced: dict[str, set] = {}
    for override in overrides:
        uid = override.get("uid")
        if uid:
            replaced.setdefault(uid, set()).add(
                _instant_key(override["recurrence_id"]))

    results: list[dict] = []

    for event in masters:
        if (event.get("status") or "").upper() == "CANCELLED":
            continue
        duration = _event_duration(event)
        all_day = isinstance(event["dtstart"], date) and \
            not isinstance(event["dtstart"], datetime)

        # Widen the search back by the event's own length so an occurrence
        # that began before the window but is still running is found.
        lookback = duration if duration > timedelta(0) else timedelta(0)
        if all_day:
            after = datetime(window_start.year, window_start.month,
                             window_start.day) - lookback
            before = datetime(window_end.year, window_end.month,
                              window_end.day)
        else:
            after = window_start - lookback
            before = window_end

        skip = replaced.get(event.get("uid"), set())
        exdates = {_instant_key(x) for x in event.get("exdates", [])}

        for start in _expand_one(event, after, before):
            key = _instant_key(start)
            if key in exdates or key in skip:
                continue
            results.append(_occurrence(event, start, duration, all_day,
                                       display_tz))

    for override in overrides:
        if (override.get("status") or "").upper() == "CANCELLED":
            continue
        start = override["dtstart"]
        duration = _event_duration(override)
        all_day = isinstance(start, date) and not isinstance(start, datetime)
        occ = _occurrence(override, start, duration, all_day, display_tz)
        if _overlaps(occ, window_start, window_end):
            results.append(occ)

    results.sort(key=_sort_key)
    return results


def _occurrence(event: dict, start, duration: timedelta, all_day: bool,
                display_tz: ZoneInfo) -> dict:
    """Build one occurrence, with times moved into the display zone."""
    if all_day:
        end_exclusive = start + (duration or timedelta(days=1))
        # DTEND is exclusive for all-day events: a single-day event on the
        # 12th is written DTSTART=12, DTEND=13. Store the last day the
        # event actually covers, which is what a reader expects to see.
        end = end_exclusive - timedelta(days=1)
        if end < start:
            end = start
        return {
            "uid": event.get("uid", ""),
            "summary": event.get("summary", ""),
            "location": event.get("location", ""),
            "description": event.get("description", ""),
            "all_day": True,
            "start": start,
            "end": end,
        }

    start_local = start.astimezone(display_tz)
    return {
        "uid": event.get("uid", ""),
        "summary": event.get("summary", ""),
        "location": event.get("location", ""),
        "description": event.get("description", ""),
        "all_day": False,
        "start": start_local,
        "end": (start + duration).astimezone(display_tz),
    }


def _overlaps(occ: dict, window_start: datetime, window_end: datetime) -> bool:
    if occ["all_day"]:
        return (occ["end"] >= window_start.date()
                and occ["start"] <= window_end.date())
    return occ["end"] >= window_start and occ["start"] <= window_end


def _sort_key(occ: dict) -> tuple:
    """All-day events sort to the top of their day, then by start time."""
    if occ["all_day"]:
        return (occ["start"], 0, occ["summary"].lower())
    return (occ["start"].date(), 1, occ["start"].timetz(),
            occ["summary"].lower())
