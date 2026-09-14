#!/usr/bin/env python3
"""Generate the demo content the UI shows when no services are running.

`web/json/demo/` is what the browser emulator and `cd web && python3 -m
http.server` render (see web/js/demo-backend.js). It is generated rather than
hand-written so the shapes stay consistent with what each service returns and
the artwork stays self-contained — every cover is an inline SVG, so nothing
hotlinks a CDN that will rot.

Usage:
    python3 tools/gen-demo-data.py            # rewrite web/json/demo/
    python3 tools/gen-demo-data.py --check    # fail if the files are stale
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parents[1] / "web" / "json" / "demo"


def cover(hue: int, label: str, sub: str, size: int = 300) -> str:
    """A gradient cover as an inline SVG data URI.

    Two details that broke this before: text must be XML-escaped (an artist
    like "Satie & Debussy" otherwise produces an unparseable SVG, and the UI
    silently falls back to a two-letter placeholder), and the SVG needs a
    viewBox or it will not scale when drawn larger than its natural size.
    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="hsl({hue},45%,34%)"/>'
        f'<stop offset="1" stop-color="hsl({(hue + 40) % 360},50%,13%)"/>'
        f'</linearGradient></defs>'
        f'<rect width="{size}" height="{size}" fill="url(#g)"/>'
        f'<text x="50%" y="47%" text-anchor="middle" fill="#fff" '
        f'font-family="Helvetica Neue,sans-serif" font-size="{size // 14}" '
        f'letter-spacing="1.5">{escape(label)}</text>'
        f'<text x="50%" y="58%" text-anchor="middle" fill="rgba(255,255,255,.7)" '
        f'font-family="Helvetica Neue,sans-serif" font-size="{size // 22}">{escape(sub)}</text>'
        f'</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def playlist(pid: str, name: str, artist: str, hue: int, tracks: list[str]) -> dict:
    return {
        "id": pid,
        "name": name,
        "image": cover(hue, name.upper()[:16], artist),
        "tracks": [
            {"name": t, "artist": artist, "id": f"{pid}-{i}",
             "image": cover(hue, artist.upper()[:14], t[:26], 160)}
            for i, t in enumerate(tracks)
        ],
    }


def station(name: str, country: str, tags: str, hue: int) -> dict:
    slug = name.lower().replace(" ", "-")
    return {
        "type": "station", "name": name, "id": f"demo-{slug}", "path": f"station/{slug}",
        "image": cover(hue, name.upper()[:12], country, 200),
        "url_resolved": "", "favicon": "", "country": country, "tags": tags,
        "codec": "MP3", "bitrate": 192,
    }


