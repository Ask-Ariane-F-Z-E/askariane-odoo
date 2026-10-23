/** @odoo-module **/
// Call health: what the panel measures about a call, in the browser.
// Plain functions and one tracker per call, no Odoo imports, so Node's test
// runner loads this file as it is (static/tests/call_health.test.mjs).
//
// - Line quality from the Twilio Voice SDK's per-second "sample" event: rtt
//   (the browser to Twilio's EDGE and back; we cannot see the customer's
//   handset), jitter, packet loss (percent) and MOS.
// - Connect timings: click -> authorised -> device.connect -> ringing -> answered.
// - Who hung up: rep / customer / cancelled (before it connected) / error.
// - "API connection": the time an Odoo ping takes, every API_PING_MS.
// - Coach answer time: coach_pending to the coach's answer for the same pass,
//   both on the browser's clock.
// - A timeline of every event, capped.
// Raw samples stay in this tracker; only summary() goes to Odoo.

export const HEALTH_MAX_EVENTS = 300;
export const HEALTH_MAX_BYTES = 20000;
export const API_PING_MS = 10000;
// Samples kept per metric: two hours at one a second. Later ones are not kept.
const MAX_SAMPLES = 7200;
const SHOWN_EVENTS = 40;

// Red past Twilio's own warning thresholds (rtt 400 ms, MOS 3, loss 3 % on
// average); amber when nearer than comfortable or a warning is active.
export function qualityOf(sample, activeWarnings = 0) {
    if (!sample) return activeWarnings ? "amber" : "none";
    const { rtt, jitter, loss, mos } = sample;
    if ((mos !== null && mos < 3) || rtt > 400 || loss > 3 || jitter > 60) return "red";
    if (activeWarnings || (mos !== null && mos < 3.6) || rtt > 250 || loss > 1 || jitter > 30) return "amber";
    return "green";
}

function round(v, places = 1) {
    const f = 10 ** places;
    return Math.round(v * f) / f;
}

// min / avg / p95 (nearest rank) of a list of numbers, or null when empty.
export function stats(values, places = 1) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const p95 = sorted[Math.min(sorted.length - 1, Math.ceil(0.95 * sorted.length) - 1)];
    const avg = sorted.reduce((s, v) => s + v, 0) / sorted.length;
    return { min: round(sorted[0], places), avg: round(avg, places), p95: round(p95, places) };
}

