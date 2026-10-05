"""Jellyfin 12.1 authenticates streams by header, not by query parameter.

The server the BeoSound runs against answers 401 to
``/Audio/<id>/universal?...&api_key=<token>`` and 200 to the same URL
with ``Authorization: MediaBrowser ..., Token="<token>"``. Every track
therefore died ~2s in: mpv could not open the stream, and the source
logged "failed stream, not advancing".

These pin the three halves of the fix — a tokenless URL, the header on
the way to the player, and an mpv command line that does not shred a
header value full of commas.
"""

import json
import sys
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
JELLYFIN_DIR = REPO_ROOT / "services" / "sources" / "jellyfin"
PLAYERS_DIR = REPO_ROOT / "services" / "players"

TOKEN = "tok-abc123"
DEVICE_ID = "dev-999"
USER_ID = "user-1"


@pytest.fixture
def api():
    for p in (str(JELLYFIN_DIR), str(REPO_ROOT / "services")):
        if p not in sys.path:
            sys.path.insert(0, p)
    sys.modules.pop("jellyfin_api", None)
    import jellyfin_api
    return jellyfin_api


@pytest.fixture
def client(api):
    return api.JellyfinClient("http://jf.local:8096", DEVICE_ID,
                              token=TOKEN, user_id=USER_ID)


# ── The URL ──

def test_stream_url_carries_no_token(client):
    """The regression itself: a token in the query is a 401 on 12.1."""
    url = client.stream_url("item-42")
    assert "api_key" not in url
    assert TOKEN not in url


def test_stream_url_keeps_the_playback_parameters(client, api):
    """Dropping api_key must not drop the direct-play negotiation with it."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(
        client.stream_url("item-42")).query)
    assert query["UserId"] == [USER_ID]
    assert query["DeviceId"] == [DEVICE_ID]
    assert query["Container"] == [api.DIRECT_PLAY_CONTAINERS]
    assert query["MaxStreamingBitrate"] == [str(api.MAX_STREAMING_BITRATE)]


def test_stream_headers_carry_the_token(client):
    header = client.stream_headers()["Authorization"]
    assert header.startswith("MediaBrowser ")
    assert f'Token="{TOKEN}"' in header
    assert f'DeviceId="{DEVICE_ID}"' in header


def test_with_api_key_is_the_fallback_for_header_less_players(api, client):
    """Sonos & friends fetch the URL themselves — api_key is all they have."""
    url = api.with_api_key(client.stream_url("item-42"), TOKEN)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["api_key"] == [TOKEN]


def test_with_api_key_leaves_a_tokenless_call_alone(api):
    assert api.with_api_key("http://jf/x", "") == "http://jf/x"
    assert api.with_api_key("", TOKEN) == ""


# ── The probe that decides whether a networked player stands a chance ──

class _FakeSession:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return type("R", (), {"status_code": self.status})()


def test_query_auth_probe_goes_out_without_the_authorization_header(client):
    """With the header attached, every server answers 200 and the probe lies."""
    session = _FakeSession(401)
    client._session = session
    assert client.query_auth_works() is False
    _url, params, headers = session.calls[0]
    assert params == {"api_key": TOKEN}
    assert "Authorization" not in (headers or {})


def test_query_auth_probe_is_cached(client):
    session = _FakeSession(200)
    client._session = session
    assert client.query_auth_works() is True
    assert client.query_auth_works() is True
    assert len(session.calls) == 1


def test_unreachable_server_does_not_cache_a_guess(client):
    class _Boom:
        def get(self, *a, **k):
            raise OSError("no route to host")

    client._session = _Boom()
    assert client.query_auth_works() is True   # permissive while unknown
    assert client._query_auth is None          # ... but still undecided


# ── The mpv command line ──

@pytest.fixture
def local_player_module():
    if str(REPO_ROOT / "services") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "services"))
    sys.path.insert(0, str(PLAYERS_DIR))
    sys.modules.pop("local", None)
    import local
    return local


def test_mpv_command_without_headers_is_unchanged(local_player_module):
    cmd = local_player_module._mpv_command("http://host/x.mp3")
    assert cmd[:3] == ["mpv", "--ao=pulse", "http://host/x.mp3"]
    assert not [a for a in cmd if "http-header-fields" in a]


def test_mpv_gets_the_whole_header_despite_its_commas(local_player_module):
    """--http-header-fields splits on commas; Jellyfin's value is full of them.

    Passing it there sent four broken headers and the server answered
    400 — the -append form takes the value verbatim.
    """
    value = ('MediaBrowser Client="BeoSound 5c", Device="BeoSound 5c", '
             f'DeviceId="{DEVICE_ID}", Version="1", Token="{TOKEN}"')
    cmd = local_player_module._mpv_command("http://host/x.mp3",
                                           {"Authorization": value})
    opts = [a for a in cmd if a.startswith("--http-header-fields")]
    assert opts == [f"--http-header-fields-append=Authorization: {value}"]


# ── Which credential the source hands the active player ──

class _StubClient:
    token = TOKEN

    @staticmethod
    def stream_headers():
        return {"Authorization": f'MediaBrowser Token="{TOKEN}"'}


class _StubSource:
    """Just enough of JellyfinSource for the unbound _playable call."""

    def __init__(self, player):
        self.player = player
        self.auth = type("A", (), {"client": _StubClient()})()


@pytest.fixture
def jellyfin_service():
    if str(REPO_ROOT / "services") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "services"))
    import importlib
    return importlib.import_module("sources.jellyfin.service")


def test_local_player_gets_the_header_and_a_clean_url(jellyfin_service):
    playable = jellyfin_service.JellyfinService._playable
    url, headers = playable(_StubSource("local"), "http://jf/Audio/1/universal?x=1")
    assert url == "http://jf/Audio/1/universal?x=1"
    assert headers == {"Authorization": f'MediaBrowser Token="{TOKEN}"'}


def test_networked_player_gets_api_key_and_no_header(jellyfin_service):
    """A speaker that fetches the URL itself has nowhere to put a header."""
    playable = jellyfin_service.JellyfinService._playable
    url, headers = playable(_StubSource("remote"), "http://jf/Audio/1/universal?x=1")
    assert url == f"http://jf/Audio/1/universal?x=1&api_key={TOKEN}"
    assert headers is None


def test_no_session_means_no_credentials_invented(jellyfin_service):
    playable = jellyfin_service.JellyfinService._playable
    source = _StubSource("local")
    source.auth.client = None
    assert playable(source, "http://jf/x") == ("http://jf/x", None)


# ── Header hygiene on the way in ──

def test_play_body_headers_are_filtered():
    sys.path.insert(0, str(REPO_ROOT / "services"))
    from lib.player_base import _clean_headers

    assert _clean_headers(None) is None
    assert _clean_headers("Authorization: x") is None
    assert _clean_headers({}) is None
    assert _clean_headers({"Authorization": "MediaBrowser Token=\"t\""}) == {
        "Authorization": 'MediaBrowser Token="t"'}
    # CRLF in either half would let a caller append a request of its own
    assert _clean_headers({"X-A": "v\r\nX-B: w"}) is None
    assert _clean_headers({"X-A\n": "v"}) is None
    assert _clean_headers({"X-A": 7}) is None
