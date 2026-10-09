# Arc lists

The wheel-driven lists in `web/softarc/` are built on two different
implementations. Both draw the same arc; they do not agree on what the
buttons mean, and they differ in how much state they keep.

| | `script.js` (v1) | `script-v2.js` (v2) |
|---|---|---|
| Pages | apple_music, jellyfin, plex, spotify, tidal | demo, news, radio, scenes, usb |
| Data | the whole tree up front (`parent_child`) | a level at a time, via `childrenLoader` |
| Open a folder | **LEFT** (`enterChildView`) | **GO** (`drillDown`) |
| Back out | **RIGHT** (`exitChildView`) | **RIGHT** / `goBack` |
| GO on a folder | plays it | opens it |
| GO on a leaf | plays it | plays it |

## The button difference is the one that bites

In Jellyfin, GO on an album **plays the album** — opening it to see the
tracks is LEFT. In Radio, GO on a category **opens it**, and only GO on a
station plays. Someone who learns one list guesses wrong in the other.

Neither is wrong on its own: v1 lists are playlists and albums, where
"play the whole thing" is the common intent, and the v1 arc has no notion
of a folder you would only browse. v2 lists are hierarchies — countries,
genres, directories — where most entries cannot be played at all.

**Open question, parked 2026-10-09 (device owner's call):** should GO open
an album in the v1 lists, the way it opens a category in v2? It would make
the two consistent, but it takes the one-press "play this album" away, and
`script.js` is shared by five sources — so the change belongs in each
page's config rather than in the shared script. Nothing has been changed;
this is only written down.

## State across a view change

A preloaded iframe does **not** survive a route change. `view-manager`
moves it to a holding container before replacing the content area and back
again on the way in, and re-parenting an iframe discards its browsing
context, so the document reloads. `revive()` reads as though the document
lived through it; it does not — the ArcList that revives is a new one,
rebuilt from `localStorage`.

That makes `restoreState()` the only thing standing between the user and
the root of the list. v1 restores by walking the tree it already holds. v2
cannot do that for a `childrenLoader` list, because the levels live in a
service — so `_restoreDeepState()` replays the saved path asynchronously
after the first paint, re-fetching each level by its id. Radio and USB
depend on it; without it, picking a station dropped you back at the root.

The reloading iframe itself is untouched: every entry into such a view
re-runs the page and re-fetches every level. Leaving the iframe in place
and hiding it would remove that work, but it changes behaviour for every
preloaded view at once.
