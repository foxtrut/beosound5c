/**
 * Weather Source Preset — DMI (Danish Meteorological Institute) forecast
 *
 * A single info page, not an arc browser: fetches the weather service's
 * /forecast endpoint directly and renders today's conditions + an hourly
 * rain outlook, plus a live precipitation radar map centered on the
 * configured location. No iframe, no ArcList — there is nothing to drill
 * into.
 *
 * Radar tiles come from RainViewer (rainviewer.com) — free, no API key,
 * global coverage, ~10 min update cadence. DMI's own radar product is
 * raw HDF5 composites (needs an API key + server-side reprojection to
 * turn into map tiles), not something worth building for this display.
 *
 * UI strings follow the browser locale, same convention as the calendar
 * view: a small table for da/sv/nb/de, falling back to English. Numbers
 * (°, mm, m/s) and the hourly HH:MM times are locale-neutral already.
 */

let _weatherTimer = null;
let _weatherMap = null;
let _weatherRadarLayer = null;
let _weatherMarker = null;
let _leafletLoadPromise = null;
let _weatherRadarFrames = [];   // RainViewer "past" frames, oldest first
let _weatherRadarHost = '';
let _weatherRadarFrameIdx = 0;
let _weatherRadarAnimTimer = null;

const RAINVIEWER_INDEX_URL = 'https://api.rainviewer.com/public/weather-maps.json';
// RainViewer publishes one frame every 10 min (its own cadence, not ours) —
// ~13 past frames covers the last ~2 hours, same window DMI's own radar
// loop shows. RADAR_FRAME_MS is purely how fast we flip through them on
// screen, independent of that 10 min data interval.
const RADAR_FRAME_MS = 600;
const RADAR_LOOP_PAUSE_MS = 2000;

const _WX_STRINGS = {
    en: { loading: 'Loading…',
          unavailable: 'Weather unavailable — is the weather source configured and running?',
          noData: 'No forecast data yet.',
          rainToday: 'Rain expected today — ~{amount} mm',
          rainTodayNoAmount: 'Rain expected today',
          noRainToday: 'No rain expected today',
          cloud: '{pct}% cloud', wind: '{speed} m/s wind',
          radarUnavailable: 'Radar unavailable.',
          radarLabel: 'Precipitation radar — last 2h, looping',
          now: 'Now' },
    da: { loading: 'Indlæser…',
          unavailable: 'Vejret er ikke tilgængeligt — er vejrkilden sat op, og kører den?',
          noData: 'Ingen prognose endnu.',
          rainToday: 'Regn ventet i dag — ca. {amount} mm',
          rainTodayNoAmount: 'Regn ventet i dag',
          noRainToday: 'Ingen regn ventet i dag',
          cloud: '{pct}% skydække', wind: '{speed} m/s vind',
          radarUnavailable: 'Radar ikke tilgængelig.',
          radarLabel: 'Nedbørsradar — sidste 2 timer, i loop',
          now: 'Nu' },
    sv: { loading: 'Laddar…',
          unavailable: 'Vädret är inte tillgängligt — är väderkällan konfigurerad och igång?',
          noData: 'Ingen prognos ännu.',
          rainToday: 'Regn väntas i dag — ca. {amount} mm',
          rainTodayNoAmount: 'Regn väntas i dag',
          noRainToday: 'Inget regn väntas i dag',
          cloud: '{pct}% molntäcke', wind: '{speed} m/s vind',
          radarUnavailable: 'Radar inte tillgänglig.',
          radarLabel: 'Nederbördsradar — senaste 2 timmarna, i loop',
          now: 'Nu' },
    nb: { loading: 'Laster…',
          unavailable: 'Været er ikke tilgjengelig — er værkilden satt opp, og kjører den?',
          noData: 'Ingen prognose ennå.',
          rainToday: 'Regn ventet i dag — ca. {amount} mm',
          rainTodayNoAmount: 'Regn ventet i dag',
          noRainToday: 'Ingen regn ventet i dag',
          cloud: '{pct}% skydekke', wind: '{speed} m/s vind',
          radarUnavailable: 'Radar ikke tilgjengelig.',
          radarLabel: 'Nedbørsradar — siste 2 timer, i loop',
          now: 'Nå' },
    de: { loading: 'Wird geladen…',
          unavailable: 'Wetter nicht verfügbar — ist die Wetterquelle eingerichtet und aktiv?',
          noData: 'Noch keine Vorhersage.',
          rainToday: 'Regen heute erwartet — ca. {amount} mm',
          rainTodayNoAmount: 'Regen heute erwartet',
          noRainToday: 'Kein Regen heute erwartet',
          cloud: '{pct}% Bewölkung', wind: '{speed} m/s Wind',
          radarUnavailable: 'Radar nicht verfügbar.',
          radarLabel: 'Niederschlagsradar — letzte 2 Std., in Schleife',
          now: 'Jetzt' },
};

