// Call health in the panel (owner request, 21 Oct 2026: "debugger, ping,
// quality... it should be in the front end"):
// CH1-CH5   the tracker's own rules (static/src/call_health.js);
// CH6-CH11  how the REAL dialer_service.js feeds it from the fake Twilio call,
//           the Odoo ping and the coach feed, and sends one summary at the end;
// CH12-CH13 the panel: a rep can hide the section, a manager always has it.
// Run: `node --test ariane_dialer/static/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// call_health.js imports nothing; a copy named .mjs loads in plain Node 18+
// (Node 18 reads a bare .js as CommonJS).
const tmp = mkdtempSync(join(tmpdir(), "call-health-"));
copyFileSync(fileURLToPath(new URL("../src/call_health.js", import.meta.url)), join(tmp, "call_health.mjs"));
const { capTimeline, createHealth, HEALTH_MAX_BYTES, HEALTH_MAX_EVENTS, qualityOf, stats } =
    await import(pathToFileURL(join(tmp, "call_health.mjs")).href);
rmSync(tmp, { recursive: true, force: true });
import { deferred, flush, loadPanel, startService } from "./dialer_service_harness.mjs";

const PHONE = "+14155550101";
const PANEL_XML = readFileSync(fileURLToPath(new URL("../src/panel.xml", import.meta.url)), "utf8");
const MANAGER = "ariane_dialer.group_dialer_manager";
const USER = "ariane_dialer.group_dialer_user";

function clock(start = 1000) {
    const c = { t: start };
    c.now = () => c.t;
    return c;
}

function healthPosts(h) {
    return h.rpcCalls.filter((c) => c.route === "/ariane_dialer/call/health");
}

function pings(h) {
    return h.rpcCalls.filter((c) => c.route === "/ariane_dialer/ping").length;
}

// A live call: dialled, ringing, answered.
async function liveCall(opts = {}) {
    const h = await startService(opts);
    await h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    const call = h.phoneCalls[0];
    call.fire("ringing");
    call.fire("accept");
    await flush();
    assert.equal(h.state.mode, "live");
    return { h, call };
}

// ------------------------------------------------------------------ tracker rules
test("CH1: stats are min, mean and the 95th percentile (nearest rank)", () => {
    const values = Array.from({ length: 100 }, (_, i) => 100 - i); // 100..1, unsorted on purpose
    assert.deepEqual(stats(values, 0), { min: 1, avg: 51, p95: 95 });
    assert.deepEqual(stats([7]), { min: 7, avg: 7, p95: 7 });
    assert.equal(stats([]), null);
});

test("CH2: the quality dot: green, amber near the limits or on a warning, red past Twilio's thresholds", () => {
    const good = { rtt: 80, jitter: 5, loss: 0, mos: 4.4 };
    assert.equal(qualityOf(good), "green");
    assert.equal(qualityOf(good, 1), "amber", "an active Twilio warning must not show green");
    assert.equal(qualityOf({ ...good, rtt: 300 }), "amber");
    assert.equal(qualityOf({ ...good, rtt: 450 }), "red");
    assert.equal(qualityOf({ ...good, mos: 2.8 }), "red");
    assert.equal(qualityOf({ ...good, loss: 5 }), "red");
    assert.equal(qualityOf({ ...good, jitter: 80 }), "red");
    assert.equal(qualityOf(null), "none");
});

test("CH3: the timeline is capped at 300 events and 20 KB, cut from the end", () => {
    const many = Array.from({ length: 400 }, (_, i) => ({ t: i, ev: "x" }));
    const a = capTimeline(many);
    assert.equal(a.events.length, HEALTH_MAX_EVENTS);
    assert.equal(a.dropped, 100);
    assert.equal(a.events[0].t, 0, "the start of the call was dropped instead of the end");
    const big = Array.from({ length: 250 }, (_, i) => ({ t: i, ev: "warning", detail: "d".repeat(120) }));
    const b = capTimeline(big);
    assert.ok(JSON.stringify(b.events).length <= HEALTH_MAX_BYTES, "over 20 KB");
    assert.equal(b.events.length + b.dropped, 250);
    assert.ok(b.dropped > 0, "the 20 KB cut did not run");
    // the byte cut also drops from the end: the call's start is kept, in order
    assert.deepEqual(b.events.map((e) => e.t), Array.from({ length: b.events.length }, (_, i) => i));
    // control: a short timeline is kept whole
    assert.deepEqual(capTimeline(many.slice(0, 5)), { events: many.slice(0, 5), dropped: 0 });
});

