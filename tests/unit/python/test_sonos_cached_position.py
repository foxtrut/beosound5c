"""The cached Sonos payload must carry a current position.

fetch_media_data() only refreshes _cached_media_data when it broadcasts (a
track change, or a seek detected as external_control). The pause/stop branch
of the monitor loop, however, reuses that cache verbatim — so before
_remember_position() the state_change push carried the position the track
*started* at, and the PLAYING view's progress bar jumped backwards on every
pause. bluesound/wiim/heos/ase refresh their cached position on each poll;
this is the Sonos equivalent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from test_sonos_external_start import _install_fake_soco

TRACK_URI = "x-sonos-spotify:spotify%3atrack%3aabc123?sid=9"
OTHER_URI = "x-sonos-spotify:spotify%3atrack%3adef456?sid=9"


@pytest.fixture
def player():
    _install_fake_soco()
    from players.sonos import MediaServer
    p = MediaServer()
    p.sonos_viewer = MagicMock()
    p.broadcast_media_update = AsyncMock()
    return p


def _cached(uri=TRACK_URI, position="0:02"):
    return {"title": "Joyride", "uri": uri,
            "position": position, "duration": "3:34"}


class TestRememberPosition:
    def test_refreshes_the_current_track(self, player):
        player._cached_media_data = _cached()
        player._remember_position(TRACK_URI, "1:12")
        assert player._cached_media_data["position"] == "1:12"

    def test_leaves_another_track_alone(self, player):
        """Radio keeps the previous track's payload — see the state_change
        branch in the monitor loop. Stamping this position onto it would
        describe a track that isn't playing."""
        player._cached_media_data = _cached(uri=OTHER_URI)
        player._remember_position(TRACK_URI, "1:12")
        assert player._cached_media_data["position"] == "0:02"

    def test_ignores_an_empty_position(self, player):
        player._cached_media_data = _cached(position="1:12")
        player._remember_position(TRACK_URI, "")
        assert player._cached_media_data["position"] == "1:12"

    def test_ignores_a_missing_track_id(self, player):
        player._cached_media_data = _cached()
        player._remember_position("", "1:12")
        assert player._cached_media_data["position"] == "0:02"

    def test_no_cache_is_not_an_error(self, player):
        player._cached_media_data = None
        player._remember_position(TRACK_URI, "1:12")
        assert player._cached_media_data is None
