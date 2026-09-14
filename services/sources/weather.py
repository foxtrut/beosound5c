#!/usr/bin/env python3
"""
BeoSound 5c — Weather source (DMI Open Data).

Fetches an hourly forecast for a configured lat/lon from DMI's public
Forecast EDR API (the HARMONIE DINI model) and serves a "today" summary
plus an hourly breakdown to the frontend. No API key required.

When DMI's API refuses or fails — it answers 429 "Server is busy" for hours
at a time — the same DMI model is fetched through Open-Meteo instead, and the
WEATHER page credits whichever one served the forecast.

Config (config.json):
    "weather": { "latitude": "56.172", "longitude": "10.199",
                 "location_name": "Christiansbjerg, Aarhus" }
location_name is optional and purely a display label — the coordinates
are what's actually sent to DMI. Falls back to showing the coordinates
if left out.

Port: 8790
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web

sys.path.insert(0, "..")
sys.path.insert(0, ".")

from lib.config import cfg
from lib.source_base import SourceBase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEATHER] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DMI_EDR_BASE = "https://opendataapi.dmi.dk/v1/forecastedr"
COLLECTION = "harmonie_dini_sf"
PARAMETERS = ["temperature-2m", "total-precipitation",
              "fraction-of-cloud-cover", "wind-speed-10m"]
# Fallback: Open-Meteo's feed of the same DMI HARMONIE model. Free for
# non-commercial use (10,000 calls/day), no key; its data is CC BY 4.0, which
# is why the page names the provider.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODEL = "dmi_seamless"
OPEN_METEO_HOURLY = ["temperature_2m", "precipitation", "cloud_cover", "wind_speed_10m"]
PROVIDER_NAMES = {"dmi": "DMI", "open_meteo": "Open-Meteo"}
REFRESH_INTERVAL = 30 * 60  # 30 minutes — forecast doesn't change fast enough for more
# After a failed fetch, try again sooner than REFRESH_INTERVAL: DMI answers
# 429 "Server is busy" for stretches (seen around midnight), and one miss used
# to leave the WEATHER page empty for half an hour. Resets on the next success.
RETRY_DELAYS = (60, 2 * 60, 5 * 60, 10 * 60, 20 * 60, REFRESH_INTERVAL)
RAIN_THRESHOLD_MM = 0.1  # hourly amount considered "it's raining"
# Fixed regardless of provider or time of day — capping the hourly list to
# "whatever's left of today" instead meant it shrank to almost nothing in
# the evening, and DMI vs. Open-Meteo could return different step counts
# for the same window, so the two providers visibly disagreed on how many
# hours to show. The frontend only ever renders hours.slice(0, 10) anyway.
HOURLY_COUNT = 10
LOCAL_TZ = ZoneInfo("Europe/Copenhagen")


def kelvin_to_c(kelvin):
    return round(kelvin - 273.15, 1)


def next_delay(failures):
    """Seconds to wait before the next fetch, after `failures` failed fetches in a row."""
    if failures <= 0:
        return REFRESH_INTERVAL
    return RETRY_DELAYS[min(failures, len(RETRY_DELAYS)) - 1]


class WeatherService(SourceBase):
    id = "weather"
    name = "Weather"
    port = 8790
    player = "local"
    action_map = {
        "go": "select",
        "up": "up",
        "down": "down",
        "left": "back",
        "right": "select",
    }

    def __init__(self):
        super().__init__()
        self._forecast = {}
        self._last_fetch = 0
        self._lat = ""
        self._lon = ""
        self._location_name = ""

    async def on_start(self):
        self._lat = cfg("weather", "latitude", default="")
        self._lon = cfg("weather", "longitude", default="")
        self._location_name = cfg("weather", "location_name", default="")
        if not self._lat or not self._lon:
            log.info("No weather.latitude/longitude in config — weather source disabled")
            raise SystemExit(0)

        log.info("Weather location configured (%s, %s), starting forecast fetch loop",
                  self._lat, self._lon)
        await self.register("available")
        self._spawn(self._refresh_loop(), name="refresh_loop")

    async def on_stop(self):
        await self.register("gone")

    async def _refresh_loop(self):
        failures = 0
        while True:
            try:
                ok = await self._fetch_forecast()
            except asyncio.CancelledError:
                return
            except Exception as e:
                # A timeout's message is empty — name the type so the log says something.
                log.error("Fetch failed: %s", str(e) or type(e).__name__)
                ok = False
            failures = 0 if ok else failures + 1
            delay = next_delay(failures)
            if failures:
                log.info("Retrying forecast fetch in %d min", delay // 60)
            await asyncio.sleep(delay)

    async def _fetch_forecast(self):
        """Fetch and summarise the forecast. Returns True if it was updated.

        DMI's own API first; if it refuses or fails, the same model through
        Open-Meteo, so a DMI outage does not leave the page empty.
        """
        for provider, fetch_steps in (("dmi", self._fetch_dmi_steps),
                                      ("open_meteo", self._fetch_open_meteo_steps)):
            try:
                steps = await fetch_steps()
            except Exception as e:
                # A timeout's message is empty — name the type so the log says something.
                log.error("%s fetch failed: %s", PROVIDER_NAMES[provider],
                          str(e) or type(e).__name__)
                continue
            if steps:
                self._forecast = self._build_summary(steps, provider)
                self._last_fetch = time.time()
                log.info("Forecast updated from %s: %d hourly steps",
                         PROVIDER_NAMES[provider], len(steps))
                return True
        return False

    async def _fetch_dmi_steps(self):
        log.info("Fetching forecast from DMI Open Data...")
        params = {
            "coords": f"POINT({self._lon} {self._lat})",
            "parameter-name": ",".join(PARAMETERS),
            "crs": "crs84",
            "f": "GeoJSON",
        }
        url = f"{DMI_EDR_BASE}/collections/{COLLECTION}/position"
        async with self._http_session.get(url, params=params, timeout=20) as resp:
            if resp.status != 200:
                log.error("DMI API returned %d", resp.status)
                return []
            data = await resp.json()

        steps = self._parse_steps(data)
        if not steps:
            log.warning("DMI response had no usable forecast steps")
        return steps

    async def _fetch_open_meteo_steps(self):
        log.info("Fetching forecast from Open-Meteo (DMI model) instead...")
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "hourly": ",".join(OPEN_METEO_HOURLY),
            "models": OPEN_METEO_MODEL,
            "wind_speed_unit": "ms",
            "timeformat": "unixtime",
            "timezone": "GMT",
            "forecast_days": "3",
        }
        async with self._http_session.get(OPEN_METEO_URL, params=params, timeout=20) as resp:
            if resp.status != 200:
                log.error("Open-Meteo API returned %d", resp.status)
                return []
            data = await resp.json()

        steps = self._parse_open_meteo_steps(data)
        if not steps:
            log.warning("Open-Meteo response had no usable forecast steps")
        return steps

    def _parse_steps(self, data):
        steps = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            step = props.get("step")
            if not step:
                continue
            try:
                ts = datetime.fromisoformat(step.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            except ValueError:
                continue
            cloud = props.get("fraction-of-cloud-cover")
            steps.append({
                "time": ts,
                "temp_c": kelvin_to_c(props["temperature-2m"])
                          if "temperature-2m" in props else None,
                "precip_cum_mm": props.get("total-precipitation"),
                "cloud_pct": round(cloud * 100) if cloud is not None else None,
                "wind_ms": props.get("wind-speed-10m"),
            })
        steps.sort(key=lambda s: s["time"])
        return steps

    def _parse_open_meteo_steps(self, data):
        """Open-Meteo's hourly arrays, in the shape _parse_steps returns.

        Open-Meteo's precipitation is the amount in the hour up to each
        timestamp; DMI's is accumulated over the model run, which is what
        _build_summary diffs — so accumulate it here the same way.
        """
        hourly = data.get("hourly") or {}

        def at(name, i):
            values = hourly.get(name) or []
            return values[i] if i < len(values) else None

        steps = []
        cum = 0.0
        for i, ts in enumerate(hourly.get("time") or []):
            precip = at("precipitation", i)
            if precip is not None:
                cum = round(cum + precip, 2)
            steps.append({
                "time": datetime.fromtimestamp(ts, LOCAL_TZ),
                "temp_c": at("temperature_2m", i),
                "precip_cum_mm": cum if precip is not None else None,
                "cloud_pct": at("cloud_cover", i),
                "wind_ms": at("wind_speed_10m", i),
            })
        return steps

    def _build_summary(self, steps, provider="dmi"):
        now = datetime.now(LOCAL_TZ)
        today = now.date()
        now_hour = now.replace(minute=0, second=0, microsecond=0)
        # steps[0] is the oldest point in the model run, which can be hours
        # in the past relative to "now" — "current" must track the step
        # whose timestamp is nearest to now instead (whichever side of it).
        current_step = min(steps, key=lambda s: abs((s["time"] - now).total_seconds()))

        hourly = []
        prev_cum = None
        today_start_cum = None
        today_end_cum = None
        today_temps = []
        will_rain = False

        for s in steps:
            cum = s["precip_cum_mm"]
            delta = None
            if prev_cum is not None and cum is not None:
                delta = max(0.0, round(cum - prev_cum, 2))
            prev_cum = cum

            # "Today"'s summary (will_rain, min/max) stays scoped to the
            # calendar day — but the hourly list below is a fixed lookahead
            # from now, independent of the day boundary.
            if s["time"].date() == today:
                if cum is not None:
                    if today_start_cum is None:
                        today_start_cum = cum
                    today_end_cum = cum
                if s["temp_c"] is not None:
                    today_temps.append(s["temp_c"])
                if delta is not None and delta >= RAIN_THRESHOLD_MM:
                    will_rain = True

            if s["time"] >= now_hour and len(hourly) < HOURLY_COUNT:
                hourly.append({
                    "time": s["time"].strftime("%H:%M"),
                    "temp_c": s["temp_c"],
                    "rain_mm": delta,
                    "cloud_pct": s["cloud_pct"],
                })

        rain_today_mm = None
        if today_start_cum is not None and today_end_cum is not None:
            rain_today_mm = round(max(0.0, today_end_cum - today_start_cum), 1)

        current = current_step
        return {
            "updated": time.time(),
            "provider": provider,
            "location": {
                "name": self._location_name or None,
                "lat": self._lat,
                "lon": self._lon,
            },
            "current": {
                "temp_c": current["temp_c"],
                "cloud_pct": current["cloud_pct"],
                "wind_ms": current["wind_ms"],
            },
            "today": {
                "date": today.isoformat(),
                "will_rain": will_rain,
                "rain_mm": rain_today_mm,
                "temp_min_c": round(min(today_temps), 1) if today_temps else None,
                "temp_max_c": round(max(today_temps), 1) if today_temps else None,
            },
            "hourly": hourly,
        }

    def add_routes(self, app):
        app.router.add_get("/forecast", self._handle_forecast)

    async def _handle_forecast(self, request):
        return web.json_response(self._forecast, headers=self._cors_headers())

    async def handle_status(self):
        return {
            "source": self.id,
            "name": self.name,
            "last_fetch": self._last_fetch,
            "has_data": bool(self._forecast),
            "provider": self._forecast.get("provider"),
        }

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        if cmd == "refresh":
            await self._fetch_forecast()
            return {"refreshed": True}
        return {}


if __name__ == "__main__":
    service = WeatherService()
    asyncio.run(service.run())
