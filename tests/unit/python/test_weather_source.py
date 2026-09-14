"""Tests for the weather source's forecast summarising and refresh schedule.

DMI's EDR API returns the whole model run, whose first entry can be several
hours in the past relative to "now" — a real user caught `current` reporting
the model run's oldest point (17.5°C) instead of the hour closest to now
(14.3°C, matching what DMI.dk itself showed at the time).

DMI also answers 429 "Server is busy" for stretches; a failed fetch used to
wait the full 30-minute refresh interval before trying again.
"""
from __future__ import annotations

import asyncio
import datetime as real_datetime
from zoneinfo import ZoneInfo

import pytest

from sources.weather import REFRESH_INTERVAL, WeatherService, kelvin_to_c, next_delay

TZ = ZoneInfo("Europe/Copenhagen")


class _FrozenDateTime(real_datetime.datetime):
    """Subclassing (not mocking) keeps fromisoformat/other classmethods real."""
    _frozen = real_datetime.datetime(2026, 9, 13, 19, 25, tzinfo=TZ)

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz else cls._frozen


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr("sources.weather.datetime", _FrozenDateTime)


def _step(hour, temp_c, cum_mm=0.0, cloud_pct=50):
    return _step_at(real_datetime.datetime(2026, 9, 13, hour, tzinfo=TZ), temp_c, cum_mm, cloud_pct)


def _step_at(dt, temp_c, cum_mm=0.0, cloud_pct=50):
    return {
        "time": dt,
        "temp_c": temp_c,
        "precip_cum_mm": cum_mm,
        "cloud_pct": cloud_pct,
        "wind_ms": 3.0,
    }


def test_current_uses_the_hour_closest_to_now_not_the_oldest_step(frozen_now):
    """Regression: current used to be steps[0] (the model run's start)."""
    steps = [
        _step(14, 17.5),
        _step(15, 16.6),
        _step(18, 15.0),
        _step(19, 14.3),  # "now" is 19:25 — this is the current hour
        _step(20, 13.6),
    ]
    svc = WeatherService()
    summary = svc._build_summary(steps)
    assert summary["current"]["temp_c"] == 14.3


def test_current_picks_closest_step_when_now_is_past_the_whole_forecast(frozen_now):
    """If every step is stale (now is past all of them), "current" should be
    the most recent one (15:00), not the oldest (14:00)."""
    steps = [_step(14, 17.5), _step(15, 16.6)]
    svc = WeatherService()
    summary = svc._build_summary(steps)
    assert summary["current"]["temp_c"] == 16.6


def test_will_rain_true_when_hourly_delta_exceeds_threshold(frozen_now):
    steps = [
        _step(19, 14.0, cum_mm=0.0),
        _step(20, 13.5, cum_mm=0.05),   # below RAIN_THRESHOLD_MM (0.1)
        _step(21, 13.0, cum_mm=1.2),    # +1.15mm — clears the threshold
    ]
    svc = WeatherService()
    summary = svc._build_summary(steps)
    assert summary["today"]["will_rain"] is True
    assert summary["today"]["rain_mm"] == 1.2


def test_no_rain_when_precipitation_never_accumulates(frozen_now):
    steps = [_step(19, 14.0, cum_mm=0.0), _step(20, 13.5, cum_mm=0.0)]
    svc = WeatherService()
    summary = svc._build_summary(steps)
    assert summary["today"]["will_rain"] is False
    assert summary["today"]["rain_mm"] == 0.0


def test_hourly_list_has_a_fixed_length_spanning_past_midnight(monkeypatch):
    """Regression: hourly used to be capped to "whatever's left of today",
    so in the evening it shrank to almost nothing — and DMI vs. Open-Meteo,
    which can return different total step counts, visibly disagreed on how
    many hours to show as a result. It's now a fixed lookahead (HOURLY_COUNT)
    regardless of the calendar-day boundary or which provider answered."""
    class _LateNight(real_datetime.datetime):
        _frozen = real_datetime.datetime(2026, 9, 13, 22, 30, tzinfo=TZ)

        @classmethod
        def now(cls, tz=None):
            return cls._frozen.astimezone(tz) if tz else cls._frozen
    monkeypatch.setattr("sources.weather.datetime", _LateNight)

    start = real_datetime.datetime(2026, 9, 13, 22, tzinfo=TZ)
    steps = [_step_at(start + real_datetime.timedelta(hours=h), 10.0 + h) for h in range(15)]

    from sources.weather import HOURLY_COUNT
    summary = WeatherService()._build_summary(steps)
    assert len(summary["hourly"]) == HOURLY_COUNT
    assert summary["hourly"][0]["time"] == "22:00"
    assert summary["hourly"][-1]["time"] == "07:00"  # crosses into the next day
    # "Today"'s own summary stays scoped to the calendar day (22:00, 23:00
    # only) — only the hourly list's cutoff changed.
    assert summary["today"]["temp_min_c"] == 10.0
    assert summary["today"]["temp_max_c"] == 11.0


