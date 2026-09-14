/**
 * Calendar Source Preset — Google Calendar agenda (read-only)
 *
 * A single scrolling agenda, not an arc browser: fetches the calendar
 * service's /events endpoint and renders upcoming days. No iframe and no
 * ArcList — there is nothing to drill into and nothing to play, so the
 * wheel just scrolls the list and GO forces a refresh.
 *
 * All times arrive pre-formatted from the service, which owns the
 * timezone; the only formatting done here is naming weekdays and months,
 * via _calLang() — the device's language setting if one is configured,
 * otherwise the browser's own locale.
 */

const _CAL_SCROLL_STEP = 90;          // one wheel click ≈ one event row
const _CAL_REFRESH_MS = 10 * 60 * 1000;

let _calTimer = null;

// Weekday and month names come from the browser locale, so the words around
// them have to as well — otherwise a Danish device reads "TOMORROW · SØNDAG".
// "Today"/"Tomorrow" come from Intl and cover every locale; the rest has no
// Intl equivalent and falls back to English outside this table.
const _CAL_STRINGS = {
    en: { allDay: 'All day', until: 'until {date}', loading: 'Loading…',
          unavailable: 'Calendar unavailable — is the calendar source configured and running?',
          empty: 'Nothing scheduled in the next {n} days.' },
    da: { allDay: 'Hele dagen', until: 'til {date}', loading: 'Indlæser…',
          unavailable: 'Kalenderen er ikke tilgængelig — er kalenderkilden sat op, og kører den?',
          empty: 'Intet planlagt de næste {n} dage.' },
    sv: { allDay: 'Heldag', until: 'till {date}', loading: 'Laddar…',
          unavailable: 'Kalendern är inte tillgänglig — är kalenderkällan konfigurerad och igång?',
          empty: 'Inget inplanerat de närmaste {n} dagarna.' },
    nb: { allDay: 'Hele dagen', until: 'til {date}', loading: 'Laster…',
          unavailable: 'Kalenderen er ikke tilgjengelig — er kalenderkilden satt opp, og kjører den?',
          empty: 'Ingenting planlagt de neste {n} dagene.' },
    de: { allDay: 'Ganztägig', until: 'bis {date}', loading: 'Wird geladen…',
          unavailable: 'Kalender nicht verfügbar — ist die Kalenderquelle eingerichtet und aktiv?',
          empty: 'Nichts geplant in den nächsten {n} Tagen.' },
};

function _calLang() {
    // An explicit device setting (config.json's "language") wins over the
    // browser's own locale when set to anything but "auto"/unset. Used for
    // both our own string table (_calT) and every Intl call below — passing
    // Intl `undefined` reads the browser's raw default locale, which ignores
    // this override entirely (that's how weekday names and "Tomorrow" kept
    // showing up in English after switching the device setting to Danish).
    const override = window.AppConfig?.language;
    let lang = ((override && override !== 'auto') ? override : navigator.language || 'en').toLowerCase().split('-')[0];
    if (lang === 'no' || lang === 'nn') lang = 'nb';
    return lang;
}

function _calT(key, vars) {
    let text = (_CAL_STRINGS[_calLang()] || _CAL_STRINGS.en)[key];
    for (const [name, value] of Object.entries(vars || {})) {
        text = text.replace('{' + name + '}', () => value);
    }
    return text;
}

function _calRelativeDay(offset) {
    // numeric:'auto' gives the word ("i morgen"), not "om 1 dag".
    const word = new Intl.RelativeTimeFormat(_calLang(), { numeric: 'auto' })
        .format(offset, 'day');
    return word.charAt(0).toLocaleUpperCase() + word.slice(1);
}

function _calUrl() {
    return (window.AppConfig?.calendarServiceUrl || 'http://localhost:8791') + '/events';
}

function _calScroller() {
    return document.getElementById('calendar-scroll');
}

