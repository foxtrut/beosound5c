/**
 * Weather Source Preset — DMI (Danish Meteorological Institute) forecast
 *
 * A single info page, not an arc browser: fetches the weather service's
 * /forecast endpoint directly and renders today's conditions + an hourly
 * rain outlook. No iframe, no ArcList — there is nothing to drill into.
 */

let _weatherTimer = null;

function _weatherUrl() {
    return (window.AppConfig?.weatherServiceUrl || 'http://localhost:8790') + '/forecast';
}

async function _weatherFetchAndRender() {
    const root = document.getElementById('weather-view');
    if (!root) return;
    try {
        const resp = await fetch(_weatherUrl());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        _weatherRender(root, data);
    } catch (e) {
        root.innerHTML = '<div class="wx-error">Weather unavailable — is the weather source configured and running?</div>';
    }
}

function _weatherRender(root, data) {
    if (!data || !data.current || !data.today) {
        root.innerHTML = '<div class="wx-error">No forecast data yet.</div>';
        return;
    }
    const cur = data.current;
    const today = data.today;
    const hours = data.hourly || [];

    const rainLine = today.will_rain
        ? `Rain expected today${today.rain_mm != null ? ' — ~' + today.rain_mm + ' mm' : ''}`
        : 'No rain expected today';

    const hourly = hours.slice(0, 10).map(h => `
        <div class="wx-hour">
            <div class="t">${h.time}</div>
            <div>${h.temp_c != null ? Math.round(h.temp_c) + '°' : '–'}</div>
            <div class="r">${h.rain_mm != null && h.rain_mm > 0 ? h.rain_mm.toFixed(1) + 'mm' : ''}</div>
        </div>
    `).join('');

    const range = (today.temp_min_c != null && today.temp_max_c != null)
        ? `${Math.round(today.temp_min_c)}° / ${Math.round(today.temp_max_c)}°`
        : '';
    const details = [
        cur.cloud_pct != null ? `${cur.cloud_pct}% cloud` : '',
        cur.wind_ms != null ? `${Math.round(cur.wind_ms)} m/s wind` : '',
    ].filter(Boolean).join(' · ');

    root.innerHTML = `
        <div class="wx-now">
            <div class="wx-temp">${cur.temp_c != null ? Math.round(cur.temp_c) + '°' : '–'}</div>
            <div class="wx-sub">${range}<br>${details}</div>
        </div>
        <div class="wx-rain ${today.will_rain ? 'yes' : ''}">${rainLine}</div>
        <div class="wx-hourly">${hourly}</div>
    `;
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
                #weather-view .wx-now { display:flex; align-items:baseline; gap:20px; margin-bottom:8px; }
                #weather-view .wx-temp { font-size:4rem; font-weight:200; }
                #weather-view .wx-sub { font-size:1rem; color:rgba(255,255,255,0.6); line-height:1.5; }
                #weather-view .wx-rain { font-size:1.1rem; margin:18px 0 24px; letter-spacing:0.5px; color:rgba(255,255,255,0.7); }
                #weather-view .wx-rain.yes { color:#7ec8ff; }
                #weather-view .wx-hourly { display:flex; gap:22px; flex-wrap:wrap; }
                #weather-view .wx-hour { text-align:center; font-size:0.85rem; color:rgba(255,255,255,0.8); min-width:46px; }
                #weather-view .wx-hour .t { color:rgba(255,255,255,0.5); margin-bottom:6px; }
                #weather-view .wx-hour .r { color:#7ec8ff; margin-top:4px; font-size:0.75rem; min-height:1em; }
                #weather-view .wx-error { color:rgba(255,255,255,0.5); font-size:0.95rem; }
            </style>
            <div id="weather-view"><div class="wx-error">Loading…</div></div>
        `,
    },

    onAdd() {},

    onMount() {
        _weatherFetchAndRender();
        _weatherTimer = setInterval(_weatherFetchAndRender, 5 * 60 * 1000);
    },

    onRemove() {
        if (_weatherTimer) {
            clearInterval(_weatherTimer);
            _weatherTimer = null;
        }
    },
};