function _wxT(key, vars) {
    // An explicit device setting (config.json's "language") wins over the
    // browser's own locale when set to anything but "auto"/unset.
    const override = window.AppConfig?.language;
    let lang = ((override && override !== 'auto') ? override : navigator.language || 'en').toLowerCase().split('-')[0];
    if (lang === 'no' || lang === 'nn') lang = 'nb';
    let text = (_WX_STRINGS[lang] || _WX_STRINGS.en)[key];
    for (const [name, value] of Object.entries(vars || {})) {
        text = text.replace('{' + name + '}', () => value);
    }
    return text;
}

function _weatherUrl() {
    return (window.AppConfig?.weatherServiceUrl || 'http://localhost:8790') + '/forecast';
}

function _weatherLocationLabel(location) {
    if (!location) return '';
    if (location.name) return location.name;
    const lat = parseFloat(location.lat);
    const lon = parseFloat(location.lon);
    if (Number.isNaN(lat) || Number.isNaN(lon)) return '';
    return `${lat.toFixed(2)}°N, ${lon.toFixed(2)}°E`;
}

function _weatherIconClass(cloudPct, raining) {
    if (raining) return 'ph-cloud-rain';
    if (cloudPct == null) return 'ph-cloud';
    if (cloudPct < 20) return 'ph-sun';
    if (cloudPct < 70) return 'ph-cloud-sun';
    return 'ph-cloud';
}

function _weatherLoadLeaflet() {
    if (window.L) return Promise.resolve();
    if (_leafletLoadPromise) return _leafletLoadPromise;
    _leafletLoadPromise = new Promise((resolve, reject) => {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = 'assets/leaflet/leaflet.css';
        document.head.appendChild(link);

        const script = document.createElement('script');
        script.src = 'assets/leaflet/leaflet.js';
        script.onload = () => resolve();
        script.onerror = () => reject(new Error('Failed to load Leaflet'));
        document.head.appendChild(script);
    });
    return _leafletLoadPromise;
}

async function _weatherFetchAndRender() {
    const textEl = document.getElementById('weather-text');
    if (!textEl) return;
    try {
        const resp = await fetch(_weatherUrl());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        _weatherRender(data);
        _weatherUpdateRadar(data.location);
    } catch (e) {
        textEl.innerHTML = '<div class="wx-error">' + _wxT('unavailable') + '</div>';
    }
}

function _weatherRender(data) {
    const textEl = document.getElementById('weather-text');
    if (!textEl) return;
    if (!data || !data.current || !data.today) {
        textEl.innerHTML = '<div class="wx-error">' + _wxT('noData') + '</div>';
        return;
    }
    const cur = data.current;
    const today = data.today;
    const hours = data.hourly || [];

    const rainLine = today.will_rain
        ? (today.rain_mm != null ? _wxT('rainToday', { amount: today.rain_mm }) : _wxT('rainTodayNoAmount'))
        : _wxT('noRainToday');

    const hourly = hours.slice(0, 10).map(h => {
        const raining = h.rain_mm != null && h.rain_mm > 0;
        return `
        <div class="wx-hour">
            <div class="t">${h.time}</div>
            <i class="ph ${_weatherIconClass(h.cloud_pct, raining)}"></i>
            <div>${h.temp_c != null ? Math.round(h.temp_c) + '°' : '–'}</div>
            <div class="r">${raining ? h.rain_mm.toFixed(1) + 'mm' : ''}</div>
        </div>
    `;
    }).join('');

    const range = (today.temp_min_c != null && today.temp_max_c != null)
        ? `${Math.round(today.temp_min_c)}° / ${Math.round(today.temp_max_c)}°`
        : '';
    const details = [
        cur.cloud_pct != null ? _wxT('cloud', { pct: cur.cloud_pct }) : '',
        cur.wind_ms != null ? _wxT('wind', { speed: Math.round(cur.wind_ms) }) : '',
    ].filter(Boolean).join(' · ');

    const nowRaining = hours.length > 0 && hours[0].rain_mm != null && hours[0].rain_mm > 0;
    const nowIcon = _weatherIconClass(cur.cloud_pct, nowRaining);
    const locLabel = _weatherLocationLabel(data.location);
    // Open-Meteo's data is CC BY 4.0 — the credit is required, not decoration.
    const credit = data.provider === 'open_meteo'
        ? 'Weather data by Open-Meteo.com (DMI model)'
        : 'Weather data by DMI';

    textEl.innerHTML = `
        ${locLabel ? `<div class="wx-loc">${locLabel}</div>` : ''}
        <div class="wx-now">
            <i class="ph ${nowIcon} wx-icon"></i>
            <div class="wx-temp">${cur.temp_c != null ? Math.round(cur.temp_c) + '°' : '–'}</div>
            <div class="wx-sub">${range}<br>${details}</div>
        </div>
        <div class="wx-rain ${today.will_rain ? 'yes' : ''}">${rainLine}</div>
        <div class="wx-hourly">${hourly}</div>
        <div class="wx-credit">${credit}</div>
    `;
}

