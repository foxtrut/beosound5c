/**
 * Tests for web/js/playing-progress.js — the AFSPILLER progress model.
 *
 * Run with: node --test tests/unit/js/test_playing_progress.js
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const {
    PROGRESS_EMPTY,
    progressSeconds,
    progressTrackKey,
    progressUpdate,
    progressApply,
    progressSample,
} = require('../../../web/js/playing-progress.js');

const TRACK = { title: 'Joyride', artist: 'Roxette', album: 'Joyride' };

/** Fold a payload into the model at time `at` (ms, monotonic). */
function upd(state, media, at) {
    return progressUpdate(state, { ...TRACK, ...media }, at);
}

describe('progressSeconds', () => {
    it('parses the players\' m:ss strings', () => {
        assert.equal(progressSeconds(undefined, '3:34'), 214);
        assert.equal(progressSeconds(undefined, '0:07'), 7);
    });

    it('parses h:mm:ss (soco reports this shape)', () => {
        assert.equal(progressSeconds(undefined, '1:03:34'), 3814);
        assert.equal(progressSeconds(undefined, '0:03:34'), 214);
    });

    it('takes plain numbers as seconds (airplay, source_base)', () => {
        assert.equal(progressSeconds(undefined, 214), 214);
        assert.equal(progressSeconds(undefined, 0), 0);
    });

    it('prefers an explicit *_ms field (demo backend, emulator)', () => {
        assert.equal(progressSeconds(214000, '3:34'), 214);
        assert.equal(progressSeconds(0, '3:34'), 0);
    });

    it('treats an implausibly large number as milliseconds', () => {
        // go-librespot reports ms in the same field others use for seconds.
        assert.equal(progressSeconds(undefined, 214000), 214);
    });

    it('rejects what is not a time at all', () => {
        // Sonos answers NOT_IMPLEMENTED for streams without a length.
        assert.equal(progressSeconds(undefined, 'NOT_IMPLEMENTED'), null);
        assert.equal(progressSeconds(undefined, ''), null);
        assert.equal(progressSeconds(undefined, undefined), null);
        assert.equal(progressSeconds(undefined, -5), null);
        assert.equal(progressSeconds(undefined, '3:4'), null);
    });
});

describe('progressTrackKey', () => {
    it('is title|artist|album', () => {
        assert.equal(progressTrackKey(TRACK), 'Joyride|Roxette|Joyride');
    });

    it('survives a payload with nothing in it', () => {
        assert.equal(progressTrackKey({}), '||');
    });
});

describe('progressSample', () => {
    it('is null until a length is known', () => {
        assert.equal(progressSample(PROGRESS_EMPTY, 0), null);
        const s = upd(PROGRESS_EMPTY, { state: 'playing', duration: '0:00' }, 0);
        assert.equal(progressSample(s, 0), null);
    });

    it('extrapolates from the anchor while playing', () => {
        const s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:30' }, 1000);
        const at10s = progressSample(s, 11000);
        assert.equal(at10s.positionSec, 40);
        assert.equal(at10s.durationSec, 214);
        assert.equal(at10s.fraction, 40 / 214);
        assert.equal(at10s.remainingMs, 174000);
        assert.equal(at10s.playing, true);
    });

    it('never runs past the end of the track', () => {
        const s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '3:30' }, 0);
        const late = progressSample(s, 60000);
        assert.equal(late.positionSec, 214);
        assert.equal(late.fraction, 1);
        assert.equal(late.remainingMs, 0);
    });

    it('does not move while paused', () => {
        const s = upd(PROGRESS_EMPTY,
            { state: 'paused', duration: '3:34', position: '1:00' }, 0);
        assert.equal(progressSample(s, 60000).positionSec, 60);
        assert.equal(progressSample(s, 60000).playing, false);
    });
});

