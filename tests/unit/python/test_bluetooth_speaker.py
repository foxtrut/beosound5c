"""Tests for lib/bluetooth_speaker.py and lib/volume_adapters/bluetooth.py.

Pins parsing of bluetoothctl/pactl output (which devices count as speakers,
how the speaker's sink and the tone chain's stream are found) and the
adapter's connection handling: connect when the sink is missing, throttle
retries, route a (re)appearing sink once and carry the volume over, stay
disconnected in standby.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[3] / "services"
sys.path.insert(0, str(SERVICES_DIR))

from lib import bluetooth_speaker as bts  # noqa: E402
from lib.volume_adapters import bluetooth as bt_volume  # noqa: E402
from lib.volume_adapters.bluetooth import BluetoothVolume  # noqa: E402


MAC = "00:11:22:33:44:55"

SPEAKER_INFO = """Device 00:11:22:33:44:55 (public)
\tName: Kanto YU4
\tAlias: Kanto YU4
\tClass: 0x00240414
\tIcon: audio-card
\tPaired: yes
\tBonded: yes
\tTrusted: yes
\tBlocked: no
\tConnected: no
\tLegacyPairing: no
\tUUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)
\tUUID: A/V Remote Control        (0000110e-0000-1000-8000-00805f9b34fb)
"""

REMOTE_INFO = """Device 48:D0:CF:AA:BB:CC (public)
\tName: BEORC
\tAlias: BEORC
\tAppearance: 0x0180
\tIcon: input-gaming
\tPaired: yes
\tConnected: yes
\tUUID: Human Interface Device    (00001812-0000-1000-8000-00805f9b34fb)
"""

SINKS_SHORT = (
    "48\talsa_output.platform-107c706400.hdmi.hdmi-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    "63\tbeo_tone_sink\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n"
    "91\tbluez_output.00_11_22_33_44_55.1\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n"
)

SINK_INPUTS = """Sink Input #70
\tDriver: PipeWire
\tProperties:
\t\tmedia.name = "BeoSound 5c Tone"
\t\tnode.name = "beo_tone_sink.output"
Sink Input #85
\tProperties:
\t\tnode.name = "mpv"
"""


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Parsing ───────────────────────────────────────────────────────────────

def test_normalize_mac():
    assert bts.normalize_mac("00-11-22-aa-bb-cc") == "00:11:22:AA:BB:CC"
    assert bts.normalize_mac(" 00:11:22:33:44:55 ") == MAC
    assert bts.normalize_mac("not a mac") == ""
    assert bts.normalize_mac(None) == ""


def test_parse_devices_skips_noise():
    out = ("\x1b[0;94m[bluetooth]\x1b[0m# \n"
           "Device 00:11:22:33:44:55 Kanto YU4\n"
           "Device 48:D0:CF:AA:BB:CC BEORC\n"
           "[CHG] Controller DC:A6:32:00:00:00 Discovering: yes\n")
    assert bts.parse_devices(out) == [(MAC, "Kanto YU4"), ("48:D0:CF:AA:BB:CC", "BEORC")]


def test_parse_info():
    info = bts.parse_info(SPEAKER_INFO)
    assert info["name"] == "Kanto YU4"
    assert info["paired"] and info["trusted"] and not info["connected"]
    assert info["icon"] == "audio-card"
    assert info["class"] == 0x240414
    assert info["uuids"][0].startswith("0000110b")


def test_parse_info_unknown_device():
    assert bts.parse_info("Device 00:11:22:33:44:66 not available\n") is None
    assert bts.parse_info("") is None


def test_is_audio_sink():
    assert bts.is_audio_sink(bts.parse_info(SPEAKER_INFO))
    assert not bts.is_audio_sink(bts.parse_info(REMOTE_INFO))
    assert not bts.is_audio_sink(None)
    # Unpaired speakers found by a scan may carry only one of the hints.
    assert bts.is_audio_sink({"icon": "", "uuids": ["0000110b-0000-1000-8000-00805f9b34fb"], "class": None})
    assert bts.is_audio_sink({"icon": "", "uuids": [], "class": 0x240414})
    assert not bts.is_audio_sink({"icon": "phone", "uuids": [], "class": 0x5a020c})


def test_find_bluez_sink_matches_both_naming_styles():
    assert bts.find_bluez_sink(SINKS_SHORT, MAC) == "bluez_output.00_11_22_33_44_55.1"
    legacy = "12\tbluez_output.00_11_22_33_44_55.a2dp-sink\tPipeWire\ts16le 2ch 44100Hz\tIDLE\n"
    assert bts.find_bluez_sink(legacy, MAC) == "bluez_output.00_11_22_33_44_55.a2dp-sink"
    assert bts.find_bluez_sink(SINKS_SHORT, "AA:AA:AA:AA:AA:AA") is None
    # No speaker configured must not grab whichever Bluetooth sink exists.
    assert bts.find_bluez_sink(SINKS_SHORT, "") is None


def test_find_sink_input():
    assert bts.find_sink_input(SINK_INPUTS, "beo_tone_sink.output") == "70"
    assert bts.find_sink_input(SINK_INPUTS, "mpv") == "85"
    assert bts.find_sink_input(SINK_INPUTS, "shairport-sync") is None


def test_parse_volume_percent():
    out = "Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: 26214 /  40% / -23.88 dB\n"
    assert bts.parse_volume_percent(out) == 40
    assert bts.parse_volume_percent("") is None


def test_is_audio_sink_by_le_appearance():
    speaker = bts.parse_info("Device 11:22:33:44:55:66 (random)\n\tName: Sonos Roam\n\tAppearance: 0x0841\n")
    assert speaker["appearance"] == 0x0841
    assert bts.is_audio_sink(speaker)
    watch = {"icon": "", "uuids": [], "class": None, "appearance": 0x00c1}
    assert not bts.is_audio_sink(watch)


def test_parse_controller():
    show = ("Controller 2C:CF:67:00:11:22 (public)\n\tManufacturer: 0x0131 (305)\n"
            "\tName: beosound5c\n\tPowered: no\n\tDiscovering: no\n")
    assert bts.parse_controller(show) == {
        "address": "2C:CF:67:00:11:22", "powered": False, "discovering": False,
        "classic": False, "a2dp": False}
    full = show.replace("\tPowered: no", "\tClass: 0x006c0000 (7077888)\n\tPowered: yes") + \
        "\tUUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)\n"
    controller = bts.parse_controller(full)
    assert controller["classic"] and controller["a2dp"] and controller["powered"]
    assert bts.parse_controller(show.replace("Powered: no", "Powered: yes"))["powered"]
    assert bts.parse_controller("No default controller available\n") is None
    assert bts.parse_controller("") is None


def test_scan_messages_keeps_errors_and_new_devices():
    out = ("Discovery started\n"
           "[CHG] Controller 2C:CF:67:00:11:22 Discovering: yes\n"
           "[NEW] Device 00:11:22:33:44:55 Kanto YU4\n"
           "[CHG] Device 00:11:22:33:44:55 RSSI: -60\n"
           "[CHG] Device 5A:11:22:33:44:55 ManufacturerData Key: 0x004c\n"
           "Failed to start discovery: org.bluez.Error.NotReady\n")
    assert bts.scan_messages(out) == [
        "Discovery started",
        "[CHG] Controller 2C:CF:67:00:11:22 Discovering: yes",
        "[NEW] Device 00:11:22:33:44:55 Kanto YU4",
        "Failed to start discovery: org.bluez.Error.NotReady",
    ]


def _fake_bluetoothctl(monkeypatch, responses):
    """Patch bts._run; responses maps a command's words (after --timeout N)
    to its output. Returns the list of commands run."""
    calls = []

    async def fake_run(*args, timeout=5.0):
        words = [a for a in args[1:] if a != "--timeout" and not a.isdigit()]
        calls.append(" ".join(words))
        return responses.get(" ".join(words), ""), 0
    monkeypatch.setattr(bts, "_run", fake_run)
    return calls


SHOW = "Controller 2C:CF:67:AC:0F:4A (public)\n\tClass: 0x006c0000\n\tPowered: yes\n"


def test_scan_asks_for_classic_inquiry(monkeypatch):
    calls = _fake_bluetoothctl(monkeypatch, {
        "show": SHOW,
        "scan bredr": "Discovery started\n[NEW] Device 00:11:22:33:44:55 Kanto YU4\n",
        "devices": "Device 00:11:22:33:44:55 Kanto YU4\n",
        f"info {MAC}": SPEAKER_INFO,
    })
    found = _run(bts.scan(12))
    assert calls[:2] == ["show", "scan bredr"]
    assert "scan on" not in calls
    assert found == [{"mac": MAC, "name": "Kanto YU4", "paired": True, "connected": False}]


def test_scan_falls_back_when_bredr_is_rejected(monkeypatch):
    calls = _fake_bluetoothctl(monkeypatch, {
        "show": SHOW,
        "scan bredr": "Invalid argument bredr\n",
    })
    assert _run(bts.scan(12)) == []
    assert calls[:3] == ["show", "scan bredr", "scan on"]


def test_scan_without_controller_returns_nothing(monkeypatch):
    calls = _fake_bluetoothctl(monkeypatch, {"show": "No default controller available\n"})
    assert _run(bts.scan(12)) == []
    assert calls == ["show"]


def test_describe():
    assert bts.describe(bts.parse_info(SPEAKER_INFO)) == "icon=audio-card class=0x240414 a2dp-sink"
    assert bts.describe(None) == "no info"


# ── Adapter ───────────────────────────────────────────────────────────────

class _FakeSpeaker:
    def __init__(self, sink=None, connect_ok=True):
        self.mac = MAC
        self.sink = sink
        self.connect_ok = connect_ok
        self.calls = []

    async def find_sink(self):
        return self.sink

    async def connect(self):
        self.calls.append("connect")
        if self.connect_ok:
            self.sink = "bluez_output.00_11_22_33_44_55.1"
        return self.connect_ok

    async def disconnect(self):
        self.calls.append("disconnect")
        self.sink = None

    async def route_to(self, sink):
        self.calls.append(("route", sink))
        return True

    async def set_volume(self, sink, percent):
        self.calls.append(("volume", percent))
        return True

    async def get_volume(self, sink):
        return 40


def _adapter(speaker):
    adapter = BluetoothVolume(MAC, 70, speaker=speaker)
    adapter._ensure_watch = lambda: None  # tests drive sync_once directly
    return adapter


def test_sync_connects_routes_and_restores_volume():
    speaker = _FakeSpeaker()
    adapter = _adapter(speaker)
    _run(adapter._apply_volume(35))          # not connected yet: remembered
    assert ("volume", 35) not in speaker.calls
    sink = _run(adapter.sync_once())
    assert sink == "bluez_output.00_11_22_33_44_55.1"
    assert speaker.calls == ["connect", ("route", sink), ("volume", 35)]


def test_sync_routes_once_while_sink_stays():
    speaker = _FakeSpeaker(sink="bluez_output.00_11_22_33_44_55.1")
    adapter = _adapter(speaker)
    _run(adapter.sync_once())
    _run(adapter.sync_once())
    assert [c for c in speaker.calls if c[0] == "route"] == [("route", speaker.sink)]


def test_sync_reroutes_when_sink_comes_back():
    speaker = _FakeSpeaker(sink="bluez_output.00_11_22_33_44_55.1")
    adapter = _adapter(speaker)
    _run(adapter.sync_once())
    speaker.sink = None                      # speaker switched off
    speaker.connect_ok = False
    _run(adapter.sync_once())
    speaker.sink = "bluez_output.00_11_22_33_44_55.1"   # BlueZ auto-reconnected
    _run(adapter.sync_once())
    assert [c for c in speaker.calls if c[0] == "route"] == [
        ("route", speaker.sink), ("route", speaker.sink)]


def test_reconnect_attempts_are_throttled(monkeypatch):
    speaker = _FakeSpeaker(connect_ok=False)
    adapter = _adapter(speaker)
    clock = [1000.0]
    monkeypatch.setattr(bt_volume.time, "monotonic", lambda: clock[0])
    _run(adapter.sync_once())
    _run(adapter.sync_once())
    assert speaker.calls.count("connect") == 1
    clock[0] += bt_volume.RECONNECT_INTERVAL_S
    _run(adapter.sync_once())
    assert speaker.calls.count("connect") == 2


def test_standby_disconnects_and_stays_disconnected():
    speaker = _FakeSpeaker(sink="bluez_output.00_11_22_33_44_55.1")
    adapter = _adapter(speaker)
    _run(adapter.power_off())
    assert "disconnect" in speaker.calls
    assert adapter.is_on_cached() is False
    assert _run(adapter.sync_once()) is None
    assert "connect" not in speaker.calls
    _run(adapter.power_on())                 # wake: connect right away
    assert "connect" in speaker.calls
    assert adapter.is_on_cached() is True


def test_apply_volume_when_connected():
    speaker = _FakeSpeaker(sink="bluez_output.00_11_22_33_44_55.1")
    adapter = _adapter(speaker)
    _run(adapter._apply_volume(50))
    assert ("volume", 50) in speaker.calls
    assert _run(adapter.get_volume()) == 40


def test_no_mac_does_nothing():
    adapter = BluetoothVolume("", 70)
    assert _run(adapter.sync_once()) is None
    assert adapter._watch is None
