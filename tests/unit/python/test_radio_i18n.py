"""Tests for the radio source's Danish translation.

Two layers: the lookup tables and their fallbacks in radio_i18n, and the
service actually using them — the root categories, the heading of each
browse level, station subtitles and the PLAYING metadata all have to follow
config.json's "language", and everything that is not a display string (the
browse paths and ids, which the UI and the integration tests key on) has to
stay English whatever the language is.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sources.radio import radio_i18n as i18n
from sources.radio.service import RadioService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _svc(mock_config, monkeypatch, language="da"):
    mock_config({"language": language})
    monkeypatch.setattr(RadioService, "_save_favourites", lambda self: None)
    monkeypatch.setattr(RadioService, "_save_last_station", lambda self: None)
    monkeypatch.setattr(RadioService, "_load_favourites", lambda self: None)
    monkeypatch.setattr(RadioService, "_load_last_station", lambda self: None)
    svc = RadioService()
    svc._favourites = []
    return svc


class _IcyBody:
    """Just enough of aiohttp's streaming body for _fetch_icy_title."""

    def __init__(self, data: bytes):
        self._data, self._pos = data, 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        if len(chunk) < n:
            raise EOFError("stream ended")
        self._pos += n
        return chunk


class _IcyResponse:
    def __init__(self, metaint, payload: bytes, status: int = 200):
        self.status = status
        self.headers = {} if metaint is None else {"icy-metaint": str(metaint)}
        audio = b"\0" * (metaint if isinstance(metaint, int) and metaint < 100_000 else 0)
        block = bytes([(len(payload) + 15) // 16]) + payload.ljust(
            ((len(payload) + 15) // 16) * 16, b"\0")
        self.content = _IcyBody(audio + block)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _icy_response(metaint, payload, status=200):
    return _IcyResponse(metaint, payload, status)


class TestNormalise:
    def test_auto_is_english(self):
        """A service has no browser locale to follow, so "auto" means English
        here — the same rule router.py's MENU_LABELS uses."""
        assert i18n.normalise("auto") == "en"

    def test_unknown_language_falls_back_to_english(self):
        assert i18n.normalise("fr") == "en"
        assert i18n.normalise(None) == "en"

    def test_region_suffix_is_dropped(self):
        assert i18n.normalise("da-DK") == "da"


class TestLabels:
    def test_danish_labels(self):
        assert i18n.t("favourites", "da") == "Favoritter"
        assert i18n.t("countries", "da") == "Lande"
        assert i18n.t("genres", "da") == "Genrer"
        assert i18n.t("languages", "da") == "Sprog"
        assert i18n.t("popular", "da") == "Populære"
        assert i18n.t("unknown", "da") == "Ukendt"

    def test_english_labels(self):
        assert i18n.t("favourites", "en") == "Favourites"
        assert i18n.t("favourites", "auto") == "Favourites"

    def test_every_english_key_has_a_danish_label(self):
        assert set(i18n.STRINGS["da"]) == set(i18n.STRINGS["en"])
        assert all(i18n.STRINGS["da"].values())


class TestCountries:
    def test_translates_api_spellings(self):
        assert i18n.country("Germany", "da") == "Tyskland"
        assert i18n.country("The Netherlands", "da") == "Holland"
        assert i18n.country("The United States Of America", "da") == "USA"
        assert i18n.country("Coted Ivoire", "da") == "Elfenbenskysten"

    def test_case_insensitive(self):
        assert i18n.country("denmark", "da") == "Danmark"

    def test_unknown_country_passes_through(self):
        """The English name doubles as the browse path, so an unknown one has
        to survive untouched rather than become empty."""
        assert i18n.country("Narnia", "da") == "Narnia"

    def test_english_is_untouched(self):
        assert i18n.country("Germany", "en") == "Germany"

    def test_table_keys_are_lower_case(self):
        assert all(k == k.lower() for k in i18n.COUNTRIES_DA)
        assert all(v for v in i18n.COUNTRIES_DA.values())


class TestLanguagesAndGenres:
    def test_language_names(self):
        assert i18n.language("english", "da") == "Engelsk"
        assert i18n.language("german", "da") == "Tysk"

    def test_upstream_misspelling(self):
        """The /json/languages endpoint is free text — 'engilsh' is really in
        the live top of that list."""
        assert i18n.language("engilsh", "da") == "Engelsk"

    def test_unknown_language_is_title_cased(self):
        assert i18n.language("klingon", "da") == "Klingon"
        assert i18n.language("english", "en") == "English"

    def test_genre_words_that_differ(self):
        assert i18n.genre_label("news", "da") == "Nyheder"
        assert i18n.genre_label("classical", "da") == "Klassisk"
        assert i18n.genre_label("oldies", "da") == "Gamle hits"

    def test_genre_words_that_do_not_differ(self):
        """Pop, rock and jazz are the same word in Danish, so they're absent
        from the table and title-cased like before."""
        assert i18n.genre_label("pop", "da") == "Pop"
        assert i18n.genre_label("jazz", "da") == "Jazz"

    def test_spanish_tags_are_left_alone(self):
        assert i18n.genre_label("entretenimiento", "da") == "Entretenimiento"

    def test_inline_tag_keeps_english_exactly_as_submitted(self):
        """Station subtitles and the PLAYING view have always shown the raw
        tag. Translating must not restyle the English output — a test in
        test_radio_service.py asserts on the lower-case tag."""
        assert i18n.genre_tag("rock", "en") == "rock"
        assert i18n.genre_tag("public radio", "auto") == "public radio"

    def test_inline_tag_in_danish(self):
        assert i18n.genre_tag("news", "da") == "Nyheder"
        assert i18n.genre_tag("rock", "da") == "Rock"


class TestRootCategories:
    def test_favourites_comes_first(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="auto")
        items = svc._root_categories()["items"]
        assert items[0]["id"] == "favourites"

    def test_danish_labels(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        names = [i["name"] for i in svc._root_categories()["items"]]
        assert names == ["Favoritter", "Populære", "Danmark", "Sverige",
                         "Lande", "Genrer", "Sprog"]

    def test_english_labels(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="auto")
        names = [i["name"] for i in svc._root_categories()["items"]]
        assert names == ["Favourites", "Popular", "Danish", "Swedish",
                         "Countries", "Genres", "Languages"]

    def test_paths_and_ids_stay_english(self, mock_config, monkeypatch):
        """The ids are the contract the UI drills down on and the integration
        tests assert against — translating them would break both."""
        svc = _svc(mock_config, monkeypatch, language="da")
        items = svc._root_categories()["items"]
        assert [i["id"] for i in items] == [i["path"] for i in items]
        assert [i["id"] for i in items] == [
            "favourites", "popular", "danmark", "sverige",
            "countries", "genres", "languages",
        ]

    def test_language_change_needs_no_restart(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="auto")
        assert svc._root_categories()["items"][0]["name"] == "Favourites"
        mock_config({"language": "da"})
        assert svc._root_categories()["items"][0]["name"] == "Favoritter"


class TestBrowseHeadings:
    def test_favourites_heading(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        data = _run(svc._browse("favourites"))
        assert data["name"] == "Favoritter"
        assert data["path"] == "favourites"

    def test_unknown_path_heading(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        data = _run(svc._browse("nonexistent"))
        assert data["name"] == "Ukendt"
        assert data["items"] == []

    def test_country_list_is_translated_but_paths_are_not(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        api = AsyncMock(return_value=[
            {"name": "Germany", "stationcount": 500},
            {"name": "The Netherlands", "stationcount": 100},
        ])
        with patch.object(RadioService, "_api_get", api):
            data = _run(svc._browse("countries"))
        assert data["name"] == "Lande"
        assert [i["name"] for i in data["items"]] == ["Tyskland", "Holland"]
        assert [i["path"] for i in data["items"]] == [
            "countries/Germany", "countries/The Netherlands",
        ]

    def test_country_drill_down_heading(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        with patch.object(RadioService, "_api_get", AsyncMock(return_value=[])):
            data = _run(svc._browse("countries/Germany"))
        assert data["name"] == "Tyskland"
        assert data["path"] == "countries/Germany"

    def test_genre_list_is_translated(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        api = AsyncMock(return_value=[
            {"name": "news", "stationcount": 500},
            {"name": "pop", "stationcount": 400},
        ])
        with patch.object(RadioService, "_api_get", api):
            data = _run(svc._browse("genres"))
        assert data["name"] == "Genrer"
        assert [i["name"] for i in data["items"]] == ["Nyheder", "Pop"]
        assert [i["path"] for i in data["items"]] == ["genres/news", "genres/pop"]

    def test_language_list_is_translated(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        api = AsyncMock(return_value=[{"name": "german", "stationcount": 500}])
        with patch.object(RadioService, "_api_get", api):
            data = _run(svc._browse("languages"))
        assert data["name"] == "Sprog"
        assert data["items"][0]["name"] == "Tysk"
        assert data["items"][0]["path"] == "languages/german"


class TestStationStrings:
    STATION = {
        "stationuuid": "uuid-1",
        "name": "P3",
        "url_resolved": "http://example.com/p3.mp3",
        "favicon": "",
        "country": "Denmark",
        "tags": "news,pop",
        "codec": "MP3",
        "bitrate": 128,
    }

    def test_subtitle_genres_are_translated(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        item = svc._station_to_item(self.STATION)
        assert item["subtitle"].startswith("Nyheder, Pop")
        assert "128kbps" in item["subtitle"]

    def test_subtitle_falls_back_to_country(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        item = svc._station_to_item({**self.STATION, "tags": ""})
        assert item["subtitle"].startswith("Danmark")

    def test_station_name_is_never_translated(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        assert svc._station_to_item(self.STATION)["name"] == "P3"

    def test_playing_metadata_is_translated(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="da")
        meta = svc._build_meta(self.STATION)
        assert meta["artist"] == "Nyheder, Pop"
        assert meta["album"].startswith("Danmark")

    def test_playing_metadata_in_english_is_unchanged(self, mock_config, monkeypatch):
        """English output stays byte-identical to what it was before the
        translation went in: raw tags, English country name."""
        svc = _svc(mock_config, monkeypatch, language="auto")
        meta = svc._build_meta(self.STATION)
        assert meta["artist"] == "news, pop"
        assert meta["album"].startswith("Denmark")

    def test_subtitle_in_english_is_unchanged(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch, language="auto")
        item = svc._station_to_item(self.STATION)
        assert item["subtitle"].startswith("news, pop")


# ── Curated overrides ──
# The Danish list plays DR's AAC HLS and shows DR's channel logos, neither of
# which the Radio Browser entry carries, so both come from a UUID-keyed map.
# A typo in a UUID there is silent — the station just keeps the database's
# own stream or favicon — so these pin the wiring.

from sources.radio import service as radio_service


class TestCuratedOverrides:
    def test_stream_overrides_cover_only_curated_stations(self):
        assert set(radio_service.STATION_STREAM) <= set(radio_service.CURATED_DANMARK)

    def test_artwork_overrides_cover_only_curated_stations(self):
        assert set(radio_service.STATION_ARTWORK) <= set(radio_service.CURATED_DANMARK)

    def test_no_override_points_at_the_broken_dr_origin(self):
        """drliveradio1's master playlist advertises variants that all 404,
        which is what made DR's AAC entries unplayable here."""
        assert not any("drliveradio1" in url
                       for url, _codec, _rate in radio_service.STATION_STREAM.values())

    def test_stream_override_wins_over_the_database_url(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        uuid = "9610bbeb-0601-11e8-ae97-52543be04c81"  # DR P4 Østjylland
        url = svc._stream_for({"stationuuid": uuid, "url_resolved": "http://old/A14H.mp3"})
        assert url == radio_service.STATION_STREAM[uuid][0]

    def test_subtitle_describes_the_stream_that_plays(self, mock_config, monkeypatch):
        """The Radio Browser entry is the MP3 one, so its codec and bitrate
        describe a stream we never play — the subtitle read "MP3 128kbps"
        over AAC audio until the override carried them too."""
        svc = _svc(mock_config, monkeypatch)
        item = svc._station_to_item({
            "stationuuid": "9610bbeb-0601-11e8-ae97-52543be04c81",
            "name": "DR P4 Østjyllands Radio", "tags": "regional radio",
            "codec": "MP3", "bitrate": 128, "country": "Denmark",
        })
        assert "AAC 320kbps" in item["subtitle"]
        assert "MP3" not in item["subtitle"]
        assert (item["codec"], item["bitrate"]) == ("AAC", 320)

    def test_playing_view_describes_the_stream_that_plays(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        meta = svc._build_meta({
            "stationuuid": "9610bbeb-0601-11e8-ae97-52543be04c81",
            "name": "DR P4 Østjyllands Radio", "tags": "", "codec": "MP3",
            "bitrate": 128, "country": "Denmark",
        })
        assert "AAC 320kbps" in meta["album"]

    def test_station_without_override_keeps_its_own_codec(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        assert svc._codec_for({"stationuuid": "not-curated",
                               "codec": "MP3", "bitrate": 192}) == ("MP3", 192)

    def test_station_without_override_keeps_its_own_url(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        s = {"stationuuid": "not-curated", "url_resolved": "http://example.com/x.mp3"}
        assert svc._stream_for(s) == "http://example.com/x.mp3"
        assert svc._artwork_for({**s, "favicon": "http://example.com/x.png"}) \
            == "http://example.com/x.png"

    def test_playback_uses_the_override(self, mock_config, monkeypatch):
        """The regression this guards: _play_station read url_resolved
        directly, so an override that only reached the browse item would
        have left the old stream playing."""
        svc = _svc(mock_config, monkeypatch)
        uuid = "960f5a18-0601-11e8-ae97-52543be04c81"  # DR P1
        played = []
        monkeypatch.setattr(RadioService, "player_play",
                            AsyncMock(side_effect=lambda url, **kw: played.append(url) or True))
        monkeypatch.setattr(RadioService, "register", AsyncMock())
        monkeypatch.setattr(RadioService, "post_media_update", AsyncMock())
        _run(svc._play_station({"stationuuid": uuid, "name": "DR P1",
                                "url_resolved": "http://live-icy.dr.dk/A/A03H.mp3"}))
        assert played and played[0] == radio_service.STATION_STREAM[uuid][0]


class TestLocalStationTiles:
    """The four stations Radio Browser has no usable artwork for get a tile
    generated on the device, as a data: URI."""

    TILED = {
        "9610c1ca-0601-11e8-ae97-52543be04c81": "DR Nyheder",
        "7a17dda6-45b5-11e8-8919-52543be04c81": "Classic FM",
        "0d939aa0-cce8-4841-92fe-1a03d36da0d3": "Classic Rock Danmark",
        "6397fc3c-fca0-11e9-bbf2-52543be04c81": "Radio4",
    }

    def test_each_tiled_station_has_a_data_uri(self):
        for uuid in self.TILED:
            art = radio_service.STATION_ARTWORK[uuid]
            assert art.startswith("data:image/svg+xml,")

    def test_tile_wins_over_a_broken_upstream_favicon(self, mock_config, monkeypatch):
        """Radio4's favicon.ico is 78 bytes of the literal text
        'data:image...' — the tile has to replace it, not defer to it."""
        svc = _svc(mock_config, monkeypatch)
        uuid = "6397fc3c-fca0-11e9-bbf2-52543be04c81"
        art = svc._artwork_for({"stationuuid": uuid,
                                "favicon": "https://radio4.dk/favicon.ico"})
        assert art == radio_service.STATION_ARTWORK[uuid]

    def test_tile_renders_as_svg(self):
        from urllib.parse import unquote
        svg = unquote(radio_service.STATION_ARTWORK[
            "7a17dda6-45b5-11e8-8919-52543be04c81"].split(",", 1)[1])
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert 'viewBox="0 0 800 800"' in svg
        assert "CLASSIC" in svg and "FM" in svg

    def test_proxy_serves_a_tile_back(self, mock_config, monkeypatch):
        """The UI routes every artwork URL through /favicon, so the proxy has
        to hand our own data: URIs back rather than reject them as not-URLs."""
        svc = _svc(mock_config, monkeypatch)
        uuid = "9610c1ca-0601-11e8-ae97-52543be04c81"
        request = MagicMock()
        request.query = {"url": radio_service.STATION_ARTWORK[uuid]}
        resp = _run(svc._handle_favicon(request))
        assert resp.status == 200
        assert resp.content_type == "image/svg+xml"
        assert resp.body.startswith(b"<svg")

    def test_proxy_still_rejects_a_non_url(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        request = MagicMock()
        request.query = {"url": "javascript:alert(1)"}
        assert _run(svc._handle_favicon(request)).status == 400

    def test_playing_artwork_escapes_the_favicon(self, mock_config, monkeypatch):
        """The favicon is a query value. Unescaped, a favicon carrying its
        own '?ver=...' (Radio Soft and Nova both do) arrived truncated."""
        svc = _svc(mock_config, monkeypatch)
        meta = svc._build_meta({"stationuuid": "x", "name": "Radio Soft",
                                "favicon": "https://example.com/l.jpg?ver=149"})
        assert "ver%3D149" in meta["artwork"]
        assert meta["artwork"].count("?") == 1


class TestAdoptRunningStream:
    """Restarting this service alone leaves mpv streaming, because the player
    is a separate service. Registering "available" regardless is what left
    audio playing with an empty PLAYING view."""

    STATION = {
        "stationuuid": "9610bbeb-0601-11e8-ae97-52543be04c81",
        "name": "DR P4 Østjyllands Radio", "tags": "regional radio",
        "codec": "MP3", "bitrate": 128, "country": "Denmark",
        "url_resolved": "http://live-icy.dr.dk/A/A14H.mp3",
    }

    def _svc_with_last_station(self, mock_config, monkeypatch, state, playing_url):
        svc = _svc(mock_config, monkeypatch)
        svc._current_station = dict(self.STATION)
        monkeypatch.setattr(RadioService, "player_state", AsyncMock(return_value=state))
        monkeypatch.setattr(RadioService, "player_track_uri",
                            AsyncMock(return_value=playing_url))
        monkeypatch.setattr(RadioService, "post_media_update", AsyncMock())
        monkeypatch.setattr(RadioService, "_start_state_poll", lambda self: None)
        registered = []
        monkeypatch.setattr(RadioService, "register",
                            AsyncMock(side_effect=lambda s, **kw: registered.append(s)))
        return svc, registered

    def test_adopts_our_own_stream(self, mock_config, monkeypatch):
        our_url = radio_service.STATION_STREAM[self.STATION["stationuuid"]][0]
        svc, registered = self._svc_with_last_station(
            mock_config, monkeypatch, "playing", our_url)
        _run(svc._adopt_running_stream())
        assert registered == ["playing"]
        assert svc._playing_state == "playing"
        svc.post_media_update.assert_awaited()

    def test_adopts_a_paused_stream_as_paused(self, mock_config, monkeypatch):
        our_url = radio_service.STATION_STREAM[self.STATION["stationuuid"]][0]
        svc, registered = self._svc_with_last_station(
            mock_config, monkeypatch, "paused", our_url)
        _run(svc._adopt_running_stream())
        assert registered == ["paused"]

    def test_does_not_claim_another_sources_stream(self, mock_config, monkeypatch):
        """The player could be playing Jellyfin or Spotify — adopting that
        would make radio claim someone else's playback."""
        svc, registered = self._svc_with_last_station(
            mock_config, monkeypatch, "playing", "http://jellyfin/Audio/abc/universal")
        _run(svc._adopt_running_stream())
        assert registered == ["available"]
        assert svc._playing_state != "playing"

    def test_stopped_player_registers_available(self, mock_config, monkeypatch):
        our_url = radio_service.STATION_STREAM[self.STATION["stationuuid"]][0]
        svc, registered = self._svc_with_last_station(
            mock_config, monkeypatch, "stopped", our_url)
        _run(svc._adopt_running_stream())
        assert registered == ["available"]

    def test_no_last_station_registers_available(self, mock_config, monkeypatch):
        svc, registered = self._svc_with_last_station(
            mock_config, monkeypatch, "playing", "whatever")
        svc._current_station = None
        _run(svc._adopt_running_stream())
        assert registered == ["available"]

    def test_unreachable_player_still_registers(self, mock_config, monkeypatch):
        """A player that can't be reached must not stop the source coming up."""
        svc, registered = self._svc_with_last_station(
            mock_config, monkeypatch, "playing", "x")
        monkeypatch.setattr(RadioService, "player_state",
                            AsyncMock(side_effect=OSError("no route")))
        _run(svc._adopt_running_stream())
        assert registered == ["available"]


class TestDisplayNames:
    P4 = "9610bbeb-0601-11e8-ae97-52543be04c81"

    def test_long_name_is_shortened_for_display(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        station = {"stationuuid": self.P4, "name": "DR P4 Østjyllands Radio"}
        assert svc._name_for(station) == "DR P4 Østjylland"
        assert svc._station_to_item({**station, "tags": ""})["name"] == "DR P4 Østjylland"
        assert svc._build_meta({**station, "tags": ""})["title"] == "DR P4 Østjylland"

    def test_other_stations_keep_their_name(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        assert svc._name_for({"stationuuid": "x", "name": "Skala FM"}) == "Skala FM"

    def test_overrides_only_cover_curated_stations(self):
        assert set(radio_service.STATION_NAME) <= set(radio_service.CURATED_DANMARK)

    def test_play_by_name_still_matches_the_database_name(self, mock_config, monkeypatch):
        """The override is display-only — the station dict keeps the name
        Radio Browser knows, so a favourite saved from here and the voice
        shortcut both still resolve."""
        svc = _svc(mock_config, monkeypatch)
        svc._favourites = [{"stationuuid": self.P4, "name": "DR P4 Østjyllands Radio",
                            "url_resolved": "http://x"}]
        found = _run(svc._find_station_by_name("østjylland"))
        assert found is not None and found["stationuuid"] == self.P4


class TestIcyNowPlaying:
    """The song title comes from the stream's ICY metadata. DR is played as
    HLS, which carries none, but the catalogue entry still holds DR's Icecast
    URL — which does — so that is what gets read."""

    P4 = "9610bbeb-0601-11e8-ae97-52543be04c81"
    STATION = {"stationuuid": P4, "name": "DR P4 Østjyllands Radio",
               "url_resolved": "http://live-icy.dr.dk/A/A14H.mp3",
               "tags": "regional radio", "codec": "MP3", "bitrate": 128,
               "country": "Denmark"}

    def test_reads_from_the_catalogue_url_not_the_played_one(self, mock_config, monkeypatch):
        """_stream_for sends playback to HLS; the ICY read must not follow it
        there, or DR would never report a title."""
        svc = _svc(mock_config, monkeypatch)
        assert ".m3u8" in svc._stream_for(self.STATION)
        assert svc._icy_url_for(self.STATION) == "http://live-icy.dr.dk/A/A14H.mp3"

    def test_an_hls_only_station_has_nowhere_to_read(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        assert svc._icy_url_for({"stationuuid": "x",
                                 "url_resolved": "https://e.com/x/master.m3u8"}) == ""

    def test_song_rides_alongside_the_title(self, mock_config, monkeypatch):
        """The station keeps the title. The song travels as `track`, which the
        PLAYING view leads with and the immersive view never reads."""
        svc = _svc(mock_config, monkeypatch)
        svc._icy_title, svc._icy_uuid = "Nick Cave & The Bad Seeds - Into My Arms", self.P4
        meta = svc._build_meta(self.STATION)
        assert meta["track"] == "Nick Cave & The Bad Seeds - Into My Arms"
        assert meta["title"] == "DR P4 Østjylland"
        assert meta["artist"] == "Regionalradio"
        assert "AAC 320kbps" in meta["album"]

    def test_no_song_sends_no_track_field(self, mock_config, monkeypatch):
        """Nothing extra in the payload, so nothing changes anywhere."""
        svc = _svc(mock_config, monkeypatch)
        meta = svc._build_meta(self.STATION)
        assert "track" not in meta
        assert meta["title"] == "DR P4 Østjylland"
        assert meta["artist"] == "Regionalradio"

    def test_a_song_from_another_station_is_not_sent(self, mock_config, monkeypatch):
        """Stale state must not label one station with another's song."""
        svc = _svc(mock_config, monkeypatch)
        svc._icy_title, svc._icy_uuid = "Billy Idol - White Wedding", "some-other-uuid"
        meta = svc._build_meta(self.STATION)
        assert "track" not in meta
        assert meta["title"] == "DR P4 Østjylland"

    def test_parses_a_streamtitle(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        payload = b"StreamTitle='Billy Idol - White Wedding';StreamUrl='';\x00\x00"
        resp = _icy_response(metaint=16, payload=payload)
        svc._api_session = SimpleNamespace(get=lambda *a, **kw: resp)
        assert _run(svc._fetch_icy_title("http://x/y.mp3")) == "Billy Idol - White Wedding"

    def test_empty_streamtitle_reads_as_no_title(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        resp = _icy_response(metaint=16, payload=b"StreamTitle='';StreamUrl='';\x00")
        svc._api_session = SimpleNamespace(get=lambda *a, **kw: resp)
        assert _run(svc._fetch_icy_title("http://x/y.mp3")) == ""

    def test_a_stream_without_icy_is_not_read(self, mock_config, monkeypatch):
        """No icy-metaint means no metadata channel — don't read audio
        waiting for one that never comes."""
        svc = _svc(mock_config, monkeypatch)
        resp = _icy_response(metaint=None, payload=b"")
        svc._api_session = SimpleNamespace(get=lambda *a, **kw: resp)
        assert _run(svc._fetch_icy_title("http://x/y.mp3")) == ""

    def test_absurd_metaint_is_refused(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        resp = _icy_response(metaint=50_000_000, payload=b"")
        svc._api_session = SimpleNamespace(get=lambda *a, **kw: resp)
        assert _run(svc._fetch_icy_title("http://x/y.mp3")) == ""

    def test_changing_station_drops_the_old_song(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        svc._icy_title, svc._icy_uuid = "Old Song", "old-uuid"
        monkeypatch.setattr(RadioService, "player_play", AsyncMock(return_value=True))
        monkeypatch.setattr(RadioService, "register", AsyncMock())
        monkeypatch.setattr(RadioService, "post_media_update", AsyncMock())
        monkeypatch.setattr(RadioService, "_start_state_poll", lambda self: None)
        monkeypatch.setattr(RadioService, "_start_icy_poll", lambda self: None)
        _run(svc._play_station(dict(self.STATION)))
        assert svc._icy_title == "" and svc._icy_uuid == ""

    def test_strips_drs_leading_slash(self, mock_config, monkeypatch):
        """DR sends "/ Lana Del Rey - West Coast"."""
        svc = _svc(mock_config, monkeypatch)
        resp = _icy_response(metaint=16, payload=b"StreamTitle='/ Lana Del Rey - West Coast';")
        svc._api_session = SimpleNamespace(get=lambda *a, **kw: resp)
        assert _run(svc._fetch_icy_title("http://x/y.mp3")) == "Lana Del Rey - West Coast"

    def test_keeps_a_slash_inside_the_title(self, mock_config, monkeypatch):
        svc = _svc(mock_config, monkeypatch)
        resp = _icy_response(metaint=16, payload=b"StreamTitle='AC/DC - Highway to Hell';")
        svc._api_session = SimpleNamespace(get=lambda *a, **kw: resp)
        assert _run(svc._fetch_icy_title("http://x/y.mp3")) == "AC/DC - Highway to Hell"

    def test_the_track_field_reaches_the_router_payload(self, mock_config, monkeypatch):
        """post_media_update has a fixed signature, so an unknown key would
        be a TypeError rather than a silently dropped field."""
        import inspect
        from lib.source_base import SourceBase
        assert "track" in inspect.signature(SourceBase.post_media_update).parameters