# ── Libraries ─────────────────────────────────────────────────────────────────
# Placeholder repertoire, deliberately generic: no real artist's catalogue, and
# distinct per service so the emulator doesn't look copy-pasted.
LIBRARIES: dict[str, list[tuple]] = {
    "spotify": [
        ("Morning Coffee", "Satie and Debussy", 28,
         ["Gymnopédie No. 1", "Rêverie", "Arabesque No. 1", "Clair de Lune"]),
        ("Late Night Strings", "Chamber Ensemble", 265,
         ["Adagio for Strings", "Nocturne in E-flat", "Air on the G String"]),
        ("Kitchen Jazz", "Demo Jazz Trio", 150,
         ["Blue Room", "Take the Ferry", "Slow Sunday", "Midnight Tram"]),
        ("Nordic Ambient", "Northern Drift", 190,
         ["Archipelago", "First Frost", "Long Light", "Winter Harbour"]),
        ("Hi-Fi Test Tracks", "Test Signals", 205,
         ["Bass Sweep", "Room Impulse", "Stereo Image", "Silence (0:30)"]),
        ("Acoustic Guitar", "Solo Guitar", 35,
         ["Asturias", "Recuerdos", "Cavatina", "Romanza"]),
    ],
    "apple_music": [
        ("Piano Reworks", "Modern Piano", 340, ["Opening", "Second Light", "Slow Return", "Coda"]),
        ("Sunday Chorales", "Cathedral Choir", 300, ["Jesu, Joy", "Ave Verum", "Lux Aeterna"]),
        ("Cinematic Strings", "Score Orchestra", 20, ["First Frame", "The Chase", "End Titles"]),
        ("Bossa Afternoon", "Rio Quartet", 130, ["Corcovado", "Wave", "Meditação"]),
        ("Electronica 1998", "Analog Loops", 240, ["Tape Delay", "Bright Room", "Night Bus"]),
    ],
    "tidal": [
        ("Masters Selection", "Studio Masters", 200, ["Take Five", "So What", "Blue in Green"]),
        ("Vinyl Transfers", "Archive Series", 15, ["Side A", "Side B", "Bonus Track"]),
        ("Hi-Res Classical", "Philharmonic", 260,
         ["Symphony No. 5 I", "Symphony No. 5 II", "Symphony No. 5 III"]),
        ("Deep Focus", "Ambient Works", 175, ["Long Form I", "Long Form II"]),
        ("Nordic Jazz", "Oslo Trio", 105, ["Fjord", "Snowline", "Late Ferry"]),
    ],
    "plex": [
        ("Ripped CDs", "Various Artists", 90, ["Track 01", "Track 02", "Track 03", "Track 04"]),
        ("Live Recordings", "Bootleg Series", 45, ["Intro", "Main Set", "Encore"]),
        ("Christmas Box", "Seasonal", 355, ["Sleigh Ride", "Winter Song", "First Snow"]),
        ("Kids' Playlist", "Family", 60, ["Counting Song", "Animal Parade", "Bedtime"]),
    ],
    "jellyfin": [
        ("Home Server Mix", "Various Artists", 195, ["Opening Bars", "Second Wind", "Slow Fade"]),
        ("Sunday Morning", "Coffee Sessions", 30, ["First Cup", "Slow Start", "Out the Door"]),
        ("Concert Bootlegs", "Field Recordings", 285, ["Soundcheck", "Set One", "Set Two"]),
        ("Radio Rips", "Broadcast Archive", 120, ["Station ID", "The Interview", "Sign Off"]),
    ],
}

SCENES = [
    {"id": "dinner", "name": "Dinner", "icon": "fork-knife", "color": "#fa0"},
    {"id": "cozy", "name": "Cozy", "icon": "fire", "color": "#fa0"},
    {"id": "reading", "name": "Reading", "icon": "book-open", "color": "#fa0"},
    {"id": "all_on", "name": "All on", "icon": "sun", "color": "#fa0"},
    {"id": "all_off", "name": "All off", "icon": "power", "color": "#c55"},
]