test("CH4: the summary holds numbers and the timeline, never the raw samples", () => {
    const c = clock();
    const t = createHealth(c.now);
    for (let i = 0; i < 50; i++) {
        c.t += 1000;
        t.sample({ rtt: 100 + i, jitter: 4, packetsLostFraction: 0.5, mos: 4.3 });
    }
    const s = t.summary();
    assert.equal(s.samples, 50);
    assert.deepEqual(s.rtt, { min: 100, avg: 124.5, p95: 147 });
    assert.deepEqual(Object.keys(s).sort(), ["api", "coach", "events", "events_dropped", "hangup_by",
        "jitter", "loss", "mos", "rtt", "samples", "timings", "warnings"]);
    const longest = Math.max(...Object.values(s).filter(Array.isArray).map((a) => a.length));
    assert.ok(longest < 50, "a per-second list went into the summary");
    assert.ok(!JSON.stringify(s).includes("149"), "a raw sample value is in the summary");
});

test("CH5: who hung up: the first reason is kept", () => {
    const t = createHealth(clock().now);
    assert.equal(t.summary().hangup_by, "unknown");
    t.hangup("rep");
    t.hangup("customer"); // the disconnect the rep's own Hang up causes
    assert.equal(t.summary().hangup_by, "rep");
});

// ------------------------------------------------------------- the service feeds it
test("CH6: Twilio samples and warnings drive the live dot and the numbers", async () => {
    const { h, call } = await liveCall();
    call.sample({ rtt: 90 });
    assert.equal(h.state.health.dot, "green");
    assert.equal(h.state.health.rtt, 90);
    call.sample({ rtt: 520, packetsLostFraction: 4.2, mos: 2.6, jitter: 70 });
    assert.equal(h.state.health.dot, "red");
    assert.deepEqual([h.state.health.rtt, h.state.health.loss, h.state.health.mos, h.state.health.jitter], [520, 4.2, 2.6, 70]);
    call.sample({});
    call.fire("warning", "high-jitter");
    assert.equal(h.state.health.dot, "amber");
    assert.deepEqual(h.state.health.warnings, ["high-jitter"]);
    call.fire("warning-cleared", "high-jitter");
    assert.equal(h.state.health.dot, "green");
    const evs = h.state.health.events.map((e) => `${e.ev}:${e.code || ""}`);
    assert.ok(evs.includes("warning:high-jitter") && evs.includes("warning_cleared:high-jitter"), evs.join(" "));
});

test("CH7: connect timings: click -> authorised -> connected -> ringing -> answered", async () => {
    const auth = deferred();
    const conn = deferred();
    const h = await startService({ connect: () => conn.promise });
    h.apiRoutes["/v1/dialer/authorize"] = () => auth.promise;
    const run = h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    await flush();
    await h.clock.advance(120);
    auth.resolve({ authorization_id: "auth", live: false });
    await flush();
    await h.clock.advance(300);
    const call = { parameters: { CallSid: "CA-t" }, handlers: {}, on(n, f) { this.handlers[n] = f; }, disconnect() {}, mute() {} };
    conn.resolve(call);
    await run;
    await h.clock.advance(500);
    call.handlers.ringing();
    await h.clock.advance(2000);
    call.handlers.accept();
    assert.deepEqual(h.state.health.timings, { authorize: 120, connect: 300, ringing: 500, answer: 2000 });
    const order = h.state.health.events.map((e) => e.ev).filter((e) => e !== "coach_connect");
    assert.deepEqual(order.slice(0, 5), ["click", "authorized", "connected", "ringing", "answered"]);
});

