// How dialer_service.js USES the coach card's rules (coach_view.js): the slow
// product search filler, the coach_pending / coach / coach_idle handlers on the
// coach feed, and the "Selling in {state}" strip on a call that fails to start.
// The real service runs, with stubbed Odoo imports, network, phone and a hand
// moved clock (dialer_service_harness.mjs). Run: `node --test ariane_dialer/static/tests/`.
// Also: the panel template binds to the state fields the service sets.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { deferred, flush, HOT_BLOCK, startService } from "./dialer_service_harness.mjs";

const FILLERS = ["Hold on one sec", "One moment please"];
const SEARCH_FILLER_MS = 600;
const FILLER_WAIT_MS = 25000;
const iso = (ms) => new Date(ms).toISOString();

// A demo call (the AI rep) is the simplest way to get a live coach feed open.
async function demoCall(h) {
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: true });
    await flush();
    const ws = h.socket();
    assert.ok(ws, "the coach feed was not opened");
    ws.onopen?.();
    return ws;
}

const pending = (h, passId) => ({ type: "coach_pending", question: true, pass_id: passId, emitted_at: iso(h.clock.now) });

// ------------------------------------------------------------ slow search filler
test("search: a filler from the tenant's list appears once the search passes SEARCH_FILLER_MS, and goes when it lands", async () => {
    const h = await startService({ fillers: FILLERS });
    const slow = deferred();
    h.rpcRoutes["/ariane_dialer/products"] = () => slow.promise;
    const run = h.dialer.search("pipe");
    await h.clock.advance(SEARCH_FILLER_MS - 1);
    assert.equal(h.state.filler, "", "a filler before the search was slow");
    await h.clock.advance(1);
    assert.ok(FILLERS.includes(h.state.filler), `filler ${JSON.stringify(h.state.filler)} is not the tenant's`);
    slow.resolve({ products: [{ id: 1 }], hidden: 0 });
    await run;
    assert.equal(h.state.filler, "");
    assert.deepEqual(h.state.results, [{ id: 1 }]);
});

test("search: a fast search never shows a filler (control for the one above)", async () => {
    const h = await startService({ fillers: FILLERS });
    const fast = deferred();
    h.rpcRoutes["/ariane_dialer/products"] = () => fast.promise;
    const run = h.dialer.search("pipe");
    await h.clock.advance(SEARCH_FILLER_MS - 100);
    fast.resolve({ products: [], hidden: 0 });
    await run;
    await h.clock.advance(5 * SEARCH_FILLER_MS);
    assert.equal(h.state.filler, "");
});

test("search: a newer search replaces the older one's wait; the older timer gives no filler", async () => {
    const h = await startService({ fillers: FILLERS });
    const first = deferred();
    const second = deferred();
    const answers = [first.promise, second.promise];
    h.rpcRoutes["/ariane_dialer/products"] = () => answers.shift();
    const runA = h.dialer.search("pipe");
    await h.clock.advance(300);
    const runB = h.dialer.search("pipes");
    await h.clock.advance(SEARCH_FILLER_MS - 300);   // the first search's timer is due now
    assert.equal(h.state.filler, "", "the older search's timer showed a filler");
    await h.clock.advance(300);                       // the second search is slow now
    assert.ok(FILLERS.includes(h.state.filler));
    first.resolve({ products: [{ id: 1 }], hidden: 0 });
    await runA;
    assert.ok(FILLERS.includes(h.state.filler), "the older search's answer ended the newer one's wait");
    assert.deepEqual(h.state.results, [], "the older search's answer was shown");
    second.resolve({ products: [{ id: 2 }], hidden: 0 });
    await runB;
    assert.equal(h.state.filler, "");
    assert.deepEqual(h.state.results, [{ id: 2 }]);
});

// ------------------------------------------------------------ coach feed handlers
test("coach_pending shows a filler; the coach result for that pass ends it and is shown", async () => {
    const h = await startService({ fillers: FILLERS });
    const ws = await demoCall(h);
    assert.equal(h.state.filler, "");
    ws.emit(pending(h, 3));
    assert.ok(FILLERS.includes(h.state.filler));
    ws.emit({ type: "coach", pass_id: 3, say_next: "Ask about the 2kg" });
    assert.equal(h.state.filler, "");
    assert.equal(h.state.coach.say_next, "Ask about the 2kg");
});

test("coach_pending: not a question gives no filler (control: a question does)", async () => {
    const h = await startService({ fillers: FILLERS });
    const ws = await demoCall(h);
    ws.emit({ ...pending(h, 3), question: false });
    assert.equal(h.state.filler, "");
    ws.emit(pending(h, 3));
    assert.ok(FILLERS.includes(h.state.filler));
});

test("coach_idle for the waited pass ends the filler; an older pass's idle or coach does not", async () => {
    const h = await startService({ fillers: FILLERS });
    const ws = await demoCall(h);
    ws.emit({ type: "coach", pass_id: 1, say_next: "First" });
    ws.emit(pending(h, 4));
    const shown = h.state.filler;
    assert.ok(FILLERS.includes(shown));
    ws.emit({ type: "coach_idle", pass_id: 2 });
    assert.equal(h.state.filler, shown, "an older pass's idle ended the wait");
    ws.emit({ type: "coach", pass_id: 0, say_next: "Stale" });
    assert.equal(h.state.coach.say_next, "First", "an older coach result replaced the newer one");
    assert.equal(h.state.filler, shown);
    ws.emit({ type: "coach_idle", pass_id: 4 });
    assert.equal(h.state.filler, "");
    assert.equal(h.state.coach.say_next, "First");   // idle changes no advice
});

