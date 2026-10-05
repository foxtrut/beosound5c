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
from unittest.mock import AsyncMock, patch

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
