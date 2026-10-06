"""The local player reports mpv's position/duration to the UI.

Sources that play through mpv (Jellyfin, Plex, Tidal, Apple Music, USB,
news …) hand the player a URL and push their own metadata; post_media_update
defaults duration and position to 0, so the track length is known to mpv and
to nobody else. Without the progress loop below the PLAYING view's bar has
nothing to draw and stays hidden — which is exactly what happened on the
device the first time this shipped.

The UI extrapolates between events, so the loop only has to push when the
anchor actually moves: the length becoming known, a pause or resume, and a
slow resync for a UI that reloaded mid-track.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from players.local import LocalPlayer


class FakeProcess:
    """A process that stays alive for a fixed number of poll() calls."""

    def __init__(self, polls=99):
        self._left = polls

    def poll(self):
        if self._left <= 0:
            return 0
        self._left -= 1
        return None


@pytest.fixture
def player():
    p = LocalPlayer()
    p._active_backend = 'mpv'
    p.broadcast_progress = AsyncMock()
    p.PROGRESS_POLL = 0        # don't make the tests wait
    return p


def _reads(player, samples):
    """Feed _mpv_get from (duration, time-pos, pause[, seekable]) tuples.

    Seekability defaults to True — the ordinary case of a file or a Jellyfin
    stream; the live-stream tests pass it explicitly.
    """
    seq = list(samples)
    state = {}

    async def fake_get(prop):
        if prop == 'duration':
            if not seq:
                player._process = FakeProcess(polls=0)
                return None
            state['cur'] = seq.pop(0)
            return state['cur'][0]
        if prop == 'time-pos':
            return state['cur'][1]
        if prop == 'pause':
            return state['cur'][2]
        if prop == 'seekable':
            cur = state['cur']
            return cur[3] if len(cur) > 3 else True
        return None

    player._mpv_get = fake_get


def _run(player, samples):
    player._process = FakeProcess(polls=len(samples) + 1)
    _reads(player, samples)
    asyncio.new_event_loop().run_until_complete(player._progress_loop())
    return player.broadcast_progress.await_args_list


class TestProgressLoop:
    def test_pushes_once_the_length_is_known(self, player):
        calls = _run(player, [(322.4, 10.0, False)])
        assert len(calls) == 1
        assert calls[0].kwargs == {
            "position_ms": 10000, "duration_ms": 322400, "playing": True}

    def test_stays_quiet_while_nothing_changes(self, player):
        """Four polls of the same track produce one push, not four — the UI
        follows its own clock between events."""
        calls = _run(player, [(322.4, t, False) for t in (10.0, 11.0, 12.0, 13.0)])
        assert len(calls) == 1

    def test_pushes_on_pause_and_resume(self, player):
        calls = _run(player, [
            (322.4, 10.0, False),
            (322.4, 11.0, True),    # paused
            (322.4, 11.0, True),
            (322.4, 11.0, False),   # resumed
        ])
        assert [c.kwargs["playing"] for c in calls] == [True, False, True]

    def test_pushes_when_the_track_changes_length(self, player):
        calls = _run(player, [(322.4, 10.0, False), (214.0, 1.0, False)])
        assert [c.kwargs["duration_ms"] for c in calls] == [322400, 214000]

    def test_resyncs_on_the_slow_interval(self, player):
        """A UI that reloaded mid-track gets the anchor back without waiting
        for the next pause or track change."""
        player.PROGRESS_RESYNC = 0   # every poll counts as overdue
        calls = _run(player, [(322.4, t, False) for t in (10.0, 11.0, 12.0)])
        assert len(calls) == 3

    def test_ignores_a_stream_with_no_length(self, player):
        """Live radio: mpv reports no duration, so there is nothing to show
        and nothing to send."""
        calls = _run(player, [(None, 10.0, False), (0, 11.0, False), (-1, 12.0, False)])
        assert calls == []

    def test_ignores_a_position_mpv_has_not_parsed_yet(self, player):
        calls = _run(player, [(322.4, None, False)])
        assert calls == []

    def test_says_nothing_about_a_live_stream(self, player):
        """DR's HLS radio reports a duration — the sliding window, with the
        position tracking the live edge — so the length alone cannot tell a
        track from a stream. mpv answers seekable=False for the stream and
        True for a Jellyfin track, and that is what decides it."""
        calls = _run(player, [(129.7, 113.5, False, False),
                              (129.7, 114.5, False, False)])
        assert calls == []

    def test_waits_until_mpv_knows(self, player):
        """seekable is None until the stream is open — not a live stream,
        just not known yet."""
        calls = _run(player, [(159.0, 1.0, False, None),
                              (159.0, 2.0, False, True)])
        assert len(calls) == 1
        assert calls[0].kwargs["duration_ms"] == 159000

    def test_stops_when_the_backend_is_no_longer_mpv(self, player):
        player._active_backend = 'librespot'
        calls = _run(player, [(322.4, 10.0, False)])
        assert calls == []


def _closing_spawn(*tasks):
    """Stand in for _spawn: closes the coroutine it is handed (nothing awaits
    it here) and returns the next fake task."""
    pending = list(tasks)

    def spawn(coro, *, name=None):
        coro.close()
        return pending.pop(0)

    return spawn


class TestProgressLifecycle:
    def test_killing_mpv_stops_the_loop(self, player):
        async def scenario():
            player._spawn = _closing_spawn(MagicMock())
            player._start_progress()
            task = player._progress_task
            player._stop_progress()
            task.cancel.assert_called_once()
            assert player._progress_task is None

        asyncio.new_event_loop().run_until_complete(scenario())

    def test_starting_twice_leaves_one_loop(self, player):
        async def scenario():
            tasks = [MagicMock(), MagicMock()]
            player._spawn = _closing_spawn(*tasks)
            player._start_progress()
            player._start_progress()
            tasks[0].cancel.assert_called_once()
            assert player._progress_task is tasks[1]

        asyncio.new_event_loop().run_until_complete(scenario())
