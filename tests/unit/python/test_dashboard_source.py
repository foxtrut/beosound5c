"""Dashboard source (services/sources/dashboard.py): the /proc readers, the
now-playing summary, and the /api/dashboard envelope."""

import asyncio
import time

import pytest

from sources import dashboard


@pytest.fixture
def proc(tmp_path):
    (tmp_path / "uptime").write_text("277020.55 1083422.10\n")
    (tmp_path / "loadavg").write_text("0.31 0.27 0.22 1/312 4242\n")
    (tmp_path / "meminfo").write_text(
        "MemTotal:        3884000 kB\n"
        "MemFree:          900000 kB\n"
        "MemAvailable:    2408080 kB\n"
        "Buffers:           12000 kB\n"
    )
    return str(tmp_path)


def test_reads_uptime_load_and_memory(proc):
    assert dashboard.read_uptime(proc) == 277020
    assert dashboard.read_load(proc) == [0.31, 0.27, 0.22]
    assert dashboard.read_memory(proc) == {"total_mb": 3792, "used_pct": 38}


def test_missing_proc_gives_none(tmp_path):
    empty = str(tmp_path)
    assert dashboard.read_uptime(empty) is None
    assert dashboard.read_load(empty) is None
    assert dashboard.read_memory(empty) is None
    assert dashboard.read_cpu_temp(str(tmp_path / "temp")) is None


def test_meminfo_without_available_gives_none(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 10 kB\n")
    assert dashboard.read_memory(str(tmp_path)) is None


def test_cpu_temp_is_millidegrees(tmp_path):
    path = tmp_path / "temp"
    path.write_text("48234\n")
    assert dashboard.read_cpu_temp(str(path)) == 48.2


def test_disk_of_an_existing_path(tmp_path):
    disk = dashboard.read_disk(str(tmp_path))
    assert disk["total_gb"] >= disk["free_gb"] >= 0
    assert 0 <= disk["used_pct"] <= 100


def test_disk_of_a_missing_path():
    assert dashboard.read_disk("/no/such/path/here") is None


def test_playing_summary():
    status = {
        "active_source_name": "Spotify",
        "media": {"state": "playing", "title": "Teardrop", "artist": "Massive Attack",
                  "album": "Mezzanine", "artwork": "x"},
    }
    assert dashboard.summarise_playing(status) == {
        "state": "playing", "title": "Teardrop", "artist": "Massive Attack",
        "album": "Mezzanine", "source": "Spotify",
    }


@pytest.mark.parametrize("status", [
    None, {}, {"media": {"state": "idle", "title": "Old"}},
    {"media": {"state": "playing", "title": ""}}, {"media": {"state": "stopped", "title": "X"}},
])
def test_nothing_playing(status):
    assert dashboard.summarise_playing(status) is None


def test_snapshot_without_router(monkeypatch):
    svc = dashboard.DashboardService()

    async def no_router():
        return None

    monkeypatch.setattr(svc, "_router_status", no_router)
    monkeypatch.setattr(svc, "_electricity_config", lambda: ("", "DK1"))
    snap = asyncio.run(svc.snapshot())
    assert snap["playing"] is None
    assert snap["volume"] is None
    for key in ("device", "hostname", "ip", "time", "uptime_s", "load",
                "memory", "cpu_temp_c", "disk", "output", "electricity"):
        assert key in snap


def test_snapshot_with_router(monkeypatch):
    svc = dashboard.DashboardService()

    async def router():
        return {"volume": 31.6, "output_device": "BeoLab 5", "active_source_name": "Radio",
                "media": {"state": "paused", "title": "P1 Morgen"}}

    monkeypatch.setattr(svc, "_router_status", router)
    monkeypatch.setattr(svc, "_electricity_config", lambda: ("", "DK1"))
    snap = asyncio.run(svc.snapshot())
    assert snap["volume"] == 32
    assert snap["output"] == "BeoLab 5"
    assert snap["playing"]["state"] == "paused"
    assert snap["playing"]["source"] == "Radio"


# ── Electricity prices ──

def _step(local, total, forecast=None):
    p = {"localDate": local, "price": {"total": total}}
    if forecast is not None:
        p["forecast"] = forecast
    return p


def test_quarter_hours_average_to_the_hour():
    prices = [_step("2026-10-10T00:00:00", 0.30), _step("2026-10-10T00:15:00", 0.38),
              _step("2026-10-10T00:30:00", 0.40), _step("2026-10-10T00:45:00", 0.36),
              _step("2026-10-10T01:00:00", 0.35)]
    assert dashboard.hourly_prices(prices, "2026-10-10") == [
        {"hour": 0, "price": 0.36, "forecast": False},
        {"hour": 1, "price": 0.35, "forecast": False},
    ]


def test_forecast_hours_are_marked_and_other_days_left_out():
    prices = [_step("2026-10-10T23:00:00", 1.46), _step("2026-10-11T00:00:00", 1.12, True),
              _step("2026-10-12T00:00:00", 1.56, True)]
    assert dashboard.hourly_prices(prices, "2026-10-11") == [
        {"hour": 0, "price": 1.12, "forecast": True}]


def test_malformed_steps_are_skipped():
    prices = [{"localDate": "2026-10-10T05:00:00"}, {"price": {"total": 1}},
              _step("2026-10-10Tx", 1.0), _step("2026-10-10T06:00:00", "0.5")]
    assert dashboard.hourly_prices(prices, "2026-10-10") == [
        {"hour": 6, "price": 0.5, "forecast": False}]


def test_days_are_today_and_tomorrow_by_local_date():
    prices = [_step("2026-10-09T12:00:00", 9.0), _step("2026-10-10T12:00:00", 1.0),
              _step("2026-10-11T12:00:00", 2.0, True), _step("2026-10-12T12:00:00", 3.0, True)]
    late = time.mktime((2026, 10, 10, 23, 30, 0, 0, 0, -1))
    days = dashboard.electricity_days(prices, now=late)
    assert [d["date"] for d in days] == ["2026-10-10", "2026-10-11"]


def test_days_without_prices_are_left_out():
    prices = [_step("2026-10-10T12:00:00", 1.0)]
    noon = time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))
    assert [d["date"] for d in dashboard.electricity_days(prices, now=noon)] == ["2026-10-10"]


def test_no_net_company_means_no_prices(monkeypatch):
    svc = dashboard.DashboardService()
    monkeypatch.setattr(svc, "_electricity_config", lambda: ("", "DK1"))
    assert asyncio.run(svc._electricity()) is None


def test_prices_are_cached_and_a_failure_keeps_the_last_list(monkeypatch):
    svc = dashboard.DashboardService()
    monkeypatch.setattr(svc, "_electricity_config", lambda: ("dinel_c", "DK1"))
    calls = []
    today = time.strftime("%Y-%m-%dT10:00:00")

    async def fetch(supplier, area):
        calls.append((supplier, area))
        if len(calls) > 1:
            raise OSError("down")
        return [_step(today, 0.5)], "Dinel C"

    monkeypatch.setattr(svc, "_fetch_prices", fetch)
    first = asyncio.run(svc._electricity())
    assert first["supplier"] == "Dinel C" and first["days"][0]["hours"][0]["price"] == 0.5
    asyncio.run(svc._electricity())
    assert len(calls) == 1                      # served from cache

    svc._prices_at = svc._prices_tried = time.monotonic() - dashboard.PRICES_TTL - 1
    again = asyncio.run(svc._electricity())
    assert len(calls) == 2                      # tried again once stale ...
    assert again["days"] == first["days"]       # ... and kept the old list