async function _weatherUpdateRadar(location) {
    const mapEl = document.getElementById('wx-radar-map');
    if (!mapEl || !location) return;
    const lat = parseFloat(location.lat);
    const lon = parseFloat(location.lon);
    if (Number.isNaN(lat) || Number.isNaN(lon)) return;

    try {
        await _weatherLoadLeaflet();
    } catch (e) {
        mapEl.innerHTML = '<div class="wx-error">' + _wxT('radarUnavailable') + '</div>';
        return;
    }
    if (!mapEl.isConnected) return; // view was torn down while Leaflet was loading

    if (!_weatherMap) {
        _weatherMap = L.map(mapEl, {
            zoomControl: false,
            dragging: false,
            scrollWheelZoom: false,
            doubleClickZoom: false,
            boxZoom: false,
            keyboard: false,
            tap: false,
        }).setView([lat, lon], 7);

        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; OpenStreetMap',
            maxZoom: 12,
        }).addTo(_weatherMap);

        _weatherMarker = L.circleMarker([lat, lon], {
            radius: 6, weight: 2, color: '#7ec8ff', fillColor: '#7ec8ff', fillOpacity: 0.9,
        }).addTo(_weatherMap);
    } else {
        _weatherMap.setView([lat, lon]);
        _weatherMarker.setLatLng([lat, lon]);
    }

    try {
        const resp = await fetch(RAINVIEWER_INDEX_URL);
        const idx = await resp.json();
        const frames = idx?.radar?.past || [];
        if (!frames.length || !_weatherMap) return;

        const isFirstLoad = _weatherRadarFrames.length === 0;
        _weatherRadarHost = idx.host;
        _weatherRadarFrames = frames;
        if (!_weatherRadarLayer) {
            _weatherRadarLayer = L.tileLayer('', {
                attribution: 'Radar &copy; <a href="https://www.rainviewer.com">RainViewer</a>',
                opacity: 0.75,
            }).addTo(_weatherMap);
        }
        if (isFirstLoad) {
            // Start the loop on "now" so the first thing shown is current,
            // then it plays -2h -> now -> pause -> repeat.
            _weatherRadarFrameIdx = _weatherRadarFrames.length - 1;
            _weatherShowRadarFrame();
            _weatherScheduleNextRadarFrame();
        }
    } catch (e) {
        // Transient RainViewer failure — keep animating the last known frames.
    }
}

function _weatherShowRadarFrame() {
    if (!_weatherRadarLayer || !_weatherRadarFrames.length) return;
    const idx = _weatherRadarFrameIdx % _weatherRadarFrames.length;
    const frame = _weatherRadarFrames[idx];
    _weatherRadarLayer.setUrl(`${_weatherRadarHost}${frame.path}/256/{z}/{x}/{y}/2/1_1.png`);

    const label = document.getElementById('wx-radar-time');
    if (label) {
        const isNow = idx === _weatherRadarFrames.length - 1;
        const timeStr = new Date(frame.time * 1000)
            .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        label.textContent = isNow ? `${_wxT('now')} · ${timeStr}` : timeStr;
    }
}

function _weatherScheduleNextRadarFrame() {
    const atNow = _weatherRadarFrameIdx % _weatherRadarFrames.length === _weatherRadarFrames.length - 1;
    _weatherRadarAnimTimer = setTimeout(() => {
        if (_weatherRadarFrames.length) {
            _weatherRadarFrameIdx = (_weatherRadarFrameIdx + 1) % _weatherRadarFrames.length;
            _weatherShowRadarFrame();
        }
        _weatherScheduleNextRadarFrame();
    }, atNow ? RADAR_LOOP_PAUSE_MS : RADAR_FRAME_MS);
}