def build() -> dict[str, object]:
    files: dict[str, object] = {}

    for source, entries in LIBRARIES.items():
        files[f"{source}_playlists.json"] = [
            playlist(f"demo-{source}-{i}", name, artist, hue, tracks)
            for i, (name, artist, hue, tracks) in enumerate(entries)
        ]

    spotify = files["spotify_playlists.json"]
    files["digit_playlists.json"] = {
        str(i + 1): {"id": p["id"], "name": p["name"]} for i, p in enumerate(spotify)
    }
    files["scenes.json"] = SCENES

    # Radio browses a hierarchy; keyed by browse path, each level shaped like
    # the service's {path, parent, name, items} envelope.
    files["radio_browse.json"] = {
        "": {"path": "", "parent": None, "name": "Radio", "items": [
            {"type": "category", "name": "Popular", "id": "popular", "path": "popular",
             "icon": "star", "color": "#F9CA24"},
            {"type": "category", "name": "Favourites", "id": "favourites", "path": "favourites",
             "icon": "heart", "color": "#FF6B6B"},
            {"type": "category", "name": "Genres", "id": "genres", "path": "genres",
             "icon": "music-notes", "color": "#FD79A8"},
        ]},
        "popular": {"path": "popular", "parent": "", "name": "Popular", "items": [
            station("BBC Radio 3", "United Kingdom", "classical", 20),
            station("FIP", "France", "eclectic", 320),
            station("KEXP", "United States", "indie", 265),
            station("Radio Swiss Jazz", "Switzerland", "jazz", 150),
            station("NRK Klassisk", "Norway", "classical", 200),
        ]},
        "favourites": {"path": "favourites", "parent": "", "name": "Favourites", "items": [
            station("Jazz24", "United States", "jazz", 210),
            station("Radio Swiss Classic", "Switzerland", "classical", 150),
            station("NTS 1", "United Kingdom", "electronic", 90),
        ]},
        "genres": {"path": "genres", "parent": "", "name": "Genres", "items": [
            {"type": "category", "name": genre, "id": genre.lower(),
             "path": f"genres/{genre.lower()}", "icon": "music-note", "color": colour}
            for genre, colour in (("Classical", "#74B9FF"), ("Jazz", "#F9CA24"),
                                  ("Ambient", "#A29BFE"))
        ]},
    }
    files["radio_favourites.json"] = files["radio_browse.json"]["favourites"]["items"]

    files["news_articles.json"] = [
        {"name": "World", "id": "world", "articles": [
            {"name": "Demo headline: harbour lights restored", "id": "w1",
             "pages": ["Demo content shipped with the BeoSound 5c emulator."]},
            {"name": "Demo headline: night train returns", "id": "w2",
             "pages": ["Demo content shipped with the BeoSound 5c emulator."]},
        ]},
        {"name": "Culture", "id": "culture", "articles": [
            {"name": "Demo headline: a 2009 flagship, rebuilt", "id": "c1",
             "pages": ["Demo content shipped with the BeoSound 5c emulator."]},
        ]},
        {"name": "Technology", "id": "tech", "articles": [
            {"name": "Demo headline: reverse engineering the PC2", "id": "t1",
             "pages": ["Demo content shipped with the BeoSound 5c emulator."]},
        ]},
    ]

    files["weather_forecast.json"] = {
        "updated": 1_700_000_000,
        "provider": "dmi",
        "location": {"name": "Aarhus, Denmark", "lat": "56.16", "lon": "10.20"},
        "current": {"temp_c": 14.2, "cloud_pct": 62, "wind_ms": 3.4},
        "today": {
            "date": "2024-11-14",
            "will_rain": True,
            "rain_mm": 2.3,
            "temp_min_c": 11.5,
            "temp_max_c": 15.8,
        },
        "hourly": [
            {"time": "14:00", "temp_c": 14.2, "rain_mm": 0.0, "cloud_pct": 55},
            {"time": "15:00", "temp_c": 14.6, "rain_mm": 0.0, "cloud_pct": 60},
            {"time": "16:00", "temp_c": 14.1, "rain_mm": 0.3, "cloud_pct": 78},
            {"time": "17:00", "temp_c": 13.4, "rain_mm": 1.1, "cloud_pct": 92},
            {"time": "18:00", "temp_c": 12.8, "rain_mm": 0.9, "cloud_pct": 88},
            {"time": "19:00", "temp_c": 12.1, "rain_mm": 0.0, "cloud_pct": 70},
        ],
    }

    # Dates are fixed rather than relative to today: `--check` compares the
    # committed files byte for byte, so a generator that moved with the
    # calendar would report itself stale every midnight. "Today" here is
    # Monday 14 Sep 2026 — the emulator only has to render, not be current.
    def event(summary, time="", end_time="", location="", calendar="",
              all_day=False, ongoing=False, until=None):
        item: dict[str, object] = {
            "summary": summary, "location": location,
            "calendar": calendar, "all_day": all_day, "time": time,
        }
        if not all_day:
            item["end_time"] = end_time
            item["ongoing"] = ongoing
        if until:
            item["until"] = until
        return item

    files["calendar_events.json"] = {
        "updated": 1_789_000_000,
        "timezone": "Europe/Copenhagen",
        "days_ahead": 14,
        "count": 8,
        "error": None,
        "days": [
            {"date": "2026-09-14", "is_today": True, "is_tomorrow": False, "events": [
                event("Morgenmøde", "09:00", "09:30", calendar="Work"),
                event("Tandlæge", "13:30", "14:15",
                      location="Hovedgaden 4", calendar="Family"),
                event("Korprøve", "19:00", "20:30",
                      location="Sognegården", calendar="Family"),
            ]},
            {"date": "2026-09-15", "is_today": False, "is_tomorrow": True, "events": [
                event("Ferie i Skagen", all_day=True, calendar="Family",
                      until="2026-09-18"),
                event("Kvartalsgennemgang", "10:00", "11:30", calendar="Work"),
            ]},
            {"date": "2026-09-17", "is_today": False, "is_tomorrow": False, "events": [
                event("Bilsyn", "08:15", "09:00",
                      location="Bilsyn Aarhus Nord", calendar="Family"),
            ]},
            {"date": "2026-09-20", "is_today": False, "is_tomorrow": False, "events": [
                event("Fars fødselsdag", all_day=True, calendar="Family"),
                event("Middag hos Anne og Lars", "18:00", "22:00",
                      location="Kirkevej 12", calendar="Family"),
            ]},
        ],
    }

    # No stick inserted — the empty browse envelope a device would return.
    files["usb_browse.json"] = {"path": "", "parent": None, "name": "USB", "items": []}

    def dr_article(art_id: str, title: str, color: str) -> dict:
        return {"id": art_id, "name": title, "icon": "article", "color": color,
                "page": {"title": title,
                         "body": "<p><em>Demo-indhold til BeoSound 5c-emulatoren.</em></p>"
                                 "<p>På en rigtig enhed står DR's artikeltekst her.</p>"}}

    files["dr_news_articles.json"] = [
        {"id": "sec-senestenyt", "name": "Seneste nyt", "icon": "lightning",
         "color": "#E74C3C", "articles": [
             dr_article("dr-s1", "Demo: Havnebussen sejler igen", "#E74C3C"),
             dr_article("dr-s2", "Demo: Nattoget kører til Berlin", "#E74C3C"),
         ]},
        {"id": "sec-indland", "name": "Indland", "icon": "house-line",
         "color": "#3498DB", "articles": [
             dr_article("dr-i1", "Demo: Struer fejrer gammel radio", "#3498DB"),
         ]},
        {"id": "sec-kultur", "name": "Kultur", "icon": "palette",
         "color": "#8E44AD", "articles": [
             dr_article("dr-k1", "Demo: Et flagskib fra 2009 genopbygget", "#8E44AD"),
         ]},
    ]

    files["config.json"] = {
        "device": "BeoSound 5c",
        "setup_complete": True,
        "_comment": "Demo config for the browser emulator and local dev — "
                    "generated by tools/gen-demo-data.py",
        "menu": {
            "PLAYING": "playing", "SPOTIFY": "spotify", "APPLE MUSIC": "apple_music",
            "TIDAL": "tidal", "PLEX": "plex", "JELLYFIN": "jellyfin",
            "RADIO": "radio",
            "SCENES": "scenes",
            "SECURITY": {"url": "softarc/security.html"},
            "SYSTEM": "system",
        },
        "scenes": SCENES,
        "cameras": [
            {"id": "door", "title": "Front door", "entity": "camera.demo_front_door"},
            {"id": "gate", "title": "Gate", "entity": "camera.demo_gate"},
        ],
        "player": {"type": "sonos", "ip": "192.168.1.100"},
        "volume": {"type": "sonos", "max": 70, "step": 3, "output_name": "Living Room"},
        "home_assistant": {"url": "http://homeassistant.local:8123"},
        "transport": {"mode": "webhook"},
    }

    return files


def main() -> int:
    check_only = "--check" in sys.argv
    files = build()
    OUT.mkdir(parents=True, exist_ok=True)

    stale = []
    for name, data in files.items():
        text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        path = OUT / name
        if check_only:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)

    if check_only:
        if stale:
            print("Stale demo data — run tools/gen-demo-data.py:", ", ".join(stale))
            return 1
        print(f"{len(files)} demo files up to date")
        return 0

    for name in sorted(files):
        print(f"  {name}  {(OUT / name).stat().st_size / 1024:.1f} KB")
    print(f"Wrote {len(files)} files to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
