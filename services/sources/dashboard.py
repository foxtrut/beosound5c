#!/usr/bin/env python3
"""
BeoSound 5c — Dashboard (DASHBOARD on the arc, OVERBLIK in Danish).

One page of at-a-glance information instead of something to browse: what is
playing, today's and tomorrow's electricity price hour by hour, and how the
device itself is doing (uptime, CPU temperature, load, memory, disk, network
address). The clock and date on the page come from the browser, so this
service only serves what the browser cannot see itself.

The device data is gathered on request from /proc and the router's
/router/status. Electricity prices come from Strømligning's public API
(stromligning.dk, no key), all-in prices per kWh — spot price, the net
company's tariffs, taxes and VAT — for one net company. They are fetched when
the page asks and at most every 15 minutes; nothing is polled in the
background, so an idle dashboard costs nothing. Tomorrow's prices are
Strømligning's forecast until the real ones are published (around 13:00), the
same "prognose" stromligning.dk shows. Every field is optional: on a machine
without /proc (a Mac running the services for development), with the router
down or without a net company configured, the page leaves that part out.

Config (config.json):
    "dashboard": { "electricity_supplier": "dinel_c", "electricity_area": "DK1" }
The supplier is the net company's id on stromligning.dk (the "netselskab="
in its URLs); the area is DK1 (west of the Great Belt) or DK2 (east).

Port: 8795
"""

import asyncio
import logging
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from lib.config import cfg  # noqa: E402
from lib.endpoints import ROUTER_STATUS  # noqa: E402
from lib.source_base import SourceBase  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DASHBOARD] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

PROC = "/proc"
THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"

PRICES_URL = "https://stromligning.dk/api/prices"
PRICES_TTL = 15 * 60          # seconds a fetched price list is served from cache
PRICES_RETRY = 2 * 60         # after a failed fetch, wait this long before the next try


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def read_uptime(proc=PROC):
    """Seconds since boot, or None."""
    text = _read(os.path.join(proc, "uptime"))
    try:
        return int(float(text.split()[0]))
    except (AttributeError, IndexError, ValueError):
        return None


def read_load(proc=PROC):
    """1/5/15-minute load averages, or None."""
    text = _read(os.path.join(proc, "loadavg"))
    try:
        return [float(v) for v in text.split()[:3]]
    except (AttributeError, ValueError):
        return None