@pytest.mark.parametrize("kelvin,celsius", [(273.15, 0.0), (287.3, 14.2), (300.0, 26.9)])
def test_kelvin_to_c(kelvin, celsius):
    assert kelvin_to_c(kelvin) == celsius


def test_next_delay_is_the_normal_interval_after_a_success():
    assert next_delay(0) == REFRESH_INTERVAL


def test_next_delay_backs_off_from_a_minute_up_to_the_normal_interval():
    delays = [next_delay(n) for n in range(1, 10)]
    assert delays[:3] == [60, 120, 300]
    assert delays == sorted(delays)
    assert delays[-1] == REFRESH_INTERVAL


def test_refresh_loop_retries_quickly_until_a_fetch_succeeds(monkeypatch):
    """A 429, then a raised error, then success: short waits, then the normal
    interval once the forecast is in."""
    outcomes = [False, RuntimeError("connection reset"), True]
    svc = WeatherService()

    async def fake_fetch():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if not outcomes:
            raise asyncio.CancelledError  # stop the endless loop

    monkeypatch.setattr(svc, "_fetch_forecast", fake_fetch)
    monkeypatch.setattr("sources.weather.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._refresh_loop())
    assert sleeps == [60, 120, REFRESH_INTERVAL]


def test_open_meteo_steps_accumulate_hourly_precipitation(frozen_now):
    """Open-Meteo gives rain per hour; _build_summary diffs an accumulated
    total (DMI's shape), so the parser has to accumulate it."""
    base = int(real_datetime.datetime(2026, 9, 13, 19, tzinfo=TZ).timestamp())
    data = {"hourly": {
        "time": [base + h * 3600 for h in range(3)],
        "temperature_2m": [14.0, 13.5, 13.0],
        "precipitation": [0.0, 0.05, 1.15],
        "cloud_cover": [40, 80, 100],
        "wind_speed_10m": [3.1, 3.4, 4.0],
    }}
    svc = WeatherService()
    steps = svc._parse_open_meteo_steps(data)
    assert [s["time"].hour for s in steps] == [19, 20, 21]
    assert [s["precip_cum_mm"] for s in steps] == [0.0, 0.05, 1.2]

    summary = svc._build_summary(steps, "open_meteo")
    assert summary["provider"] == "open_meteo"
    assert summary["current"] == {"temp_c": 14.0, "cloud_pct": 40, "wind_ms": 3.1}
    assert summary["today"]["will_rain"] is True
    assert summary["today"]["rain_mm"] == 1.2


def _service_with_providers(dmi, open_meteo):
    """A service whose two fetchers return (or raise) the given outcomes."""
    svc = WeatherService()
    calls = []

    def fetcher(name, outcome):
        async def fetch():
            calls.append(name)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return fetch

    svc._fetch_dmi_steps = fetcher("dmi", dmi)
    svc._fetch_open_meteo_steps = fetcher("open_meteo", open_meteo)
    return svc, calls


def test_fetch_uses_dmi_and_skips_open_meteo_when_dmi_answers(frozen_now):
    svc, calls = _service_with_providers([_step(19, 14.3)], [_step(19, 99.0)])
    assert asyncio.run(svc._fetch_forecast()) is True
    assert calls == ["dmi"]
    assert svc._forecast["provider"] == "dmi"
    assert svc._forecast["current"]["temp_c"] == 14.3


@pytest.mark.parametrize("dmi_outcome", [[], TimeoutError()], ids=["refused", "timed-out"])
def test_fetch_falls_back_to_open_meteo_when_dmi_fails(frozen_now, dmi_outcome):
    """DMI refusing (429 → no steps) or raising must not leave the page empty."""
    svc, calls = _service_with_providers(dmi_outcome, [_step(19, 12.0)])
    assert asyncio.run(svc._fetch_forecast()) is True
    assert calls == ["dmi", "open_meteo"]
    assert svc._forecast["provider"] == "open_meteo"
    assert svc._forecast["current"]["temp_c"] == 12.0


def test_fetch_keeps_the_last_forecast_when_both_providers_fail(frozen_now):
    svc, _ = _service_with_providers([], RuntimeError("no route"))
    svc._forecast = {"provider": "dmi", "current": {"temp_c": 14.3}}
    assert asyncio.run(svc._fetch_forecast()) is False
    assert svc._forecast == {"provider": "dmi", "current": {"temp_c": 14.3}}
