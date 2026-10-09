"""Dashboard source (services/sources/dashboard.py): the /proc readers, the
now-playing summary, and the /api/dashboard envelope."""

import asyncio

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
    snap = asyncio.run(svc.snapshot())
    assert snap["playing"] is None
    assert snap["volume"] is None
    for key in ("device", "hostname", "ip", "time", "uptime_s", "load",
                "memory", "cpu_temp_c", "disk", "output"):
        assert key in snap


def test_snapshot_with_router(monkeypatch):
    svc = dashboard.DashboardService()

    async def router():
        return {"volume": 31.6, "output_device": "BeoLab 5", "active_source_name": "Radio",
                "media": {"state": "paused", "title": "P1 Morgen"}}

    monkeypatch.setattr(svc, "_router_status", router)
    snap = asyncio.run(svc.snapshot())
    assert snap["volume"] == 32
    assert snap["output"] == "BeoLab 5"
    assert snap["playing"]["state"] == "paused"
    assert snap["playing"]["source"] == "Radio"
