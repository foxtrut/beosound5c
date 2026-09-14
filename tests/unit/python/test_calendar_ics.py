"""Tests for the calendar source's iCalendar reader.

The parser is the part of the calendar source that can silently show the
wrong thing rather than fail loudly: a mis-parsed recurrence puts a
meeting on the wrong evening, and a mishandled all-day DTEND stretches a
one-day holiday across two. Each test below pins one of those.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Imported the way service.py imports it — the source directory goes on
# sys.path, so the module is top-level `calendar_ics`.
CALENDAR_DIR = (Path(__file__).resolve().parents[3]
                / "services" / "sources" / "calendar_source")
sys.path.insert(0, str(CALENDAR_DIR))

import calendar_ics as ics  # noqa: E402

CPH = ZoneInfo("Europe/Copenhagen")
UTC = ZoneInfo("UTC")


def wrap(*events: str, tz: str = "Europe/Copenhagen") -> str:
    """A minimal VCALENDAR around raw VEVENT blocks."""
    body = "\n".join(events)
    return ("BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\n"
            f"X-WR-TIMEZONE:{tz}\r\n"
            f"{body}\r\n"
            "END:VCALENDAR\r\n")


def expand(ics_text: str, start: datetime, days: int = 14) -> list[dict]:
    events, _ = ics.parse_ics(ics_text)
    return ics.occurrences(events, start, start + timedelta(days=days), CPH)


TIMED = """BEGIN:VEVENT
UID:timed@example.test
DTSTART;TZID=Europe/Copenhagen:20260914T100000
DTEND;TZID=Europe/Copenhagen:20260914T113000
SUMMARY:Tandlaege
LOCATION:Hovedgaden 4
END:VEVENT"""


class TestLineHandling:
    def test_unfolds_continuation_lines(self):
        """Google folds at 75 octets — a long title arrives split."""
        folded = ("BEGIN:VEVENT\r\n"
                  "UID:long@example.test\r\n"
                  "DTSTART;TZID=Europe/Copenhagen:20260914T100000\r\n"
                  "DTEND;TZID=Europe/Copenhagen:20260914T110000\r\n"
                  "SUMMARY:Et arrangement med et meget langt navn der bliver\r\n"
                  "  foldet over to linjer\r\n"
                  "END:VEVENT")
        events, _ = ics.parse_ics(wrap(folded))
        assert events[0]["summary"] == (
            "Et arrangement med et meget langt navn der bliver"
            " foldet over to linjer")

    def test_unescapes_text_values(self):
        raw = ("BEGIN:VEVENT\r\n"
               "UID:esc@example.test\r\n"
               "DTSTART;TZID=Europe/Copenhagen:20260914T100000\r\n"
               "SUMMARY:Middag\\, vin og kage\r\n"
               "LOCATION:Vej 1\\nOpgang B\r\n"
               "END:VEVENT")
        events, _ = ics.parse_ics(wrap(raw))
        assert events[0]["summary"] == "Middag, vin og kage"
        assert events[0]["location"] == "Vej 1\nOpgang B"

    def test_ignores_valarm_properties(self):
        """A VALARM carries its own TRIGGER — it must not leak into the event."""
        raw = ("BEGIN:VEVENT\r\n"
               "UID:alarm@example.test\r\n"
               "DTSTART;TZID=Europe/Copenhagen:20260914T100000\r\n"
               "DTEND;TZID=Europe/Copenhagen:20260914T110000\r\n"
               "SUMMARY:Moede\r\n"
               "BEGIN:VALARM\r\n"
               "ACTION:DISPLAY\r\n"
               "SUMMARY:Reminder\r\n"
               "END:VALARM\r\n"
               "END:VEVENT")
        events, _ = ics.parse_ics(wrap(raw))
        assert len(events) == 1
        assert events[0]["summary"] == "Moede"

    def test_skips_vtimezone_block(self):
        vtz = ("BEGIN:VTIMEZONE\r\n"
               "TZID:Europe/Copenhagen\r\n"
               "BEGIN:DAYLIGHT\r\n"
               "DTSTART:19700329T020000\r\n"
               "TZOFFSETFROM:+0100\r\n"
               "END:DAYLIGHT\r\n"
               "END:VTIMEZONE")
        events, _ = ics.parse_ics(wrap(vtz, TIMED))
        assert len(events) == 1
        assert events[0]["uid"] == "timed@example.test"


class TestValueParsing:
    def test_parses_tzid_datetime(self):
        dt = ics.parse_dt("20260914T100000",
                          {"TZID": "Europe/Copenhagen"}, UTC)
        assert dt == datetime(2026, 9, 14, 10, 0, tzinfo=CPH)

    def test_parses_utc_datetime(self):
        dt = ics.parse_dt("20260918T060000Z", {}, CPH)
        assert dt == datetime(2026, 9, 18, 6, 0, tzinfo=UTC)

    def test_floating_time_uses_calendar_zone(self):
        """No TZID and no Z — interpreted in the calendar's own zone."""
        dt = ics.parse_dt("20260914T100000", {}, CPH)
        assert dt == datetime(2026, 9, 14, 10, 0, tzinfo=CPH)

    def test_parses_date_value(self):
        assert ics.parse_dt("20260915", {"VALUE": "DATE"}, CPH) == date(2026, 9, 15)

    def test_unknown_tzid_falls_back(self):
        """Windows zone names appear in non-Google exports."""
        dt = ics.parse_dt("20260914T100000",
                          {"TZID": "Romance Standard Time"}, CPH)
        assert dt == datetime(2026, 9, 14, 10, 0, tzinfo=CPH)

    def test_quoted_param_with_colon(self):
        """TZID="GMT+01:00" — the colon inside quotes is not the separator."""
        line = 'DTSTART;TZID="GMT+01:00":20260914T100000'
        name, params, value = ics._split_property(line)
        assert name == "DTSTART"
        assert params["TZID"] == "GMT+01:00"
        assert value == "20260914T100000"

    @pytest.mark.parametrize("text,expected", [
        ("PT1H", timedelta(hours=1)),
        ("PT45M", timedelta(minutes=45)),
        ("P1D", timedelta(days=1)),
        ("P1DT2H30M", timedelta(days=1, hours=2, minutes=30)),
        ("P2W", timedelta(weeks=2)),
        ("-PT15M", timedelta(minutes=-15)),
    ])
    def test_parses_durations(self, text, expected):
        assert ics.parse_duration(text) == expected

    def test_rejects_bad_duration(self):
        assert ics.parse_duration("banana") is None


