/**
 * PlayingProgress — how far into the current track we are (AFSPILLER view).
 *
 * The media payload already carries position/duration, but nothing pushes a
 * tick per second: players broadcast on track_change, on state_change and —
 * for Sonos — when the position jumps (external_control). So the bar is
 * anchored at the last reported position and left to the clock. The model
 * extrapolates from (anchorSec, anchorAt) while playing and freezes when it
 * isn't; every new media update re-anchors it.
 *
 * Two kinds of input re-anchor the model:
 *   update(media)         a full media payload (media_update on the router WS)
 *   applyProgress(event)  position/duration only (media_progress), sent by a
 *                         player whose source owns the metadata — everything
 *                         playing through mpv, where the length is known to
 *                         the player and to nobody else
 *
 * Rendering hands the whole animation to CSS — a single `width: 100%`
 * transition lasting the rest of the track — so no timer runs on the device
 * between media updates.
 *
 * Three shapes are accepted for position/duration, because the backends
 * disagree (see services/players/* and lib/source_base.post_media_update):
 *   "3:34" / "0:03:34"  — the player services (sonos, bluesound, wiim, heos,
 *                         mozart, ase) format before broadcasting
 *   214                 — whole seconds (airplay source, source_base default)
 *   214000              — milliseconds, via the explicit *_ms fields (demo
 *                         backend, emulator) or a plain number too large to be
 *                         seconds (safety net for a backend reporting ms)
 *
 * A missing or zero duration means "length unknown" — live radio, or a source
 * that doesn't report it. The bar then hides rather than showing a wrong
 * fraction, and such a payload never re-anchors the position either:
 * post_media_update() defaults duration and position to 0 *together*, so a
 * source pushing metadata for a track the player is also reporting would
 * otherwise rewind the bar to the start.
 */

// A plain number above this many seconds (10 hours) cannot be a track length —
// treat it as milliseconds instead.
const PROGRESS_MS_THRESHOLD = 36000;

const PROGRESS_TIME_RE = /^(?:(\d+):)?(\d{1,2}):(\d{2})$/;

function progressNumber(value) {
    if (typeof value !== 'number' || !isFinite(value) || value < 0) return null;
    return value;
}

/** "3:34" / "0:03:34" → seconds, or null if it isn't a clock value. */
function progressParseClock(value) {
    const m = PROGRESS_TIME_RE.exec(String(value).trim());
    if (!m) return null;
    const hours = m[1] ? parseInt(m[1], 10) : 0;
    return hours * 3600 + parseInt(m[2], 10) * 60 + parseInt(m[3], 10);
}

/** Normalize one of the three shapes above to seconds, or null if unusable. */
function progressSeconds(msValue, value) {
    const ms = progressNumber(msValue);
    if (ms !== null) return ms / 1000;
    if (typeof value === 'string') {
        return value ? progressParseClock(value) : null;
    }
    const n = progressNumber(value);
    if (n === null) return null;
    return n > PROGRESS_MS_THRESHOLD ? n / 1000 : n;
}

/**
 * Track identity. Deliberately not track_id: only Spotify-backed payloads
 * carry one, and it is preserved across updates, so it can't tell "same track,
 * another push" from "next track".
 */
function progressTrackKey(media) {
    return [media.title || '', media.artist || '', media.album || ''].join('|');
}

// Mirrors isPlayingState() in media-manager.js; inlined so this module loads
// standalone under `node --test` (TRANSITIONING = Sonos between tracks).
function progressIsPlaying(state) {
    if (typeof window !== 'undefined' && typeof window.isPlayingState === 'function') {
        return window.isPlayingState(state);
    }
    return state === 'playing' || state === 'TRANSITIONING';
}

const PROGRESS_EMPTY = Object.freeze({
    key: '', durationSec: 0, anchorSec: 0, anchorAt: 0, playing: false,
});

/** Where the anchor has drifted to by ``now`` (clamped to the track length). */
function progressSeek(state, now) {
    const sec = state.playing
        ? state.anchorSec + Math.max(0, (now - state.anchorAt) / 1000)
        : state.anchorSec;
    if (state.durationSec > 0) return Math.min(Math.max(sec, 0), state.durationSec);
    return Math.max(sec, 0);
}

/**
 * Fold a media update into the model. Pure — returns the next state.
 * ``now`` is a monotonic timestamp (performance.now()), not wall-clock: the
 * device boots with the previous shutdown's date and jumps when NTP lands.
 */
function progressUpdate(state, media, now) {
    const key = progressTrackKey(media);
    const playing = progressIsPlaying(media.state);
    const duration = progressSeconds(media.duration_ms, media.duration);
    const position = progressSeconds(media.position_ms, media.position);
    const hasDuration = duration !== null && duration > 0;

    if (key !== state.key) {
        // New track: start over, and wait for a payload that knows the length.
        return {
            key,
            durationSec: hasDuration ? duration : 0,
            anchorSec: hasDuration && position !== null ? position : 0,
            anchorAt: now,
            playing,
        };
    }

    // Same track. Only a payload that knows the length may move the anchor.
    if (hasDuration) {
        return {
            key,
            durationSec: duration,
            anchorSec: position !== null ? position : progressSeek(state, now),
            anchorAt: now,
            playing,
        };
    }

    // Length unknown in this payload — keep what we have, follow the state
    // (this is what freezes the bar on pause and resumes it on play).
    return {
        key,
        durationSec: state.durationSec,
        anchorSec: progressSeek(state, now),
        anchorAt: now,
        playing,
    };
}

