/**
 * Dashboard Source Preset (OVERBLIK in Danish) — one page of tiles
 *
 * A single info page, not an arc browser, like WEATHER: the clock and date
 * come from the browser, everything else from beo-source-dashboard's
 * /api/dashboard (port 8795) — today's and tomorrow's electricity price hour
 * by hour (from stromligning.dk), what is playing and how the device is doing.
 * Nothing to drill into, so no iframe and no ArcList.
 *
 * Strings follow AppConfig.language unless it is "auto", then the browser's
 * locale; Danish and English, falling back to English.
 */

let _dashClockTimer = null;
let _dashDataTimer = null;

const DASH_REFRESH_MS = 10 * 1000;

const _DASH_STRINGS = {
    en: { playing: 'Now playing', paused: 'Paused', idle: 'Nothing is playing',
          device: 'Device', uptime: 'Uptime', temp: 'CPU temperature', load: 'Load',
          memory: 'Memory', disk: 'Disk', ip: 'Address', volume: 'Volume',
          freeOf: '{free} GB free of {total} GB',
          unavailable: 'Dashboard unavailable — is beo-source-dashboard running?',
          days: '{n} d', hours: '{n} h', minutes: '{n} min',
          elNow: 'Electricity now', today: 'Today', tomorrow: 'Tomorrow', forecast: 'forecast',
          elSummary: 'avg. {avg} · low {low} at {lowAt} · high {high} at {highAt}',
          elCredit: 'Prices incl. tariffs, taxes and VAT · {supplier} · {area} · stromligning.dk' },
    da: { playing: 'Spiller nu', paused: 'Sat på pause', idle: 'Der spilles ikke noget',
          device: 'Enhed', uptime: 'Oppetid', temp: 'CPU-temperatur', load: 'Belastning',
          memory: 'Hukommelse', disk: 'Disk', ip: 'Adresse', volume: 'Lydstyrke',
          freeOf: '{free} GB fri af {total} GB',
          unavailable: 'Overblikket er ikke tilgængeligt — kører beo-source-dashboard?',
          days: '{n} d', hours: '{n} t', minutes: '{n} min',
          elNow: 'Elpris nu', today: 'I dag', tomorrow: 'I morgen', forecast: 'prognose',
          elSummary: 'gns. {avg} · lavest {low} kl. {lowAt} · højest {high} kl. {highAt}',
          elCredit: 'Priser inkl. nettarif, afgifter og moms · {supplier} · {area} · stromligning.dk' },
};

function _dashLang() {
    const override = window.AppConfig?.language;
    const lang = ((override && override !== 'auto') ? override : navigator.language || 'en')
        .toLowerCase().split('-')[0];
    return _DASH_STRINGS[lang] ? lang : 'en';
}

function _dashT(key, vars) {
    let text = _DASH_STRINGS[_dashLang()][key];
    for (const [name, value] of Object.entries(vars || {})) {
        text = text.replace('{' + name + '}', () => value);
    }
    return text;
}

function _dashEsc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