function num(v) {
    return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function short(v, n) {
    if (v === undefined || v === null || v === "") return undefined;
    return String(v).slice(0, n);
}

// The timeline as Odoo stores it: at most HEALTH_MAX_EVENTS events and
// HEALTH_MAX_BYTES of JSON, cut from the end. Returns {events, dropped}.
export function capTimeline(events) {
    let out = events.slice(0, HEALTH_MAX_EVENTS);
    let dropped = events.length - out.length;
    while (out.length && JSON.stringify(out).length > HEALTH_MAX_BYTES) {
        const cut = Math.max(1, Math.floor(out.length / 10));
        out = out.slice(0, out.length - cut);
        dropped += cut;
    }
    return { events: out, dropped };
}

// One tracker per call. now() is the caller's clock (ms).
export function createHealth(now) {
    const t0 = now();
    const samples = { rtt: [], jitter: [], loss: [], mos: [] };
    let sampleCount = 0;
    let last = null;
    const events = [];
    let eventsDropped = 0;
    const marks = {};
    const warnings = new Set();
    let warningCount = 0;
    let hangupBy = null;
    const api = [];
    let apiErrors = 0;
    let apiLast = null;
    const coach = [];
    const pending = new Map(); // coach pass id -> when coach_pending arrived
    let coachState = "off";
    let levels = { input: null, output: null };

    const at = () => Math.max(0, Math.round(now() - t0));

    const tracker = {
        event(ev, { code, detail } = {}) {
            // Kept whole in memory up to a margin; summary() applies Odoo's caps.
            if (events.length >= HEALTH_MAX_EVENTS * 2) {
                eventsDropped += 1;
                return;
            }
            const e = { t: at(), ev: short(ev, 40) };
            if (short(code, 40)) e.code = short(code, 40);
            if (short(detail, 120)) e.detail = short(detail, 120);
            events.push(e);
        },
        // A connect step, timed and logged once from the click: the first time
        // wins. Twilio emits "ringing" per ringing signal (180, then 183 with
        // early media), which logged the phase twice (21 Oct 2026, 6.1 s / 8.4 s).
        mark(name) {
            if (name in marks) return;
            marks[name] = at();
            tracker.event(name);
        },
        sample(s) {
            if (!s) return;
            const point = {
                rtt: num(s.rtt), jitter: num(s.jitter), loss: num(s.packetsLostFraction), mos: num(s.mos),
            };
            sampleCount += 1;
            if (sampleCount <= MAX_SAMPLES) {
                for (const k of Object.keys(samples)) if (point[k] !== null) samples[k].push(point[k]);
            }
            last = point;
            levels = { input: num(s.audioInputLevel), output: num(s.audioOutputLevel) };
        },
        warning(name) {
            warnings.add(name);
            warningCount += 1;
            tracker.event("warning", { code: name });
        },
        warningCleared(name) {
            warnings.delete(name);
            tracker.event("warning_cleared", { code: name });
        },
        // The first reason wins: a rep's Hang up is not overwritten by the disconnect it causes.
        hangup(by) {
            if (!hangupBy) hangupBy = by;
        },
        get hangupBy() {
            return hangupBy;
        },
        apiPing(ms) {
            apiLast = Math.round(ms);
            api.push(apiLast);
        },
        apiError(code) {
            apiErrors += 1;
            apiLast = null;
            tracker.event("api_error", { code });
        },
        coachState(s) {
            coachState = s;
            tracker.event("coach_state", { code: s });
        },
        coachPending(passId) {
            if (passId !== undefined && passId !== null && !pending.has(passId)) pending.set(passId, now());
        },
        coachAnswer(passId) {
            if (!pending.has(passId)) return;
            coach.push(Math.round(now() - pending.get(passId)));
            pending.delete(passId);
        },
        // What the panel shows.
        view() {
            const a = stats(api, 0);
            const c = stats(coach, 0);
            const t = (from, to) => (marks[to] !== undefined && marks[from] !== undefined ? marks[to] - marks[from] : null);
            return {
                dot: qualityOf(last, warnings.size),
                rtt: last?.rtt ?? null,
                jitter: last?.jitter ?? null,
                loss: last?.loss ?? null,
                mos: last?.mos ?? null,
                inputLevel: levels.input,
                outputLevel: levels.output,
                warnings: [...warnings],
                timings: {
                    authorize: t("click", "authorized"), connect: t("authorized", "connected"),
                    ringing: t("connected", "ringing"), answer: t("ringing", "answered"),
                },
                hangupBy,
                apiMs: apiLast,
                apiAvg: a?.avg ?? null,
                apiErrors,
                coachMs: c?.avg ?? null,
                coachState,
                events: events.slice(-SHOWN_EVENTS),
            };
        },
        // What Odoo stores: numbers and the capped timeline. No raw samples.
        summary() {
            const t = (from, to) => (marks[to] !== undefined && marks[from] !== undefined ? marks[to] - marks[from] : null);
            const { events: timeline, dropped } = capTimeline(events);
            const a = stats(api, 0);
            const c = stats(coach, 0);
            return {
                samples: sampleCount,
                rtt: stats(samples.rtt), jitter: stats(samples.jitter),
                loss: stats(samples.loss, 2), mos: stats(samples.mos, 2),
                timings: {
                    authorize_ms: t("click", "authorized"), connect_ms: t("authorized", "connected"),
                    ringing_ms: t("connected", "ringing"), answer_ms: t("ringing", "answered"),
                },
                hangup_by: hangupBy || "unknown",
                warnings: warningCount,
                api: { avg: a?.avg ?? null, p95: a?.p95 ?? null, errors: apiErrors },
                coach: { avg: c?.avg ?? null, p95: c?.p95 ?? null },
                events: timeline,
                events_dropped: dropped + eventsDropped,
            };
        },
    };
    tracker.event("click");
    marks.click = 0;
    return tracker;
}
