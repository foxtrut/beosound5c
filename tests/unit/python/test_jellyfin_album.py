"""Jellyfin tracks carry an album name.

Without it the PLAYING view shows its em-dash placeholder where the album
should be (``album: data.album || '—'`` in web/js/media-manager.js), which is
what every Jellyfin track looked like on the device.

``Album`` is a base property on audio items and comes back on its own; it is
not an ItemFields value, so nothing may add it to a Fields query. The second
half here pins the cache guard: the incremental fetch only refetches a
playlist whose change signature moved, so a cache written before this field
existed would keep serving album-less tracks forever.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
JELLYFIN_DIR = REPO_ROOT / "services" / "sources" / "jellyfin"


@pytest.fixture(scope="module")
def fetch_mod():
    for p in (str(JELLYFIN_DIR), str(REPO_ROOT / "services")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "jellyfin_fetch", JELLYFIN_DIR / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def stream_url(self, item_id):
        return f"http://jf/Audio/{item_id}/universal?api_key=TOKEN"

    def image_url(self, item):
        return f"http://jf/Items/{item.get('Id')}/Images/Primary"


def _item(**over):
    item = {"Id": "abc", "Name": "Borderline", "Artists": ["Tame Impala"],
            "Album": "The Slow Rush"}
    item.update(over)
    return item


class TestConvertTrack:
    def test_takes_the_album_from_the_item(self, fetch_mod):
        t = fetch_mod.convert_track(FakeClient(), _item())
        assert t["album"] == "The Slow Rush"

    def test_falls_back_to_the_containing_album(self, fetch_mod):
        """Reached through an album, a track with no Album of its own still
        belongs to one."""
        t = fetch_mod.convert_track(FakeClient(), _item(Album=None),
                                    fallback_album="Currents")
        assert t["album"] == "Currents"

    def test_empty_rather_than_none_when_nothing_knows(self, fetch_mod):
        """A playlist track with no album: the key is always present, so the
        cache guard can tell "no album" from "written before the field".
        """
        t = fetch_mod.convert_track(FakeClient(), _item(Album=None))
        assert t["album"] == ""

    def test_leaves_the_other_fields_alone(self, fetch_mod):
        t = fetch_mod.convert_track(FakeClient(), _item())
        assert t["name"] == "Borderline"
        assert t["artist"] == "Tame Impala"
        assert t["id"] == "abc"
        assert "api_key=TOKEN" in t["url"]


class TestCacheGuard:
    """main() drops the cache when it cannot carry an album.

    Driven through main() with a stubbed client, because the guard lives in
    its cache-loading block.
    """

    @pytest.fixture
    def run_fetch(self, fetch_mod, tmp_path, monkeypatch, capsys):
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "server_url": "http://jf", "access_token": "TOKEN",
            "device_id": "dev", "user_id": "u1"}))

        album = {"Id": "alb1", "Name": "The Slow Rush",
                 "AlbumArtist": "Tame Impala", "DateCreated": "2020",
                 "ChildCount": 1}

        class Client(FakeClient):
            def __init__(self, *a, **kw):
                pass

            def public_info(self):
                return {"ServerName": "jf"}

            def audio_playlists(self):
                return []

            def recent_albums(self, limit=50):
                return [album]

            def album_tracks(self, album_id):
                return [_item(MediaType="Audio")]

        monkeypatch.setattr(fetch_mod, "JellyfinClient", Client)
        monkeypatch.setattr(fetch_mod, "build_digit_mapping",
                            lambda *a, **kw: {})
        monkeypatch.setattr(fetch_mod, "DIGIT_PLAYLISTS_FILE",
                            str(tmp_path / "digits.json"))
        out = tmp_path / "playlists.json"

        def run(cached=None):
            if cached is not None:
                out.write_text(json.dumps(cached))
            monkeypatch.setattr(sys, "argv", [
                "fetch.py", "--output", str(out),
                "--token-file", str(token_file)])
            rc = fetch_mod.main()
            assert rc == 0
            return json.loads(out.read_text()), capsys.readouterr().out

        return run

    # How a stream URL carries its token is a separate concern that other
    # work changes (api_key in the URL vs an Authorization header), and the
    # guard beside this one judges the cache on exactly that. These fixtures
    # therefore use a URL shape no such guard objects to — it carries the
    # current token and no api_key — so what is asserted below is the album
    # rule alone, whichever URL rule happens to sit next to it.
    NEUTRAL_URL = "http://jf/Audio/abc/universal?token=TOKEN"

    def _cached(self, **over):
        track = {"name": "Borderline", "artist": "Tame Impala", "id": "abc",
                 "image": "", "url": self.NEUTRAL_URL}
        track.update(over)
        return [{"id": "album:alb1", "name": "The Slow Rush",
                 "updatedAt": "2020:1", "tracks": [track]}]

    def test_a_cache_without_albums_is_dropped(self, run_fetch):
        playlists, log = run_fetch(cached=self._cached())
        assert "predate the album field" in log
        assert playlists[0]["tracks"][0]["album"] == "The Slow Rush"

    def test_a_cache_with_albums_is_kept(self, run_fetch):
        playlists, log = run_fetch(cached=self._cached(album="The Slow Rush"))
        assert "Invalidating cache" not in log
        assert playlists[0]["tracks"][0]["album"] == "The Slow Rush"

    def test_an_empty_album_still_counts_as_written(self, run_fetch):
        """A playlist track genuinely without an album must not re-trigger a
        full refresh on every single fetch."""
        _, log = run_fetch(cached=self._cached(album=""))
        assert "Invalidating cache" not in log