test("CH14: Twilio's second 'ringing' (180 then 183 with early media) is logged once; the first time wins", async () => {
    // Unit: a phase marked twice keeps its first time and one timeline entry.
    const c = clock();
    const t = createHealth(c.now);
    c.t += 6100;
    t.mark("ringing");
    c.t += 2300;
    t.mark("ringing");
    const ringing = t.summary().events.filter((e) => e.ev === "ringing");
    assert.deepEqual(ringing, [{ t: 6100, ev: "ringing" }]);
    // The real service, fed the SDK's two emissions.
    const conn = deferred();
    const h = await startService({ connect: () => conn.promise });
    const run = h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    await flush();
    const call = { parameters: { CallSid: "CA-r2" }, handlers: {}, on(n, f) { this.handlers[n] = f; }, disconnect() {}, mute() {} };
    conn.resolve(call);
    await run;
    await h.clock.advance(400);
    call.handlers.ringing(false);
    await h.clock.advance(2300);
    call.handlers.ringing(true);
    await h.clock.advance(1000);
    call.handlers.accept();
    const evs = h.state.health.events.map((e) => e.ev);
    assert.equal(evs.filter((e) => e === "ringing").length, 1, `ringing logged ${evs.filter((e) => e === "ringing").length} times`);
    assert.equal(h.state.health.timings.answer, 3300, "the answer time is not measured from the FIRST ringing");
});

test("CH8: hang-up reason: rep, customer, cancelled while connecting, error", async () => {
    const rep = await liveCall({ disconnectFires: true });
    await rep.h.dialer.hangup();
    assert.equal(rep.h.state.health.hangupBy, "rep", "the rep's Hang up was recorded as the customer's");

    const cust = await liveCall();
    cust.call.fire("disconnect");
    assert.equal(cust.h.state.health.hangupBy, "customer");

    const h = await startService();
    const auth = deferred();
    h.apiRoutes["/v1/dialer/authorize"] = () => auth.promise;
    const run = h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    await flush();
    await h.dialer.hangup();
    auth.resolve({ authorization_id: "auth", live: false });
    await run;
    assert.equal(h.state.health.hangupBy, "cancelled");

    const bad = await startService();
    bad.apiRoutes["/v1/dialer/authorize"] = () => {
        throw Object.assign(new Error("number blocked"), { status: 403 });
    };
    await bad.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    assert.equal(bad.state.health.hangupBy, "error");
    assert.ok(bad.state.health.events.some((e) => e.ev === "error" && e.code === "403"), "the error code is not on the timeline");
});

test("CH9: the API connection is pinged every 10 s while the call is on, and stops when it ends", async () => {
    const { h, call } = await liveCall();
    assert.equal(pings(h), 0);
    await h.clock.advance(30000);
    assert.equal(pings(h), 3);
    assert.equal(typeof h.state.health.apiMs, "number");
    h.rpcRoutes["/ariane_dialer/ping"] = () => {
        throw Object.assign(new Error("down"), { status: 502 });
    };
    await h.clock.advance(10000);
    assert.equal(h.state.health.apiErrors, 1);
    assert.equal(h.state.health.apiMs, null, "a failed ping still shows the last good time");
    call.fire("disconnect");
    await h.clock.advance(60000);
    assert.equal(pings(h), 4, "the ping kept running after the call ended");
});

test("CH10: coach answer time and coach stream events", async () => {
    const { h } = await liveCall({ liveCoach: true });
    const ws = h.socket();
    ws.onopen();
    ws.emit({ type: "coach_state", state: "live", seq: 1 });
    ws.emit({ type: "coach_pending", pass_id: 3, question: true, emitted_at: new Date(h.clock.now).toISOString(), seq: 2 });
    await h.clock.advance(1500);
    ws.emit({ type: "coach", pass_id: 3, say_next: "Hi", seq: 3 });
    assert.equal(h.state.health.coachMs, 1500);
    // an answer with no pending pass is not timed
    ws.emit({ type: "coach", pass_id: 9, say_next: "x", seq: 4 });
    assert.equal(h.state.health.coachMs, 1500);
    ws.onclose();
    const evs = h.state.health.events.map((e) => e.ev);
    for (const ev of ["coach_connect", "coach_open", "coach_state", "coach_reconnect"]) assert.ok(evs.includes(ev), `${ev} missing: ${evs}`);
    assert.equal(h.state.health.coachState, "live");
});