describe('progressUpdate', () => {
    it('freezes where it was when playback pauses', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:10' }, 0);
        // 20s later the player pauses and reports the current position.
        s = upd(s, { state: 'paused', duration: '3:34', position: '0:30' }, 20000);
        assert.equal(progressSample(s, 20000).positionSec, 30);
        assert.equal(progressSample(s, 120000).positionSec, 30);
    });

    it('picks up again from the resumed position', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'paused', duration: '3:34', position: '0:30' }, 0);
        s = upd(s, { state: 'playing', duration: '3:34', position: '0:30' }, 60000);
        assert.equal(progressSample(s, 65000).positionSec, 35);
    });

    it('starts over on a track change', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '3:00' }, 0);
        s = progressUpdate(s, {
            title: 'The Look', artist: 'Roxette', album: 'Look Sharp!',
            state: 'playing', duration: '3:56', position: '0:00',
        }, 1000);
        const now = progressSample(s, 1000);
        assert.equal(now.positionSec, 0);
        assert.equal(now.durationSec, 236);
    });

    it('ignores a payload that does not know the length', () => {
        // A source pushing metadata for a track the player also reports:
        // post_media_update defaults duration and position to 0 together, so
        // it must not rewind the bar (nor blank it).
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '1:00' }, 0);
        s = upd(s, { state: 'playing', duration: 0, position: 0 }, 30000);
        const now = progressSample(s, 30000);
        assert.equal(now.durationSec, 214);
        assert.equal(now.positionSec, 90);
    });

    it('keeps following the clock across a lengthless payload', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:00' }, 0);
        s = upd(s, { state: 'playing' }, 10000);
        assert.equal(progressSample(s, 20000).positionSec, 20);
    });

    it('still pauses on a lengthless payload', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:00' }, 0);
        s = upd(s, { state: 'paused' }, 10000);
        assert.equal(progressSample(s, 60000).positionSec, 10);
    });

    it('hides the bar when a lengthless track follows a timed one', () => {
        // Live radio after an album track: no length, so no fraction to show.
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:00' }, 0);
        s = progressUpdate(s, {
            title: 'P1', artist: 'Sveriges Radio', album: '',
            state: 'playing', duration: 'NOT_IMPLEMENTED', position: '0:00',
        }, 1000);
        assert.equal(progressSample(s, 5000), null);
    });

    it('treats TRANSITIONING as playing (Sonos between tracks)', () => {
        const s = upd(PROGRESS_EMPTY,
            { state: 'TRANSITIONING', duration: '3:34', position: '0:10' }, 0);
        assert.equal(progressSample(s, 5000).positionSec, 15);
    });

    it('adopts a seek reported mid-track', () => {
        // Sonos broadcasts external_control when the position jumps.
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:10' }, 0);
        s = upd(s, { state: 'playing', duration: '3:34', position: '2:30' }, 5000);
        assert.equal(progressSample(s, 5000).positionSec, 150);
    });
});

describe('progressApply', () => {
    // media_progress carries no track identity — it re-anchors whatever the
    // model is already on. This is the only length an mpv-backed source gets.
    it('supplies a length the media payload never had', () => {
        let s = upd(PROGRESS_EMPTY, { state: 'playing', duration: 0, position: 0 }, 0);
        assert.equal(progressSample(s, 0), null);
        s = progressApply(s, { position_ms: 30000, duration_ms: 214000, playing: true }, 1000);
        const now = progressSample(s, 1000);
        assert.equal(now.durationSec, 214);
        assert.equal(now.positionSec, 30);
    });

    it('keeps the track identity, so the next payload does not reset it', () => {
        let s = upd(PROGRESS_EMPTY, { state: 'playing' }, 0);
        s = progressApply(s, { position_ms: 30000, duration_ms: 214000, playing: true }, 0);
        s = upd(s, { state: 'playing' }, 5000);   // same track, still no length
        assert.equal(progressSample(s, 5000).positionSec, 35);
    });

    it('pauses and resumes on the state the player reports', () => {
        let s = upd(PROGRESS_EMPTY, { state: 'playing' }, 0);
        s = progressApply(s, { position_ms: 30000, duration_ms: 214000, playing: false }, 0);
        assert.equal(progressSample(s, 60000).positionSec, 30);
        s = progressApply(s, { position_ms: 30000, duration_ms: 214000, playing: true }, 60000);
        assert.equal(progressSample(s, 65000).positionSec, 35);
    });

    it('corrects drift from an earlier anchor', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '0:00' }, 0);
        s = progressApply(s, { position_ms: 95000, duration_ms: 214000, playing: true }, 60000);
        assert.equal(progressSample(s, 60000).positionSec, 95);
    });

    it('ignores an event with no usable length', () => {
        let s = upd(PROGRESS_EMPTY,
            { state: 'playing', duration: '3:34', position: '1:00' }, 0);
        s = progressApply(s, { position_ms: 0, duration_ms: 0, playing: true }, 10000);
        assert.equal(progressSample(s, 10000).positionSec, 70);
        assert.equal(progressSample(s, 10000).durationSec, 214);
    });
});
