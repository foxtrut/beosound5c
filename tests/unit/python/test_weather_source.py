"""Tests for the weather source's forecast summarising.

DMI's EDR API returns the whole model run, whose first entry can be several
hours in the past relative to "now" — a real user caught `current` reporting
the model run's oldest point (17.5°C) instead of the hour closest to now
(14.3°C, matching what DMI.dk itself showed at the time).
"""
from __future__ import annotations

import datetime as real_datetime
from zoneinfo import ZoneInfo

import pytest

from sources.weather import WeatherService, kelvin_to_c

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
    return {
        "time": real_datetime.datetime(2026, 9, 13, hour, tzinfo=TZ),
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


@pytest.mark.parametrize("kelvin,celsius", [(273.15, 0.0), (287.3, 14.2), (300.0, 26.9)])
def test_kelvin_to_c(kelvin, celsius):
    assert kelvin_to_c(kelvin) == celsius