function _weatherDestroyMap() {
    if (_weatherRadarAnimTimer) {
        clearTimeout(_weatherRadarAnimTimer);
        _weatherRadarAnimTimer = null;
    }
    _weatherRadarFrames = [];
    _weatherRadarFrameIdx = 0;
    _weatherRadarHost = '';
    if (_weatherMap) {
        _weatherMap.remove();
        _weatherMap = null;
        _weatherRadarLayer = null;
        _weatherMarker = null;
    }
}

window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.weather = {
    controller: {
        get isActive() { return true; },
        updateMetadata() {},
        handleNavEvent() { return false; },
        handleButton() { return false; },
    },
    item: { title: 'WEATHER', path: 'menu/weather' },
    after: 'menu/news',
    view: {
        title: 'WEATHER',
        content: `
            <style>
                #weather-view { width:100%; height:100%; box-sizing:border-box; padding:40px 60px; color:#fff; font-weight:300; overflow-y:auto; }
                #weather-view .wx-loc { font-size:0.95rem; letter-spacing:0.5px; color:rgba(255,255,255,0.5); text-transform:uppercase; margin-bottom:14px; }
                #weather-view .wx-now { display:flex; align-items:center; gap:24px; margin-bottom:8px; }
                #weather-view .wx-icon { font-size:3.4rem; color:#a8c8e8; }
                #weather-view .wx-temp { font-size:4rem; font-weight:200; }
                #weather-view .wx-sub { font-size:1rem; color:rgba(255,255,255,0.6); line-height:1.5; }
                #weather-view .wx-rain { font-size:1.1rem; margin:18px 0 24px; letter-spacing:0.5px; color:rgba(255,255,255,0.7); }
                #weather-view .wx-rain.yes { color:#7ec8ff; }
                #weather-view .wx-hourly { display:flex; gap:22px; flex-wrap:wrap; }
                #weather-view .wx-hour { text-align:center; font-size:0.85rem; color:rgba(255,255,255,0.8); min-width:46px; }
                #weather-view .wx-hour .t { color:rgba(255,255,255,0.5); margin-bottom:6px; }
                #weather-view .wx-hour .ph { display:block; font-size:1.3rem; color:#a8c8e8; margin:2px 0; }
                #weather-view .wx-hour .r { color:#7ec8ff; margin-top:4px; font-size:0.75rem; min-height:1em; }
                #weather-view .wx-error { color:rgba(255,255,255,0.5); font-size:0.95rem; }
                #weather-view .wx-credit { font-size:0.7rem; letter-spacing:0.5px; color:rgba(255,255,255,0.35); margin-top:16px; }
                #weather-view .wx-radar-label { font-size:0.8rem; letter-spacing:1px; color:rgba(255,255,255,0.4); text-transform:uppercase; margin:28px 0 10px; }
                #weather-view #wx-radar-map { position:relative; height:280px; border-radius:8px; overflow:hidden; background:#111; }
                #weather-view #wx-radar-time { position:absolute; top:10px; left:10px; z-index:1000; background:rgba(0,0,0,0.55); color:#fff; font-size:0.8rem; letter-spacing:0.5px; padding:4px 10px; border-radius:12px; pointer-events:none; }
                #weather-view .leaflet-control-attribution { background:rgba(0,0,0,0.55); color:rgba(255,255,255,0.6); font-size:0.65rem; }
                #weather-view .leaflet-control-attribution a { color:rgba(255,255,255,0.8); }
            </style>
            <div id="weather-view">
                <div id="weather-text"><div class="wx-error">${_wxT('loading')}</div></div>
                <div class="wx-radar-label">${_wxT('radarLabel')}</div>
                <div id="wx-radar-map"><div id="wx-radar-time"></div></div>
            </div>
        `,
    },

    onAdd() {},

    onMount() {
        if (_weatherTimer) clearInterval(_weatherTimer);
        _weatherFetchAndRender();
        _weatherTimer = setInterval(_weatherFetchAndRender, 5 * 60 * 1000);
    },

    onRemove() {
        if (_weatherTimer) {
            clearInterval(_weatherTimer);
            _weatherTimer = null;
        }
        _weatherDestroyMap();
    },
};
