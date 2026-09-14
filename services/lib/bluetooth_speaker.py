"""
Bluetooth A2DP speaker output via BlueZ (bluetoothctl) and PipeWire (pactl).

Everything the BS5c plays lands in the ``beo_tone_sink`` filter-chain (see
install/configs/53-beosound5c-tone.conf). For a Bluetooth speaker the chain's
own output stream is pointed at the speaker's ``bluez_output.*`` sink, so
bass/treble/balance still apply and mpv, go-librespot and shairport-sync all
follow without being told about the speaker.

The parsers are plain functions so they can be tested without BlueZ.

Usage:
    speaker = BluetoothSpeaker("AA:BB:CC:DD:EE:FF")
    await speaker.connect()
    sink = await speaker.find_sink()
    await speaker.route_to(sink)
    await speaker.set_volume(sink, 40)

    devices = await scan(8)          # config page: audio devices nearby
    ok, message = await pair(mac)    # pair + trust + connect
"""

import asyncio
import logging
import os
import re
import time

log = logging.getLogger(__name__)

TONE_OUTPUT_NODE = "beo_tone_sink.output"

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")
# A2DP sink service class — what a speaker advertises to accept audio.
_AUDIO_SINK_UUID = "0000110b"
# Class of Device major class "Audio/Video" (bits 8-12).
_MAJOR_CLASS_AUDIO = 0x04
# GAP Appearance categories (value >> 6) of LE-advertising speakers:
# 0x21 Audio Sink (speaker, soundbar, ...), 0x25 Wearable Audio Device.
_AUDIO_APPEARANCE_CATEGORIES = {0x21, 0x25}
# bluetoothctl scan chatter that says nothing about why a scan found nothing.
_SCAN_NOISE = ("RSSI", "ManufacturerData", "TxPower", "ServiceData",
               "AdvertisingFlags", "UUIDs", "Key:", "Value:")


def normalize_mac(mac) -> str:
    """Upper-case colon form, or "" when ``mac`` isn't a MAC address."""
    mac = str(mac or "").strip().upper().replace("-", ":")
    return mac if _MAC_RE.match(mac) else ""


def sink_prefix(mac: str) -> str:
    """PipeWire names A2DP sinks ``bluez_output.AA_BB_..._FF.<n>``."""
    return "bluez_output." + normalize_mac(mac).replace(":", "_")


def _clean(output: str) -> str:
    return _ANSI_RE.sub("", output or "")


def parse_devices(output: str) -> list[tuple[str, str]]:
    """``bluetoothctl devices`` → [(mac, name)]."""
    devices = []
    for line in _clean(output).splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) < 2 or parts[0] != "Device":
            continue
        mac = normalize_mac(parts[1])
        if mac:
            devices.append((mac, parts[2].strip() if len(parts) == 3 else mac))
    return devices


def parse_info(output: str) -> dict | None:
    """``bluetoothctl info <mac>`` → dict, or None if BlueZ doesn't know it."""
    text = _clean(output)
    if "Device " not in text or "not available" in text:
        return None
    info = {"name": "", "paired": False, "trusted": False, "connected": False,
            "icon": "", "uuids": [], "class": None, "appearance": None}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        value = value.strip()
        if key in ("Name", "Alias") and not info["name"]:
            info["name"] = value
        elif key in ("Paired", "Trusted", "Connected"):
            info[key.lower()] = value.lower().startswith("yes")
        elif key == "Icon":
            info["icon"] = value
        elif key == "UUID":
            m = re.search(r"\(([0-9a-fA-F-]+)\)", value)
            if m:
                info["uuids"].append(m.group(1).lower())
        elif key in ("Class", "Appearance"):
            try:
                info[key.lower()] = int(value.split()[0], 16)
            except (ValueError, IndexError):
                pass
    return info


def parse_controller(output: str) -> dict | None:
    """``bluetoothctl show`` → {address, powered, discovering}, or None when
    there is no controller."""
    text = _clean(output)
    m = re.search(r"Controller ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", text)
    if not m or "No default controller" in text:
        return None
    flags = {}
    for key in ("Powered", "Discovering"):
        fm = re.search(rf"^\s*{key}:\s*(\w+)", text, re.MULTILINE)
        flags[key.lower()] = bool(fm and fm.group(1).lower() == "yes")
    return {"address": m.group(1).upper(), **flags}


