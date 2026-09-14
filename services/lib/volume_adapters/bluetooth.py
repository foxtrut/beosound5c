"""
Bluetooth speaker volume adapter — A2DP speaker as the main audio output.

Keeps the speaker in ``volume.bt_mac`` connected while the BS5c is awake,
points the tone chain's output at it (see lib/bluetooth_speaker.py) and sets
its volume through the PipeWire sink, which reaches the speaker's own
amplifier over AVRCP absolute volume when it supports that.

A watch loop re-connects when the speaker was switched off or walked out of
range and re-routes when its sink comes back. Standby disconnects, so the
speaker can go to sleep and other devices can use it.
"""

import asyncio
import logging
import time

from ..background_tasks import BackgroundTaskSet
from ..bluetooth_speaker import BluetoothSpeaker
from .base import VolumeAdapter

logger = logging.getLogger("beo-router.volume.bluetooth")

WATCH_INTERVAL_S = 5
RECONNECT_INTERVAL_S = 30


class BluetoothVolume(VolumeAdapter):
    """Volume and connection management for one Bluetooth speaker."""

    def __init__(self, mac: str, max_volume: int, speaker=None):
        super().__init__(max_volume, debounce_ms=100)
        self._speaker = speaker or BluetoothSpeaker(mac)
        self._wanted = True          # False while in standby
        self._routed_sink: str | None = None
        self._next_connect_at = 0.0
        self._last_volume: float | None = None
        self._tasks = BackgroundTaskSet(logger, label="bt-speaker")
        self._watch: asyncio.Task | None = None
        # One connect/route attempt at a time: power_on's immediate attempt
        # and the watch loop's tick would otherwise both call connect.
        self._sync_lock = asyncio.Lock()

    def _ensure_watch(self) -> None:
        if not self._speaker.mac:
            return
        if self._watch is None or self._watch.done():
            self._watch = self._tasks.spawn(self._watch_loop(), name="bt_speaker_watch")

    async def _watch_loop(self) -> None:
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Bluetooth speaker check failed: %s", e)
            await asyncio.sleep(WATCH_INTERVAL_S)

    async def sync_once(self) -> str | None:
        """One watch tick: connect if wanted and missing, route a sink that
        (re)appeared. Returns the routed sink, if any."""
        async with self._sync_lock:
            return await self._sync()

    async def _sync(self) -> str | None:
        if not self._wanted or not self._speaker.mac:
            return None
        sink = await self._speaker.find_sink()
        if sink is None:
            self._routed_sink = None
            now = time.monotonic()
            if now < self._next_connect_at:
                return None
            self._next_connect_at = now + RECONNECT_INTERVAL_S
            if not await self._speaker.connect():
                return None
            # The sink shows up a moment after BlueZ reports the connection.
            for _ in range(10):
                sink = await self._speaker.find_sink()
                if sink:
                    break
                await asyncio.sleep(0.5)
            if sink is None:
                logger.warning("Connected to %s but no PipeWire sink appeared — "
                               "is the WirePlumber Bluetooth monitor enabled?",
                               self._speaker.mac)
                return None
        if sink != self._routed_sink:
            if await self._speaker.route_to(sink):
                self._routed_sink = sink
                if self._last_volume is not None:
                    await self._speaker.set_volume(sink, self._last_volume)
        return self._routed_sink

    # -- VolumeAdapter --

    async def _apply_volume(self, volume: float) -> None:
        self._last_volume = volume
        self._ensure_watch()
        sink = self._routed_sink or await self._speaker.find_sink()
        if sink is None:
            logger.info("Bluetooth speaker not connected — %.0f%% applies on connect", volume)
            return
        await self._speaker.set_volume(sink, volume)
        logger.info("-> Bluetooth speaker volume: %.0f%%", volume)

    async def get_volume(self) -> float | None:
        self._ensure_watch()
        sink = await self._speaker.find_sink()
        if sink is None:
            return None
        return await self._speaker.get_volume(sink)

    async def power_on(self) -> None:
        self._wanted = True
        self._next_connect_at = 0.0
        self._ensure_watch()
        # Connecting takes seconds (up to 20s when the speaker is off) and the
        # router awaits power_on inside request handlers — start it now, but
        # in the background.
        self._tasks.spawn(self.sync_once(), name="bt_speaker_power_on")

    async def power_off(self) -> None:
        self._wanted = False
        self._routed_sink = None
        if self._speaker.mac:
            await self._speaker.disconnect()

    async def is_on(self) -> bool:
        return self._wanted and await self._speaker.find_sink() is not None

    def is_on_cached(self) -> bool | None:
        return self._wanted

    async def close(self) -> None:
        await super().close()
        await self._tasks.cancel_all()