function _dashNum(value, digits) {
    return value.toLocaleString(_dashLang(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function _dashUrl() {
    return (window.AppConfig?.dashboardServiceUrl || 'http://localhost:8795') + '/api/dashboard';
}

function _dashUptime(seconds) {
    if (seconds == null) return '–';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const parts = [];
    if (d) parts.push(_dashT('days', { n: d }));
    if (d || h) parts.push(_dashT('hours', { n: h }));
    if (!d) parts.push(_dashT('minutes', { n: m }));
    return parts.join(' ');
}

function _dashRenderClock() {
    const timeEl = document.getElementById('dash-time');
    const dateEl = document.getElementById('dash-date');
    if (!timeEl || !dateEl) return;
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    timeEl.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const date = now.toLocaleDateString(_dashLang(), { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
    dateEl.textContent = date.charAt(0).toUpperCase() + date.slice(1);
}

function _dashRow(label, value) {
    return `<div class="dash-row"><span>${_dashEsc(label)}</span><b>${_dashEsc(value)}</b></div>`;
}

function _dashKr(value) {
    return `${_dashNum(value, 2)} kr`;
}

function _dashHourLabel(h) {
    return String(h).padStart(2, '0');
}

// One column of bars per day, hour by hour; the bar height is the price on a
// scale shared by both days. Hours already gone are dimmed, the current hour
// is the bright one and carries the only number on the chart, and forecast
// hours are hatched — the same "prognose" stromligning.dk marks tomorrow with
// until the real prices are published around 13:00.
function _dashRenderElectricity(el) {
    const box = document.getElementById('dash-electricity');
    if (!box) return;
    if (!el || !el.days || !el.days.length) {
        box.style.display = 'none';
        return;
    }
    box.style.display = '';

    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    const todayKey = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    const all = el.days.flatMap(d => d.hours.map(h => h.price));
    const top = Math.max(...all, 0.01);

    const today = el.days.find(d => d.date === todayKey);
    const current = today && today.hours.find(h => h.hour === now.getHours());
    let headline = '';
    if (current) {
        const prices = today.hours.map(h => h.price);
        const low = today.hours.reduce((a, b) => (b.price < a.price ? b : a));
        const high = today.hours.reduce((a, b) => (b.price > a.price ? b : a));
        headline = `
            <div class="dash-el-now"><span class="dash-el-big">${_dashEsc(_dashNum(current.price, 2))}</span> kr/kWh</div>
            <div class="dash-sub">${_dashEsc(_dashT('elSummary', {
                avg: _dashKr(prices.reduce((a, b) => a + b, 0) / prices.length),
                low: _dashKr(low.price), lowAt: _dashHourLabel(low.hour),
                high: _dashKr(high.price), highAt: _dashHourLabel(high.hour),
            }))}</div>`;
    }

    const days = el.days.map(day => {
        const isToday = day.date === todayKey;
        const forecast = day.hours.some(h => h.forecast);
        const bars = day.hours.map(h => {
            const cls = ['dash-bar'];
            if (h.forecast) cls.push('fc');
            if (isToday && h.hour < now.getHours()) cls.push('past');
            const isNow = isToday && h.hour === now.getHours();
            if (isNow) cls.push('now');
            const pct = Math.max(2, Math.round(100 * h.price / top));
            return `<div class="${cls.join(' ')}" style="height:${pct}%">${
                isNow ? `<span>${_dashEsc(_dashNum(h.price, 2))}</span>` : ''}</div>`;
        }).join('');
        const ticks = [0, 6, 12, 18].map(h => `<span>${_dashHourLabel(h)}</span>`).join('');
        const title = isToday ? _dashT('today') : _dashT('tomorrow');
        return `
            <div class="dash-day">
                <div class="dash-day-title">${_dashEsc(title)}${forecast ? ` <em>${_dashEsc(_dashT('forecast'))}</em>` : ''}</div>
                <div class="dash-bars">${bars}</div>
                <div class="dash-ticks">${ticks}</div>
            </div>`;
    }).join('');

    box.innerHTML = `
        <div class="dash-label"><i class="ph ph-lightning"></i> ${_dashEsc(_dashT('elNow'))}</div>
        ${headline}
        <div class="dash-days">${days}</div>
        <div class="dash-credit">${_dashEsc(_dashT('elCredit', { supplier: el.supplier, area: el.area }))}</div>`;
}

function _dashRender(data) {
    _dashRenderElectricity(data.electricity);

    const playingEl = document.getElementById('dash-playing');
    const deviceEl = document.getElementById('dash-device');
    if (!playingEl || !deviceEl) return;

    const p = data.playing;
    if (p) {
        const sub = [p.artist, p.album].filter(Boolean).join(' · ');
        playingEl.innerHTML = `
            <div class="dash-label"><i class="ph ${p.state === 'paused' ? 'ph-pause' : 'ph-play'}"></i>
                ${_dashEsc(_dashT(p.state === 'paused' ? 'paused' : 'playing'))}${p.source ? ' · ' + _dashEsc(p.source) : ''}</div>
            <div class="dash-title">${_dashEsc(p.title)}</div>
            ${sub ? `<div class="dash-sub">${_dashEsc(sub)}</div>` : ''}
            ${data.volume != null ? `<div class="dash-sub">${_dashEsc(_dashT('volume'))} ${data.volume}${data.output ? ' · ' + _dashEsc(data.output) : ''}</div>` : ''}`;
    } else {
        playingEl.innerHTML = `
            <div class="dash-label"><i class="ph ph-speaker-simple-none"></i> ${_dashEsc(_dashT('playing'))}</div>
            <div class="dash-sub">${_dashEsc(_dashT('idle'))}</div>`;
    }

    const rows = [
        _dashRow(_dashT('uptime'), _dashUptime(data.uptime_s)),
        _dashRow(_dashT('temp'), data.cpu_temp_c != null ? `${_dashNum(data.cpu_temp_c, 1)} °C` : '–'),
        _dashRow(_dashT('load'), data.load ? data.load.map(v => _dashNum(v, 2)).join('  ') : '–'),
        _dashRow(_dashT('memory'), data.memory ? `${data.memory.used_pct} %` : '–'),
        _dashRow(_dashT('disk'), data.disk ? _dashT('freeOf', { free: _dashNum(data.disk.free_gb, 1), total: _dashNum(data.disk.total_gb, 1) }) : '–'),
        _dashRow(_dashT('ip'), data.ip || '–'),
    ];
    deviceEl.innerHTML = `
        <div class="dash-label"><i class="ph ph-cpu"></i> ${_dashEsc(data.device || _dashT('device'))}</div>
        ${rows.join('')}`;
}

async function _dashFetchAndRender() {
    if (!document.getElementById('dash-device')) return;
    try {
        const resp = await fetch(_dashUrl());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        _dashRender(await resp.json());
    } catch (e) {
        const el = document.getElementById('dash-device');
        if (el) el.innerHTML = `<div class="dash-sub">${_dashEsc(_dashT('unavailable'))}</div>`;
    }
}

function _dashStop() {
    if (_dashClockTimer) clearInterval(_dashClockTimer);
    if (_dashDataTimer) clearInterval(_dashDataTimer);
    _dashClockTimer = _dashDataTimer = null;
}

window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.dashboard = {
    controller: {
        get isActive() { return true; },
        updateMetadata() {},
        handleNavEvent() { return false; },
        handleButton() { return false; },
    },
    item: { title: 'DASHBOARD', path: 'menu/dashboard' },
    after: 'menu/playing',
    view: {
        title: 'DASHBOARD',
        content: `
            <style>
                #dashboard-view { width:100%; height:100%; box-sizing:border-box; padding:40px 60px; color:#fff; font-weight:300; overflow-y:auto; }
                #dashboard-view .dash-clock { margin-bottom:22px; }
                #dashboard-view #dash-time { font-size:3.6rem; font-weight:200; letter-spacing:2px; line-height:1; }
                #dashboard-view #dash-date { font-size:1.1rem; color:rgba(255,255,255,0.6); margin-top:8px; letter-spacing:0.5px; }
                #dashboard-view .dash-tiles { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
                #dashboard-view .dash-tile { background:rgba(255,255,255,0.06); border-radius:10px; padding:18px 22px; min-width:0; }
                #dashboard-view .dash-label { font-size:0.8rem; letter-spacing:1px; color:rgba(255,255,255,0.45); text-transform:uppercase; margin-bottom:12px; }
                #dashboard-view .dash-label .ph { margin-right:4px; }
                #dashboard-view .dash-title { font-size:1.5rem; font-weight:300; margin-bottom:6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
                #dashboard-view .dash-sub { font-size:0.95rem; color:rgba(255,255,255,0.6); line-height:1.5; overflow:hidden; text-overflow:ellipsis; }
                #dashboard-view .dash-row { display:flex; justify-content:space-between; gap:12px; font-size:0.9rem; padding:4px 0; color:rgba(255,255,255,0.55); }
                #dashboard-view #dash-electricity { margin-bottom:20px; }
                #dashboard-view .dash-el-now { font-size:1rem; color:rgba(255,255,255,0.6); }
                #dashboard-view .dash-el-big { font-size:2.2rem; font-weight:200; color:#fff; }
                #dashboard-view .dash-days { display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-top:14px; }
                #dashboard-view .dash-day-title { font-size:0.8rem; letter-spacing:0.5px; color:rgba(255,255,255,0.55); margin-bottom:6px; }
                #dashboard-view .dash-day-title em { font-style:normal; color:rgba(255,255,255,0.35); }
                #dashboard-view .dash-bars { display:flex; align-items:flex-end; gap:2px; height:90px; padding-top:18px; border-bottom:1px solid rgba(255,255,255,0.15); }
                #dashboard-view .dash-bar { position:relative; flex:1; background:#7ec8ff; border-radius:4px 4px 0 0; opacity:0.85; }
                #dashboard-view .dash-bar.past { opacity:0.3; }
                #dashboard-view .dash-bar.fc { opacity:0.6; background:repeating-linear-gradient(45deg, #7ec8ff 0 3px, rgba(126,200,255,0.35) 3px 6px); }
                #dashboard-view .dash-bar.now { opacity:1; background:#fff; }
                #dashboard-view .dash-bar span { position:absolute; bottom:100%; left:50%; transform:translateX(-50%); margin-bottom:3px; font-size:0.75rem; color:#fff; white-space:nowrap; }
                #dashboard-view .dash-ticks { display:flex; justify-content:space-between; font-size:0.7rem; color:rgba(255,255,255,0.4); margin-top:4px; }
                #dashboard-view .dash-ticks span { width:25%; }
                #dashboard-view .dash-credit { font-size:0.7rem; letter-spacing:0.3px; color:rgba(255,255,255,0.35); margin-top:10px; }
                #dashboard-view .dash-row b { font-weight:400; color:rgba(255,255,255,0.9); text-align:right; }
            </style>
            <div id="dashboard-view">
                <div class="dash-clock"><div id="dash-time"></div><div id="dash-date"></div></div>
                <div class="dash-tile" id="dash-electricity" style="display:none"></div>
                <div class="dash-tiles">
                    <div class="dash-tile" id="dash-playing"></div>
                    <div class="dash-tile" id="dash-device"></div>
                </div>
            </div>
        `,
    },

    onAdd() {},

    onMount() {
        _dashStop();
        _dashRenderClock();
        _dashFetchAndRender();
        _dashClockTimer = setInterval(_dashRenderClock, 1000);
        _dashDataTimer = setInterval(_dashFetchAndRender, DASH_REFRESH_MS);
    },

    onRemove() {
        _dashStop();
    },
};