def read_memory(proc=PROC):
    """{"total_mb", "used_pct"} from /proc/meminfo, or None.

    "Used" is total minus MemAvailable — page cache the kernel would give back
    is not counted, which is what `free` calls available too."""
    text = _read(os.path.join(proc, "meminfo"))
    if not text:
        return None
    fields = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        try:
            fields[name] = int(rest.split()[0])
        except (IndexError, ValueError):
            continue
    total = fields.get("MemTotal")
    avail = fields.get("MemAvailable")
    if not total or avail is None:
        return None
    return {"total_mb": total // 1024, "used_pct": round(100 * (total - avail) / total)}


def read_cpu_temp(path=THERMAL_ZONE):
    """SoC temperature in °C (one decimal), or None."""
    text = _read(path)
    try:
        return round(int(text.strip()) / 1000, 1)
    except (AttributeError, ValueError):
        return None


def read_disk(path="/"):
    """{"total_gb", "free_gb", "used_pct"} for the root filesystem, or None."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    gb = 1024 ** 3
    return {
        "total_gb": round(usage.total / gb, 1),
        "free_gb": round(usage.free / gb, 1),
        "used_pct": round(100 * usage.used / usage.total) if usage.total else 0,
    }


def local_ip():
    """The address other machines on the LAN reach this device on, or None.

    Connecting a UDP socket sends nothing; it only makes the kernel pick the
    outgoing interface, whose address is the one we want."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere
            return s.getsockname()[0]
    except OSError:
        return None


def summarise_playing(status):
    """The bits of /router/status the dashboard shows, or None when idle."""
    if not isinstance(status, dict):
        return None
    media = status.get("media") or {}
    state = media.get("state") or "idle"
    if state not in ("playing", "paused") or not media.get("title"):
        return None
    return {
        "state": state,
        "title": media.get("title", ""),
        "artist": media.get("artist", ""),
        "album": media.get("album", ""),
        "source": status.get("active_source_name") or "",
    }


def hourly_prices(prices, day):
    """Hour-by-hour all-in prices (kr/kWh) for one local date ("YYYY-MM-DD").

    Strømligning answers in 15-minute steps for published prices and hourly
    steps for its forecast; an hour's price is the mean of its steps, which is
    what stromligning.dk's hourly table shows. Returns a list of
    {"hour", "price", "forecast"}, sorted by hour; empty when the day is not
    in the list."""
    steps = {}
    forecast = {}
    for p in prices or []:
        local = str(p.get("localDate", ""))
        try:
            total = float(p["price"]["total"])
            hour = int(local[11:13])
        except (KeyError, TypeError, ValueError):
            continue
        if local[:10] != day:
            continue
        steps.setdefault(hour, []).append(total)
        forecast[hour] = forecast.get(hour, False) or bool(p.get("forecast"))
    return [
        {"hour": h, "price": round(sum(v) / len(v), 4), "forecast": forecast[h]}
        for h, v in sorted(steps.items())
    ]


def electricity_days(prices, now=None):
    """Today and tomorrow, by the device's local clock, as
    [{"date", "hours": [...]}, ...] — days without prices are left out."""
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    days = []
    for day in (today.isoformat(), (today + timedelta(days=1)).isoformat()):
        hours = hourly_prices(prices, day)
        if hours:
            days.append({"date": day, "hours": hours})
    return days


class DashboardService(SourceBase):
    id = "dashboard"
    name = "Dashboard"
    port = 8795
    player = "local"
    action_map = {}

    def __init__(self):
        super().__init__()
        self._prices = None        # last good price list from Strømligning
        self._prices_at = 0.0      # monotonic time of that fetch
        self._prices_tried = 0.0   # monotonic time of the last attempt
        self._supplier_name = ""
        self._prices_lock = asyncio.Lock()

    def _electricity_config(self):
        supplier = str(cfg("dashboard", "electricity_supplier", default="") or "").strip()
        area = str(cfg("dashboard", "electricity_area", default="DK1") or "DK1").strip().upper()
        return supplier, area

    async def on_start(self):
        supplier, area = self._electricity_config()
        log.info("Dashboard on port %d; electricity prices: %s", self.port,
                 f"{supplier} ({area})" if supplier else "off (no dashboard.electricity_supplier)")
        await self.register("available")

    async def on_stop(self):
        await self.register("gone")

    def add_routes(self, app):
        app.router.add_get("/api/dashboard", self._handle_dashboard)

    async def _router_status(self):
        try:
            async with self._http_session.get(
                ROUTER_STATUS, timeout=aiohttp.ClientTimeout(total=1.5),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            log.debug("Router status unavailable: %s", e)
        return None

    async def _fetch_prices(self, supplier, area):
        params = {"priceArea": area, "supplierId": supplier, "forecast": "true"}
        async with self._http_session.get(
            PRICES_URL, params=params, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        prices = data.get("prices")
        if not isinstance(prices, list):
            raise ValueError("no price list in the answer")
        name = (data.get("supplier") or {}).get("name") or supplier
        return prices, name

    async def _electricity(self):
        """Today's and tomorrow's prices, from cache when it is fresh enough.
        A failed fetch keeps serving the last good list."""
        supplier, area = self._electricity_config()
        if not supplier:
            return None
        async with self._prices_lock:
            now = time.monotonic()
            stale = self._prices is None or now - self._prices_at > PRICES_TTL
            if stale and now - self._prices_tried > PRICES_RETRY:
                self._prices_tried = now
                try:
                    self._prices, self._supplier_name = await self._fetch_prices(supplier, area)
                    self._prices_at = now
                except Exception as e:
                    log.warning("Electricity prices unavailable: %s", e)
        if self._prices is None:
            return None
        return {"area": area, "supplier": self._supplier_name or supplier,
                "days": electricity_days(self._prices)}

    async def snapshot(self):
        status, electricity = await asyncio.gather(self._router_status(), self._electricity())
        return {
            "device": cfg("device", default="BeoSound 5c"),
            "hostname": socket.gethostname(),
            "ip": local_ip(),
            "time": int(time.time()),
            "uptime_s": read_uptime(),
            "load": read_load(),
            "memory": read_memory(),
            "cpu_temp_c": read_cpu_temp(),
            "disk": read_disk(),
            "volume": round(status["volume"]) if status and status.get("volume") is not None else None,
            "output": (status or {}).get("output_device"),
            "playing": summarise_playing(status),
            "electricity": electricity,
        }

    async def _handle_dashboard(self, request):
        return web.json_response(await self.snapshot(), headers=self._cors_headers())

    async def handle_status(self):
        return {"source": self.id, "name": self.name}

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        return {}


if __name__ == "__main__":
    service = DashboardService()
    asyncio.run(service.run())