def scan_messages(output: str, limit: int = 30) -> list[str]:
    """The lines of a ``bluetoothctl scan on`` run worth logging: errors,
    discovery state and newly found devices, without per-packet chatter."""
    lines = []
    for line in _clean(output).splitlines():
        line = line.strip()
        if not line or any(noise in line for noise in _SCAN_NOISE):
            continue
        lines.append(line)
    return lines[:limit]


def describe(info: dict | None) -> str:
    """Short summary of what a device reports, for the scan log."""
    if not info:
        return "no info"
    parts = []
    if info.get("icon"):
        parts.append(f"icon={info['icon']}")
    if info.get("class") is not None:
        parts.append(f"class=0x{info['class']:06x}")
    if info.get("appearance") is not None:
        parts.append(f"appearance=0x{info['appearance']:04x}")
    if any(u.startswith(_AUDIO_SINK_UUID) for u in info.get("uuids", [])):
        parts.append("a2dp-sink")
    return " ".join(parts) or "no type hints"


def is_audio_sink(info: dict | None) -> bool:
    """True for devices that can play audio we send them (speakers,
    headphones, soundbars) — by icon, A2DP sink UUID or device class."""
    if not info:
        return False
    if info.get("icon", "").startswith("audio"):
        return True
    if any(u.startswith(_AUDIO_SINK_UUID) for u in info.get("uuids", [])):
        return True
    cls = info.get("class")
    if cls is not None and (cls >> 8) & 0x1F == _MAJOR_CLASS_AUDIO:
        return True
    appearance = info.get("appearance")
    return appearance is not None and appearance >> 6 in _AUDIO_APPEARANCE_CATEGORIES


def find_bluez_sink(short_sinks: str, mac: str) -> str | None:
    """Name of the speaker's sink in ``pactl list sinks short``, if present."""
    prefix = sink_prefix(mac)
    for line in short_sinks.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].startswith(prefix):
            return parts[1]
    return None


def find_sink_input(sink_inputs: str, node_name: str) -> str | None:
    """Index of the sink-input whose ``node.name`` matches, from
    ``pactl list sink-inputs``."""
    current = None
    for line in sink_inputs.splitlines():
        line = line.strip()
        m = re.match(r"Sink Input #(\d+)", line)
        if m:
            current = m.group(1)
        elif current and line == f'node.name = "{node_name}"':
            return current
    return None


def parse_volume_percent(output: str) -> int | None:
    """First channel's percentage from ``pactl get-sink-volume``."""
    m = re.search(r"(\d+)%", output or "")
    return int(m.group(1)) if m else None


async def _run(*args, timeout: float = 5.0) -> tuple[str, int]:
    """Run a command without blocking the loop → (stdout+stderr, returncode).

    bluetoothctl reports failures on stdout, so both streams are merged.
    """
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)
    except FileNotFoundError:
        log.error("%s not found", args[0])
        return "", 127
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("%s timed out after %.0fs", " ".join(args[:3]), timeout)
        return "", -1
    return out.decode("utf-8", "replace"), proc.returncode or 0


class BluetoothSpeaker:
    """One paired A2DP speaker, addressed by MAC."""

    def __init__(self, mac: str):
        self.mac = normalize_mac(mac)

    async def info(self) -> dict | None:
        out, _ = await _run("bluetoothctl", "info", self.mac)
        return parse_info(out)

    async def connect(self) -> bool:
        out, _ = await _run("bluetoothctl", "connect", self.mac, timeout=20)
        if "Connection successful" in out:
            log.info("Bluetooth speaker %s connected", self.mac)
            return True
        info = await self.info()
        return bool(info and info["connected"])

    async def disconnect(self) -> None:
        await _run("bluetoothctl", "disconnect", self.mac, timeout=10)
        log.info("Bluetooth speaker %s disconnected", self.mac)

    async def find_sink(self) -> str | None:
        out, _ = await _run("pactl", "list", "sinks", "short")
        return find_bluez_sink(out, self.mac)

    async def route_to(self, sink: str) -> bool:
        """Send the BS5c's audio to ``sink``.

        Moves the tone chain's output stream there. Without the tone chain
        (not installed) the speaker becomes the default sink instead and
        every playing stream moves over.
        """
        inputs, _ = await _run("pactl", "list", "sink-inputs")
        tone = find_sink_input(inputs, TONE_OUTPUT_NODE)
        if tone is not None:
            _, rc = await _run("pactl", "move-sink-input", tone, sink)
            log.info("Tone chain output -> %s (rc=%d)", sink, rc)
            return rc == 0
        log.warning("No %s stream — using %s as default sink", TONE_OUTPUT_NODE, sink)
        _, rc = await _run("pactl", "set-default-sink", sink)
        short, _ = await _run("pactl", "list", "sink-inputs", "short")
        for line in short.splitlines():
            if line.strip():
                await _run("pactl", "move-sink-input", line.split("\t")[0], sink)
        return rc == 0

    async def get_volume(self, sink: str) -> int | None:
        out, rc = await _run("pactl", "get-sink-volume", sink)
        return parse_volume_percent(out) if rc == 0 else None

    async def set_volume(self, sink: str, percent: float) -> bool:
        # A2DP absolute volume (AVRCP) passes this to the speaker's own
        # amplifier when it supports it; otherwise PipeWire scales digitally.
        _, rc = await _run("pactl", "set-sink-volume", sink, f"{percent:.0f}%")
        return rc == 0