function _calEscape(text) {
    // Event titles come from a third party and land in innerHTML below.
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

async function _calFetchAndRender() {
    const root = document.getElementById('calendar-view');
    if (!root) return;
    try {
        const resp = await fetch(_calUrl());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        _calRender(root, await resp.json());
    } catch (e) {
        root.innerHTML = '<div class="cal-msg">' + _calEscape(_calT('unavailable')) + '</div>';
    }
}

function _calDayHeading(iso, isToday, isTomorrow) {
    // iso is a plain date: parsing it as UTC and formatting in UTC keeps the
    // weekday from drifting a day for viewers east or west of Greenwich.
    const date = new Date(iso + 'T00:00:00Z');
    const opts = { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' };
    let label = date.toLocaleDateString(_calLang(), opts);
    if (isToday) label = _calRelativeDay(0) + ' · ' + label;
    else if (isTomorrow) label = _calRelativeDay(1) + ' · ' + label;
    return label;
}

function _calUntilLabel(iso) {
    const date = new Date(iso + 'T00:00:00Z');
    return date.toLocaleDateString(_calLang(),
        { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

function _calEventRow(ev) {
    const meta = [];
    if (ev.location) meta.push(_calEscape(ev.location));
    if (ev.calendar) meta.push(_calEscape(ev.calendar));

    const when = ev.all_day
        ? _calEscape(_calT('allDay'))
        : _calEscape(ev.time) + (ev.end_time ? '<span class="cal-dash">–</span>'
            + _calEscape(ev.end_time) : '');

    const until = ev.until
        ? `<span class="cal-until">${_calEscape(_calT('until', { date: _calUntilLabel(ev.until) }))}</span>`
        : '';

    return `
        <div class="cal-event${ev.ongoing ? ' now' : ''}">
            <div class="cal-when">${when}</div>
            <div class="cal-what">
                <div class="cal-title">${_calEscape(ev.summary)}${until}</div>
                ${meta.length ? `<div class="cal-meta">${meta.join(' · ')}</div>` : ''}
            </div>
        </div>
    `;
}

function _calRender(root, data) {
    const days = (data && data.days) || [];
    if (!days.length) {
        const ahead = (data && data.days_ahead) || 14;
        root.innerHTML = '<div class="cal-msg">' + _calEscape(_calT('empty', { n: ahead })) + '</div>';
        return;
    }

    const banner = data.error
        ? `<div class="cal-warn">${_calEscape(data.error)}</div>`
        : '';

    const body = days.map(day => `
        <div class="cal-day">
            <div class="cal-date${day.is_today ? ' today' : ''}">
                ${_calEscape(_calDayHeading(day.date, day.is_today, day.is_tomorrow))}
            </div>
            ${(day.events || []).map(_calEventRow).join('')}
        </div>
    `).join('');

    // Preserve the reader's place across the background refresh.
    const previous = _calScroller()?.scrollTop || 0;
    root.innerHTML = banner + `<div id="calendar-scroll">${body}</div>`;
    const scroller = _calScroller();
    if (scroller) scroller.scrollTop = previous;
}

window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.calendar = {
    controller: {
        get isActive() { return true; },

        updateMetadata() {},

        handleNavEvent(data) {
            const scroller = _calScroller();
            if (!scroller) return false;
            const delta = data?.direction === 'clock' ? _CAL_SCROLL_STEP : -_CAL_SCROLL_STEP;
            scroller.scrollTop += delta;
            return true;
        },

        handleButton(button) {
            if (button === 'go') {
                _calFetchAndRender();
                return true;
            }
            return false;
        },
    },
    item: { title: 'CALENDAR', path: 'menu/calendar' },
    after: 'menu/news',
    view: {
        title: 'CALENDAR',
        content: `
            <style>
                #calendar-view { width:100%; height:100%; box-sizing:border-box; padding:34px 60px 24px; color:#fff; font-weight:300; overflow:hidden; display:flex; flex-direction:column; }
                #calendar-scroll { overflow-y:auto; scrollbar-width:none; -ms-overflow-style:none; }
                #calendar-scroll::-webkit-scrollbar { display:none; }
                #calendar-view .cal-day { margin-bottom:26px; }
                #calendar-view .cal-date { font-size:0.8rem; letter-spacing:2px; text-transform:uppercase; color:rgba(255,255,255,0.45); padding-bottom:8px; margin-bottom:10px; border-bottom:1px solid rgba(255,255,255,0.12); }
                #calendar-view .cal-date.today { color:#7ec8ff; }
                #calendar-view .cal-event { display:flex; gap:22px; padding:7px 0; align-items:baseline; }
                #calendar-view .cal-event.now { box-shadow:inset 2px 0 0 #7ec8ff; padding-left:14px; margin-left:-16px; }
                #calendar-view .cal-when { flex:0 0 118px; font-size:0.95rem; color:rgba(255,255,255,0.55); font-variant-numeric:tabular-nums; }
                #calendar-view .cal-event.now .cal-when { color:#7ec8ff; }
                #calendar-view .cal-dash { opacity:0.4; padding:0 3px; }
                #calendar-view .cal-what { min-width:0; }
                #calendar-view .cal-title { font-size:1.15rem; line-height:1.35; }
                #calendar-view .cal-until { font-size:0.8rem; color:rgba(255,255,255,0.45); margin-left:10px; }
                #calendar-view .cal-meta { font-size:0.82rem; color:rgba(255,255,255,0.45); margin-top:3px; }
                #calendar-view .cal-msg { color:rgba(255,255,255,0.5); font-size:0.95rem; }
                #calendar-view .cal-warn { color:#e8b45a; font-size:0.82rem; margin-bottom:14px; }
            </style>
            <div id="calendar-view"><div class="cal-msg">${_calEscape(_calT('loading'))}</div></div>
        `,
    },

    onAdd() {},

    onMount() {
        // Guard against a remount without an intervening onRemove, which
        // would otherwise leave the old interval polling forever.
        if (_calTimer) clearInterval(_calTimer);
        _calFetchAndRender();
        _calTimer = setInterval(_calFetchAndRender, _CAL_REFRESH_MS);
    },

    onRemove() {
        if (_calTimer) {
            clearInterval(_calTimer);
            _calTimer = null;
        }
    },
};
