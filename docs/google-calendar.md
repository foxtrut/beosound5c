# Google Calendar Source

A read-only agenda of your upcoming events, shown as a `CALENDAR` menu item. It displays only — the BS5c never writes to your calendar, so there is no way to create, move or delete an event from the device.

That read-only scope is why this uses a calendar's **iCal URL** rather than the Google Calendar API: there is no Google Cloud project to create, no OAuth client, no consent screen to complete on a device with no keyboard, and no tokens that expire and need refreshing.

## Getting the URL

In Google Calendar on a computer:

1. **Settings** (the gear, top right) → **Settings**
2. In the left sidebar under **Settings for my calendars**, click the calendar you want
3. Scroll to **Integrate calendar**
4. Copy **Secret address in iCal format** — it ends in `.ics`

> **Treat this URL as a password.** Anyone who has it can read that calendar's events without signing in. If it leaks, click **Reset** next to it in the same panel; the old URL stops working immediately.
>
> The URL is stored in `/etc/beosound5c/config.json`, which the on-device config page serves to your local network — the same as the other source credentials. Keep that in mind on a shared or untrusted network.

Use **Secret address**, not "Public address" — the public one only works if you have made the whole calendar public.

## Configuring the device

Open `http://<device-ip>/config`, go to **Sources → Calendar**, paste the URL into **iCal URL**, and save. The service restarts on its own.

Or edit `/etc/beosound5c/config.json` directly:

```json
"menu": {
  "CALENDAR": "calendar"
},
"calendar": {
  "url": "https://calendar.google.com/calendar/ical/…/basic.ics",
  "days_ahead": 14
}
```

`CALENDAR` has to be in `menu` for the item to appear. Without a URL, `beo-source-calendar` logs a warning and stops rather than showing an empty page.

### Several calendars

Merged into one agenda, each event labelled with its calendar's name:

```json
"calendar": {
  "calendars": [
    { "name": "Family", "url": "https://calendar.google.com/calendar/ical/…/basic.ics" },
    { "name": "Work",   "url": "https://calendar.google.com/calendar/ical/…/basic.ics" }
  ]
}
```

Each calendar needs its own secret URL, fetched from that calendar's own settings page. A shared calendar someone else owns works too, as long as it is visible in your Google Calendar and you copy the URL from its settings.

### Other settings

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | — | One calendar's secret iCal URL |
| `calendars` | — | Several calendars, as above |
| `days_ahead` | `14` | How far ahead the agenda reaches (1–365) |
| `timezone` | device timezone | IANA zone to render times in, e.g. `Europe/Copenhagen` |

## On the device

The wheel scrolls the agenda; **GO** refreshes immediately. Days are grouped with today first, all-day events listed above timed ones, and an event currently under way marked in blue.

The service re-fetches every 15 minutes, and the view every 10, so a change made in Google Calendar shows up within roughly a quarter of an hour. Google also caches its own iCal export, which can add a delay of its own — the secret iCal feed is not instant, and an event added seconds ago may take a few minutes to appear. Press **GO** to force a fetch.

Recurring events, single instances edited out of a series, cancellations, and multi-day and all-day events are all handled. If a calendar cannot be reached, the last agenda stays on screen rather than being replaced by an empty one.

## Troubleshooting

```bash
systemctl status beo-source-calendar
journalctl -u beo-source-calendar -n 50
```

**The menu item is missing** — `CALENDAR` is not in `menu`, or the service stopped for lack of a URL. The log says which.

**"Calendar unavailable"** — the frontend cannot reach the service on port 8791; check that it is running.

**Empty agenda** — nothing is scheduled within `days_ahead`, or the URL points at a calendar with no upcoming events. Check what Google actually returns:

```bash
curl -sL "<your ical url>" | head -40
```

**Events an hour off** — the times come from the zone in `calendar.timezone`, falling back to the device's own. Check `timedatectl`.