class TestUntilNormalisation:
    def test_adds_z_for_aware_dtstart(self):
        out = ics.normalize_until("FREQ=WEEKLY;UNTIL=20261231T235959", True)
        assert out == "FREQ=WEEKLY;UNTIL=20261231T235959Z"

    def test_expands_date_until_for_aware_dtstart(self):
        out = ics.normalize_until("FREQ=WEEKLY;UNTIL=20261231", True)
        assert out == "FREQ=WEEKLY;UNTIL=20261231T235959Z"

    def test_strips_z_for_naive_dtstart(self):
        out = ics.normalize_until("FREQ=YEARLY;UNTIL=20261231T235959Z", False)
        assert out == "FREQ=YEARLY;UNTIL=20261231T235959"

    def test_leaves_rule_without_until(self):
        assert ics.normalize_until("FREQ=WEEKLY;BYDAY=MO", True) == \
            "FREQ=WEEKLY;BYDAY=MO"


class TestSingleEvents:
    def test_timed_event_in_window(self):
        occ = expand(wrap(TIMED), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert len(occ) == 1
        assert occ[0]["summary"] == "Tandlaege"
        assert occ[0]["location"] == "Hovedgaden 4"
        assert occ[0]["all_day"] is False
        assert occ[0]["start"] == datetime(2026, 9, 14, 10, 0, tzinfo=CPH)
        assert occ[0]["end"] == datetime(2026, 9, 14, 11, 30, tzinfo=CPH)

    def test_event_outside_window_is_dropped(self):
        occ = expand(wrap(TIMED), datetime(2026, 10, 1, 0, 0, tzinfo=CPH))
        assert occ == []

    def test_utc_event_is_converted_to_display_zone(self):
        raw = """BEGIN:VEVENT
UID:utc@example.test
DTSTART:20260918T060000Z
DURATION:PT45M
SUMMARY:UTC event
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 18, 0, 0, tzinfo=CPH))
        assert occ[0]["start"] == datetime(2026, 9, 18, 8, 0, tzinfo=CPH)
        assert occ[0]["end"] == datetime(2026, 9, 18, 8, 45, tzinfo=CPH)

    def test_cancelled_event_is_skipped(self):
        raw = """BEGIN:VEVENT
UID:gone@example.test
DTSTART;TZID=Europe/Copenhagen:20260916T090000
DTEND;TZID=Europe/Copenhagen:20260916T100000
STATUS:CANCELLED
SUMMARY:Aflyst
END:VEVENT"""
        assert expand(wrap(raw), datetime(2026, 9, 16, 0, 0, tzinfo=CPH)) == []

    def test_event_in_progress_is_still_listed(self):
        """A meeting that began before the window still belongs on screen."""
        occ = expand(wrap(TIMED), datetime(2026, 9, 14, 10, 45, tzinfo=CPH))
        assert len(occ) == 1

    def test_event_without_dtstart_is_dropped(self):
        raw = """BEGIN:VEVENT
UID:broken@example.test
SUMMARY:No start
END:VEVENT"""
        events, _ = ics.parse_ics(wrap(raw))
        assert events == []


class TestAllDayEvents:
    def test_dtend_is_exclusive(self):
        """DTSTART=15, DTEND=17 covers the 15th and 16th, not the 17th."""
        raw = """BEGIN:VEVENT
UID:holiday@example.test
DTSTART;VALUE=DATE:20260915
DTEND;VALUE=DATE:20260917
SUMMARY:Ferie
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert occ[0]["all_day"] is True
        assert occ[0]["start"] == date(2026, 9, 15)
        assert occ[0]["end"] == date(2026, 9, 16)

    def test_single_day_event_starts_and_ends_same_day(self):
        raw = """BEGIN:VEVENT
UID:oneday@example.test
DTSTART;VALUE=DATE:20260915
DTEND;VALUE=DATE:20260916
SUMMARY:Fridag
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert occ[0]["start"] == occ[0]["end"] == date(2026, 9, 15)

    def test_all_day_without_dtend_lasts_one_day(self):
        raw = """BEGIN:VEVENT
UID:bare@example.test
DTSTART;VALUE=DATE:20260915
SUMMARY:Mærkedag
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert occ[0]["start"] == occ[0]["end"] == date(2026, 9, 15)

    def test_ongoing_multi_day_event_is_listed(self):
        raw = """BEGIN:VEVENT
UID:long@example.test
DTSTART;VALUE=DATE:20260910
DTEND;VALUE=DATE:20260920
SUMMARY:Ferie
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 12, 0, tzinfo=CPH))
        assert len(occ) == 1
        assert occ[0]["start"] == date(2026, 9, 10)


class TestRecurrence:
    WEEKLY = """BEGIN:VEVENT
UID:weekly@example.test
DTSTART;TZID=Europe/Copenhagen:20260907T190000
DTEND;TZID=Europe/Copenhagen:20260907T203000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Korprove
END:VEVENT"""

    def test_weekly_expands_across_window(self):
        """Every Monday inside the window, and none past its end.

        The window closes at midnight on 5 Oct, so that Monday's 19:00
        slot falls outside it.
        """
        occ = expand(wrap(self.WEEKLY),
                     datetime(2026, 9, 14, 0, 0, tzinfo=CPH), days=21)
        assert [o["start"].date() for o in occ] == [
            date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28),
        ]

    def test_recurrence_keeps_wall_clock_across_dst(self):
        """A 19:00 weekly slot stays 19:00 after the October DST change."""
        occ = expand(wrap(self.WEEKLY),
                     datetime(2026, 10, 19, 0, 0, tzinfo=CPH), days=14)
        times = {o["start"].strftime("%H:%M") for o in occ}
        offsets = {o["start"].utcoffset() for o in occ}
        assert times == {"19:00"}
        assert len(offsets) == 2, "window should straddle the DST change"

    def test_exdate_removes_one_occurrence(self):
        raw = self.WEEKLY.replace(
            "SUMMARY:Korprove",
            "EXDATE;TZID=Europe/Copenhagen:20260921T190000\nSUMMARY:Korprove")
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH),
                     days=21)
        assert date(2026, 9, 21) not in [o["start"].date() for o in occ]
        assert date(2026, 9, 28) in [o["start"].date() for o in occ]

    def test_recurrence_id_replaces_rather_than_duplicates(self):
        """An edited instance must not appear twice — moved and original."""
        override = """BEGIN:VEVENT
UID:weekly@example.test
RECURRENCE-ID;TZID=Europe/Copenhagen:20260914T190000
DTSTART;TZID=Europe/Copenhagen:20260914T203000
DTEND;TZID=Europe/Copenhagen:20260914T220000
SUMMARY:Korprove (flyttet)
END:VEVENT"""
        occ = expand(wrap(self.WEEKLY, override),
                     datetime(2026, 9, 14, 0, 0, tzinfo=CPH), days=7)
        assert len(occ) == 1
        assert occ[0]["summary"] == "Korprove (flyttet)"
        assert occ[0]["start"] == datetime(2026, 9, 14, 20, 30, tzinfo=CPH)

    def test_cancelled_instance_of_series_disappears(self):
        override = """BEGIN:VEVENT
UID:weekly@example.test
RECURRENCE-ID;TZID=Europe/Copenhagen:20260914T190000
DTSTART;TZID=Europe/Copenhagen:20260914T190000
DTEND;TZID=Europe/Copenhagen:20260914T203000
STATUS:CANCELLED
SUMMARY:Korprove
END:VEVENT"""
        occ = expand(wrap(self.WEEKLY, override),
                     datetime(2026, 9, 14, 0, 0, tzinfo=CPH), days=7)
        assert occ == []

    def test_yearly_all_day_birthday(self):
        raw = """BEGIN:VEVENT
UID:bday@example.test
DTSTART;VALUE=DATE:20000920
RRULE:FREQ=YEARLY
SUMMARY:Fars fodselsdag
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert len(occ) == 1
        assert occ[0]["start"] == date(2026, 9, 20)
        assert occ[0]["all_day"] is True

    def test_until_bounded_series_stops(self):
        raw = """BEGIN:VEVENT
UID:until@example.test
DTSTART;TZID=Europe/Copenhagen:20260907T190000
DTEND;TZID=Europe/Copenhagen:20260907T200000
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260921T235959Z
SUMMARY:Kursus
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH),
                     days=28)
        assert [o["start"].date() for o in occ] == [
            date(2026, 9, 14), date(2026, 9, 21)]

    def test_count_bounded_series_stops(self):
        raw = """BEGIN:VEVENT
UID:count@example.test
DTSTART;TZID=Europe/Copenhagen:20260914T190000
DTEND;TZID=Europe/Copenhagen:20260914T200000
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Tre dage
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert len(occ) == 3

    def test_unparseable_rrule_keeps_the_base_event(self):
        """One bad rule must not blank the whole calendar."""
        raw = """BEGIN:VEVENT
UID:bad@example.test
DTSTART;TZID=Europe/Copenhagen:20260914T190000
DTEND;TZID=Europe/Copenhagen:20260914T200000
RRULE:FREQ=NONSENSE;BYDAY=XX
SUMMARY:Daarlig regel
END:VEVENT"""
        occ = expand(wrap(raw), datetime(2026, 9, 14, 0, 0, tzinfo=CPH))
        assert len(occ) == 1
        assert occ[0]["summary"] == "Daarlig regel"


class TestOrdering:
    def test_all_day_events_sort_before_timed_on_same_day(self):
        allday = """BEGIN:VEVENT
UID:a@example.test
DTSTART;VALUE=DATE:20260914
SUMMARY:Helligdag
END:VEVENT"""
        occ = expand(wrap(TIMED, allday),
                     datetime(2026, 9, 14, 0, 0, tzinfo=CPH), days=1)
        assert [o["summary"] for o in occ] == ["Helligdag", "Tandlaege"]

    def test_timed_events_sort_by_start(self):
        later = TIMED.replace("T100000", "T160000").replace(
            "T113000", "T170000").replace("timed@", "later@").replace(
            "Tandlaege", "Senere")
        earlier = TIMED.replace("T100000", "T080000").replace(
            "T113000", "T090000").replace("timed@", "early@").replace(
            "Tandlaege", "Tidligere")
        occ = expand(wrap(later, earlier),
                     datetime(2026, 9, 14, 0, 0, tzinfo=CPH), days=1)
        assert [o["summary"] for o in occ] == ["Tidligere", "Senere"]
