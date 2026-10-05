# Radio Source (beo-source-radio)

Internet radio browser and player. Browses stations via the [Radio Browser API](https://www.radio-browser.info/), plays via the configured player service (Sonos, BlueSound, or local mpv).

**Port:** 8779

## Favourites

Favourites are stored as a JSON array, one file per device:

- **Production:** `/etc/beosound5c/radio_favourites.json`
- **Development:** `services/sources/radio/radio_favourites.json`

Digit buttons on the BeoRemote map directly to favourites: **1** plays the first favourite, **2** the second, etc. (up to 10, with **0** = 10th).

### File format

```json
[
  {
    "stationuuid": "960c660b-0601-11e8-ae97-52543be04c81",
    "name": "Sveriges Radio - P1",
    "url_resolved": "https://live1.sr.se/p1-aac-320",
    "favicon": "https://example.com/icon.png",
    "country": "Sweden",
    "tags": "news,public radio",
    "codec": "AAC",
    "bitrate": 312,
    "votes": 0
  }
]
```

**Required fields:** `stationuuid` (used as unique ID), `url_resolved` (stream URL), `name`.

The easiest way to find station UUIDs and stream URLs is to browse from the BS5 UI, or search the Radio Browser API directly:

```bash
# Search by name
curl 'https://de1.api.radio-browser.info/json/stations/byname/P1?limit=5' | python3 -m json.tool

# Get a specific station by UUID
curl 'https://de1.api.radio-browser.info/json/stations/byuuid/960c660b-0601-11e8-ae97-52543be04c81'
```

### Adding/removing via remote

- **RED button:** Toggle current station as favourite (add if not present, remove if present)
- **BLUE button:** Remove current station from favourites

## Sveriges Radio Now-Playing

For the four curated Sveriges Radio channels (P1, P2, P3, P4 Plus), the service polls the [SR API](https://api.sr.se/) every 60 seconds for current program metadata. When an SR station is playing, the PLAYING view shows:

- **Title:** `P1: Ekot nyhetssändning` (channel + episode title)
- **Artist:** Program name (e.g. "Ekot nyhetssändning")
- **Artwork:** Program-specific image from SR, falling back to the high-res channel logo

When a program changes while listening, the metadata and artwork update automatically.

### Mapped SR channels

| Channel | SR API ID | Radio Browser UUID |
|---------|----------|--------------------|
| P1 | 132 | `960c660b-0601-11e8-ae97-52543be04c81` |
| P2 | 163 | `960c62c1-0601-11e8-ae97-52543be04c81` |
| P3 | 164 | `960c4e01-0601-11e8-ae97-52543be04c81` |
| P4 Plus | 4951 | `962c1da9-0601-11e8-ae97-52543be04c81` |

To add more SR channels, add entries to `SR_CHANNEL_MAP` in `service.py`. Find channel IDs at `https://api.sr.se/api/v2/channels?format=json`.

## Language

Everything the source names itself is available in Danish: the root
categories, the heading of each browse level, and the country and genre
words in station subtitles and in the PLAYING view. The strings and lookup
tables live in `radio_i18n.py`.

The language comes from `config.json`'s top-level `"language"`, the same
setting the arc menu labels read. `"auto"` (the default) means English here:
a service has no browser locale to follow the way the web views do.

What is *not* translated, by design:

- **Station names** — they are the station's own name.
- **Browse paths and ids** (`countries/Germany`, `genres/rock`) — the id is
  the contract the UI drills down on and the integration tests assert
  against. Only the label is translated.
- **Genres the Radio Browser API carries in another language.** Its tag list
  is free text and its top entries are largely Spanish (`entretenimiento`,
  `música regional mexicana`); a station's own self-description is left as
  it was written. The same goes for the junk entries in the language list.
- **Demo mode.** `web/json/demo/radio_browse.json` is a static fixture served
  without going through the service, so it stays English whatever the
  language is.

A country, genre or language the tables don't cover falls back to the word
as the API spelled it, so a new entry upstream shows up untranslated rather
than disappearing. `COUNTRIES_DA` covers every country the API currently
returns.

## Curated Lists

The Swedish and Danish browse categories show hand-picked stations (best stream quality per station). Edit `CURATED_SVERIGE` / `CURATED_DANMARK` in `service.py` to customize.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /browse?path=` | Browse categories and stations |
| `GET /favicon?url=` | Proxy + cache external favicons |
| `GET /sr-artwork?uuid=` | SR program artwork (or channel logo fallback) |
| `POST /command` | Playback commands (via router or direct) |
| `GET /status` | Current playback state |
| `GET /resync` | Re-register with router and re-broadcast metadata |
