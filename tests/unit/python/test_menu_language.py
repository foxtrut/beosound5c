"""Tests for the language-based arc menu label override in EventRouter._parse_menu.

config.json's menu keys ARE the display strings (e.g. "NEWS" -> id "news"),
so translating the menu can't be a client-side lookup on the title — it has
to override by source id, server-side, before the title ever reaches the
browser.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICES_DIR = Path(__file__).resolve().parents[3] / "services"
sys.path.insert(0, str(SERVICES_DIR))

DEFAULT_MENU = {
    "PLAYING": "playing", "NEWS": "news", "WEATHER": "weather",
    "SPOTIFY": "spotify", "SCENES": "scenes", "SYSTEM": "system",
}


def _make_router(menu=None, language="auto", player_type=""):
    menu = DEFAULT_MENU if menu is None else menu

    def fake_cfg(*keys, default=None):
        if keys == ("menu",):
            return menu
        if keys == ("language",):
            return language
        if keys == ("player", "type"):
            return player_type
        return default

    import router as router_mod
    # router.py does `from lib.config import cfg`, binding the name into its
    # own module namespace — patching lib.config.cfg wouldn't reach it, so
    # the mock has to replace router.cfg directly.
    with patch("router.cfg", side_effect=fake_cfg), \
         patch("lib.transport.Transport"), \
         patch("lib.volume_adapters.create_volume_adapter"), \
         patch("lib.volume_adapters.infer_volume_type", return_value="sonos"), \
         patch("lib.lydbro.LydbroHandler"):
        with patch.object(router_mod, "router_instance", MagicMock()):
            router = router_mod.EventRouter()
        # _parse_menu() runs during the async start(), not __init__ — call it
        # directly so this test doesn't need to drive the whole startup path.
        router._parse_menu()
    return router


def _titles(router):
    return {item["id"]: item["title"] for item in router._menu_order}


def test_default_language_keeps_config_titles_verbatim():
    router = _make_router(language="auto")
    assert _titles(router) == {
        "playing": "PLAYING", "news": "NEWS", "weather": "WEATHER",
        "spotify": "SPOTIFY", "scenes": "SCENES", "system": "SYSTEM",
    }


def test_danish_overrides_only_the_translated_ids():
    router = _make_router(language="da")
    titles = _titles(router)
    assert titles["news"] == "NYHEDER"
    assert titles["weather"] == "VEJR"
    assert titles["scenes"] == "SCENER"
    # Proper nouns / already-Danish-looking words are left as configured —
    # there's no MENU_LABELS entry for them.
    assert titles["spotify"] == "SPOTIFY"
    assert titles["system"] == "SYSTEM"


def test_danish_translates_the_injected_speakers_entry():
    """The SPEAKERS/join entry is injected in code, not read from config.json,
    so it needs the same override applied after insertion, not before."""
    router = _make_router(menu={"PLAYING": "playing"}, language="da", player_type="sonos")
    assert _titles(router)["join"] == "HØJTTALERE"


def test_unknown_language_falls_back_to_configured_titles():
    router = _make_router(language="fr")
    assert _titles(router)["news"] == "NEWS"