test("a pending that gets no answer runs out at FILLER_WAIT_MS, not before", async () => {
    const h = await startService({ fillers: FILLERS });
    const ws = await demoCall(h);
    ws.emit(pending(h, 5));
    await h.clock.advance(FILLER_WAIT_MS - 1);
    assert.ok(FILLERS.includes(h.state.filler));
    await h.clock.advance(1);
    assert.equal(h.state.filler, "");
});

test("a pending replayed after its pass ended is ignored (control: the next pass shows a filler)", async () => {
    const h = await startService({ fillers: FILLERS });
    const ws = await demoCall(h);
    ws.emit(pending(h, 6));
    ws.emit({ type: "coach", pass_id: 6, say_next: "x" });
    assert.equal(h.state.filler, "");
    ws.emit(pending(h, 6));   // the replay after a reconnect
    assert.equal(h.state.filler, "");
    ws.emit(pending(h, 7));
    assert.ok(FILLERS.includes(h.state.filler));
});

test("a new pending never repeats the previous filler line", async () => {
    const h = await startService({ fillers: FILLERS });
    const ws = await demoCall(h);
    const seen = [];
    // a fixed "random": the first line every time, unless the service holds it back
    const random = Math.random;
    Math.random = () => 0;
    try {
        for (let pass = 1; pass <= 6; pass++) {
            ws.emit(pending(h, pass));
            seen.push(h.state.filler);
            ws.emit({ type: "coach_idle", pass_id: pass });
        }
    } finally {
        Math.random = random;
    }
    for (let i = 1; i < seen.length; i++) assert.notEqual(seen[i], seen[i - 1], JSON.stringify(seen));
});

test("a message from a closed coach feed changes nothing (control: the open feed's does)", async () => {
    const h = await startService({ fillers: FILLERS });
    const old = await demoCall(h);
    old.emit(pending(h, 2));
    assert.ok(FILLERS.includes(h.state.filler));
    await h.dialer.hangup();
    // the demo's own "ended" closes the feed and ends the wait
    old.emit({ type: "call_state", state: "ended", status: "completed" });
    assert.equal(h.state.filler, "");
    old.emit(pending(h, 9));
    assert.equal(h.state.filler, "");
});

// ------------------------------------------------------------ the hot strip on a failed start
test("demo call: the strip shows when the AI rep call starts (control)", async () => {
    const h = await startService();
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: true });
    assert.equal(h.state.mode, "ringing");
    assert.equal(h.state.hot?.state, "TX");
    assert.deepEqual(h.state.hot.items.map((i) => [i.id, i.price]), [[11, 9.5]]);
});

test("demo call: a failed start clears the strip it had already set", async () => {
    const h = await startService();
    h.apiRoutes["/v1/dialer/demo-call"] = () => {
        throw Object.assign(new Error("number not allowed"), { status: 403 });
    };
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: true });
    assert.equal(h.state.mode, "wrapup");
    assert.match(h.state.blocked, /^Not dialled: /);
    assert.equal(h.state.hot, null, "the hot strip stayed up for a call that was never placed");
});

test("live call: the strip shows when the coached call is placed (control)", async () => {
    const h = await startService({ liveCoach: true });
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: false });
    assert.equal(h.state.mode, "ringing");
    assert.equal(h.state.hot?.state, HOT_BLOCK.state);
    assert.equal(h.phoneCalls.length, 1);
});

test("live call: a phone that fails to connect clears the strip", async () => {
    const h = await startService({
        liveCoach: true,
        connect: () => {
            throw new Error("microphone blocked");
        },
    });
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: false });
    assert.equal(h.state.mode, "wrapup");
    assert.match(h.state.blocked, /^Could not dial: /);
    assert.equal(h.state.hot, null, "the hot strip stayed up for a call that was never placed");
});

// ------------------------------------------------------------ template wiring (parse level)
const PANEL_XML = readFileSync(fileURLToPath(new URL("../src/panel.xml", import.meta.url)), "utf8");
const PANEL_JS = readFileSync(fileURLToPath(new URL("../src/panel.js", import.meta.url)), "utf8");

function section(xml, marker) {
    const start = xml.indexOf(marker);
    assert.ok(start >= 0, `panel.xml has no ${marker}`);
    const open = xml.lastIndexOf("<section", start);
    const end = xml.indexOf("</section>", start);
    return xml.slice(open, end);
}

test("panel.xml: the filler card shows state.filler, gated by fillerVisible(state)", () => {
    const filler = section(PANEL_XML, "o_ad_say_filler");
    assert.match(filler, /^<section t-if="fillerShown"/);
    assert.match(filler, /t-esc="state\.filler"/);
    assert.match(PANEL_JS, /get fillerShown\(\)\s*{\s*return fillerVisible\(this\.state\);/);
    assert.match(PANEL_JS, /this\.state = useState\(this\.dialer\.state\);/);
});

test("panel.xml: the hot strip reads only fields the service's state.hot has", async () => {
    const h = await startService();
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: true });
    const hot = h.state.hot;
    const strip = section(PANEL_XML, "o_ariane_hot");
    assert.match(strip, /^<section t-if="state\.mode !== 'idle' and state\.hot"/);
    assert.match(strip, /t-foreach="state\.hot\.items" t-as="h"/);
    const stripFields = new Set([...strip.matchAll(/state\.hot\.(\w+)/g)].map((m) => m[1]));
    const rowFields = new Set([...strip.matchAll(/\bh\.(\w+)/g)].map((m) => m[1]));
    assert.deepEqual([...stripFields].sort(), ["days", "items", "state", "status"]);
    for (const f of stripFields) assert.ok(f in hot, `state.hot.${f} is not set by the service`);
    assert.ok(rowFields.size >= 4, [...rowFields].join());
    for (const f of rowFields) assert.ok(f in hot.items[0], `h.${f} is not set by the service`);
});