async def _devices() -> list[tuple[str, str]]:
    out, _ = await _run("bluetoothctl", "devices")
    return parse_devices(out)


async def scan(seconds: float = 8, exclude: set[str] | None = None) -> list[dict]:
    """Discover for ``seconds`` (0 = don't), then list every audio sink BlueZ
    knows — freshly found and already paired — sorted paired-first."""
    seconds = int(min(max(float(seconds), 0), 20))
    show, _ = await _run("bluetoothctl", "show")
    controller = parse_controller(show)
    if controller is None:
        log.error("Bluetooth scan: no controller (%s)",
                  " / ".join(scan_messages(show, 3)) or "bluetoothctl show was empty")
        return []
    log.info("Bluetooth scan: controller %s, powered=%s, %ds",
             controller["address"], controller["powered"], seconds)
    if not controller["powered"]:
        out, _ = await _run("bluetoothctl", "power", "on")
        log.info("Bluetooth scan: power on → %s", " / ".join(scan_messages(out, 3)))
    if seconds:
        out, rc = await _run("bluetoothctl", "--timeout", str(seconds), "scan", "on",
                             timeout=seconds + 5)
        for line in scan_messages(out):
            log.info("Bluetooth scan: %s", line)
        log.info("Bluetooth scan: bluetoothctl exited rc=%d", rc)
    exclude = {normalize_mac(m) for m in (exclude or set())}
    found = []
    for mac, name in await _devices():
        if mac in exclude:
            continue
        info = await BluetoothSpeaker(mac).info()
        audio = is_audio_sink(info)
        log.info("Bluetooth scan: %s %r %s → %s", mac, name, describe(info),
                 "speaker" if audio else "skipped")
        if not audio:
            continue
        found.append({"mac": mac, "name": info["name"] or name,
                      "paired": info["paired"], "connected": info["connected"]})
    found.sort(key=lambda d: (not d["paired"], d["name"].lower()))
    return found


async def pair(mac: str) -> tuple[bool, str]:
    """Pair, trust and connect a speaker in pairing mode."""
    speaker = BluetoothSpeaker(mac)
    if not speaker.mac:
        return False, "Invalid MAC address"

    info = await speaker.info()
    if info is None:
        # BlueZ forgets unpaired devices ~30s after a scan — look again.
        scanner = await asyncio.create_subprocess_exec(
            "bluetoothctl", "--timeout", "15", "scan", "on",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        try:
            while info is None and time.monotonic() < deadline:
                await asyncio.sleep(1)
                info = await speaker.info()
        finally:
            if scanner.returncode is None:
                scanner.kill()
            await scanner.wait()
        if info is None:
            return False, "Speaker not found — is it in pairing mode?"

    if not info["paired"]:
        out, _ = await _run("bluetoothctl", "--agent", "NoInputNoOutput",
                            "pair", speaker.mac, timeout=40)
        if "Pairing successful" not in out and "AlreadyExists" not in out:
            info = await speaker.info()
            if not (info and info["paired"]):
                reason = re.search(r"Failed to pair: (\S+)", _clean(out))
                return False, f"Pairing failed{': ' + reason.group(1) if reason else ''}"
    await _run("bluetoothctl", "trust", speaker.mac)
    if not await speaker.connect():
        return False, "Paired, but could not connect — try again"
    log.info("Bluetooth speaker %s paired and connected", speaker.mac)
    return True, "Paired and connected"