/**
 * Fold in a progress-only event (media_progress on the router WS).
 *
 * A player sends these when its source owns the metadata: everything that
 * plays through mpv hands the player a URL and pushes its own title/artwork,
 * and post_media_update defaults the length to 0, so the numbers can only
 * come from the player. The event carries no track identity, so it re-anchors
 * whatever track the model is on and leaves the key alone.
 */
function progressApply(state, event, now) {
    const duration = progressSeconds(event.duration_ms, event.duration);
    if (duration === null || duration <= 0) return state;
    const position = progressSeconds(event.position_ms, event.position);
    return {
        key: state.key,
        durationSec: duration,
        anchorSec: position !== null ? position : progressSeek(state, now),
        anchorAt: now,
        playing: event.playing === undefined ? state.playing : !!event.playing,
    };
}

/** Seconds → the same m:ss / h:mm:ss the player services format. */
function progressClock(seconds) {
    const total = Math.max(0, Math.floor(seconds || 0));
    const s = String(total % 60).padStart(2, '0');
    const m = Math.floor(total / 60) % 60;
    if (total >= 3600) {
        return `${Math.floor(total / 3600)}:${String(m).padStart(2, '0')}:${s}`;
    }
    return `${m}:${s}`;
}

/** What to draw, or null when there is nothing meaningful to show. */
function progressSample(state, now) {
    if (!state || !(state.durationSec > 0)) return null;
    const positionSec = progressSeek(state, now);
    return {
        positionSec,
        durationSec: state.durationSec,
        fraction: positionSec / state.durationSec,
        remainingMs: Math.max(0, (state.durationSec - positionSec) * 1000),
        playing: state.playing,
    };
}

// ── Singleton + rendering (browser only) ──

const PlayingProgress = (() => {
    let model = PROGRESS_EMPTY;
    let ticker = null;

    const monotonic = () => (typeof performance !== 'undefined' && performance.now
        ? performance.now()
        : Date.now());

    /** Fold in a media update (called from MediaManager.handleMediaUpdate). */
    function update(media, now) {
        model = progressUpdate(model, media || {}, now === undefined ? monotonic() : now);
        return model;
    }

    /** Fold in a progress-only event (media_progress on the router WS). */
    function applyProgress(event, now) {
        model = progressApply(model, event || {}, now === undefined ? monotonic() : now);
        return model;
    }

    function sample(now) {
        return progressSample(model, now === undefined ? monotonic() : now);
    }

    function reset() {
        model = PROGRESS_EMPTY;
        ensureTicker(false);
    }

    /**
     * Draw every bar on the page — the one in the PLAYING info box and the one
     * in the immersive overlay, which is created lazily. Both are found by
     * class, so neither view has to know about the other.
     */
    function render(now) {
        if (typeof document === 'undefined') return;
        const groups = document.querySelectorAll('.bs5c-progress-group');
        if (!groups.length) return;
        const s = sample(now);
        for (const group of groups) {
            const fill = group.querySelector('.bs5c-progress-fill');
            if (!s) {
                group.hidden = true;
                if (fill) {
                    fill.style.setProperty('--bs5c-progress-ms', '0ms');
                    fill.style.width = '0%';
                }
                continue;
            }
            group.hidden = false;
            if (fill) {
                // Jump to the known position without animating...
                fill.style.setProperty('--bs5c-progress-ms', '0ms');
                fill.style.width = `${(s.fraction * 100).toFixed(3)}%`;
                if (s.playing && s.remainingMs > 0) {
                    // ...then let one CSS transition cover the rest of the track.
                    void fill.offsetWidth;   // commit the jump first
                    fill.style.setProperty('--bs5c-progress-ms',
                                           `${Math.round(s.remainingMs)}ms`);
                    fill.style.width = '100%';
                }
            }
            const total = group.querySelector('.bs5c-progress-total');
            if (total) total.textContent = progressClock(s.durationSec);
        }
        renderElapsed(s, now);
        ensureTicker(!!s && s.playing);
    }

    /**
     * The elapsed readout is the one thing CSS cannot carry — it needs a digit
     * every second. Kept apart from render() so the ticker only rewrites two
     * text nodes rather than restarting the bar's transition each second.
     */
    function renderElapsed(s, now) {
        const labels = document.querySelectorAll('.bs5c-progress-elapsed');
        if (!labels.length) return;
        const cur = s === undefined ? sample(now) : s;
        const text = cur ? progressClock(cur.positionSec) : '';
        for (const el of labels) {
            if (el.textContent !== text) el.textContent = text;
        }
    }

    /**
     * One second tick, alive only while a visible bar is actually advancing.
     * Nothing on screen (the view was left, playback paused, length unknown)
     * means no timer at all — the device runs this UI for days at a time.
     */
    function ensureTicker(wanted) {
        if (wanted && !ticker) {
            ticker = setInterval(() => {
                if (!document.querySelector('.bs5c-progress-elapsed')) {
                    ensureTicker(false);   // view gone — stop rather than spin
                    return;
                }
                renderElapsed();
            }, 1000);
        } else if (!wanted && ticker) {
            clearInterval(ticker);
            ticker = null;
        }
    }

    return { update, applyProgress, sample, render, reset,
             get model() { return model; } };
})();

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        PROGRESS_MS_THRESHOLD,
        PROGRESS_EMPTY,
        progressSeconds,
        progressTrackKey,
        progressClock,
        progressUpdate,
        progressApply,
        progressSample,
        PlayingProgress,
    };
} else {
    window.PlayingProgress = PlayingProgress;
}