test("CH11: after the wrap-up is saved, ONE summary goes to the call record; nothing for preview", async () => {
    const { h, call } = await liveCall();
    call.sample({ rtt: 120 });
    call.fire("disconnect");
    await flush();
    assert.equal(h.state.mode, "wrapup");
    assert.ok(h.state.health, "Call health is gone after the call ended (it must stay through wrap-up)");
    assert.equal(healthPosts(h).length, 0, "the summary went before the wrap-up was saved");
    await h.dialer.wrapup({ outcome: "interested", note: "abc" });
    const posts = healthPosts(h);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].params.call_id, 77);
    const s = posts[0].params.summary;
    assert.equal(s.samples, 1);
    assert.equal(s.hangup_by, "customer");
    assert.ok(s.events.some((e) => e.ev === "wrapup_saved"), "the wrap-up save is not on the timeline");
    assert.equal(h.state.health, null, "the next call would start with this call's health");

    // a failed save sends nothing
    const f = await liveCall();
    f.call.fire("disconnect");
    f.h.rpcRoutes["/ariane_dialer/call/wrapup"] = () => {
        throw new Error("Write a note to finish");
    };
    await assert.rejects(f.h.dialer.wrapup({ outcome: "interested", note: "" }));
    assert.equal(healthPosts(f.h).length, 0);

    // control: preview mode measures nothing and sends nothing
    const p = await startService();
    p.state.preview = true;
    await p.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    assert.equal(p.state.health, null);
    await p.dialer.wrapup({ outcome: "interested", note: "abc" });
    assert.equal(healthPosts(p).length, 0);
});

// ------------------------------------------------------------------- the panel
async function panelWith(state, ui = {}) {
    const Panel = await loadPanel();
    const p = Object.create(Panel.prototype);
    p.state = { mode: "live", health: { dot: "green" }, isManager: false, ...state };
    p.ui = { healthHidden: false, healthOpen: false, ...ui };
    return p;
}

test("CH12: a rep can hide Call health; a manager always has it", async () => {
    const rep = await panelWith({});
    assert.equal(rep.healthShown, true);
    assert.equal(rep.healthHideable, true);
    rep.setHealthHidden(true);
    assert.equal(rep.healthShown, false);
    rep.setHealthHidden(false);
    assert.equal(rep.healthShown, true);

    const mgr = await panelWith({ isManager: true });
    assert.equal(mgr.healthHideable, false);
    mgr.setHealthHidden(true);
    assert.equal(mgr.healthShown, true, "a manager lost Call health");
    // even a hidden flag left from before (say, the same browser as a rep) does not hide it
    const mgr2 = await panelWith({ isManager: true }, { healthHidden: true });
    assert.equal(mgr2.healthShown, true);

    // control: no call measured, nothing shown; idle shows nothing
    assert.equal((await panelWith({ health: null })).healthShown, false);
    assert.equal((await panelWith({ mode: "idle" })).healthShown, false);

    // the service knows who is a manager
    const asRep = await startService({ groups: [USER] });
    assert.equal(asRep.state.isManager, false);
    const asMgr = await startService({ groups: [USER, MANAGER] });
    assert.equal(asMgr.state.isManager, true);
});

test("CH13: panel.xml binds the section, labels the round trip honestly and gates the hide button", () => {
    const start = PANEL_XML.indexOf("<!-- call health");
    assert.ok(start > 0, "no Call health section in panel.xml");
    const block = PANEL_XML.slice(start, PANEL_XML.indexOf('<div class="o_ariane_dialer_body">'));
    assert.match(block, /<section t-if="healthShown"/);
    assert.match(block, /<button t-if="healthHideable"[^>]*o_ad_health_hide/);
    for (const label of ["Twilio edge round trip", "API connection", "Jitter", "Packet loss", "MOS", "Coach answer time",
        "Click → authorised", "Ringing → answered", "not to the customer's phone"]) {
        assert.ok(block.includes(label), `label missing: ${label}`);
    }
    assert.ok(!/>\s*Ping\b/.test(block), "a bare 'Ping' label claims more than we measure");
    // every "round trip" the rep reads says it is to Twilio's edge (the grid row included)
    assert.match(block, /<span>Twilio edge round trip<\/span>/);
    const trips = [...block.matchAll(/([^>]{0,12})round trip([^<]{0,40})/gi)];
    assert.ok(trips.length >= 2, "round trip is not shown");
    for (const [all, before, after] of trips) {
        assert.ok(/Twilio edge $/.test(before) || /Twilio's edge/.test(after), `round trip without Twilio's edge: ${all}`);
    }
});
