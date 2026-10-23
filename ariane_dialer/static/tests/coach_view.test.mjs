// The coach card's plain rules (static/src/coach_view.js), checked on their
// own with Node's built-in runner: `node --test ariane_dialer/static/tests/`.
// tests/test_coach_view_js.py runs this file inside the Odoo test suite.
//
// coach_view.js is an Odoo module (`/** @odoo-module **/`, ESM `export`, a .js
// name). It imports nothing, so a copy named .mjs loads in plain Node 18+.
import { test } from "node:test";
import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const src = fileURLToPath(new URL("../src/coach_view.js", import.meta.url));
const dir = mkdtempSync(join(tmpdir(), "coach-view-"));
copyFileSync(src, join(dir, "coach_view.mjs"));
const cv = await import(pathToFileURL(join(dir, "coach_view.mjs")).href);
rmSync(dir, { recursive: true, force: true });

const T0 = Date.parse("2026-10-21T12:00:00Z");
const at = (ms) => new Date(T0 + ms).toISOString();
const pending = (passId, emittedMs = 0, question = true) => ({ question, pass_id: passId, emitted_at: at(emittedMs) });

// ------------------------------------------------------------ U5 / U9: acceptPending
test("U5: a customer question on a new pass is accepted and waits FILLER_WAIT_MS", () => {
    const g = cv.newPassGuard();
    assert.equal(cv.acceptPending(g, pending(7), T0), true);
    assert.deepEqual(g.pending, { passId: 7, until: T0 + cv.FILLER_WAIT_MS });
});

test("U5: not a question, no pass id or a bad emitted_at gives no filler", () => {
    for (const msg of [pending(7, 0, false), { question: true, emitted_at: at(0) },
        { question: true, pass_id: -1, emitted_at: at(0) }, { question: true, pass_id: 1.5, emitted_at: at(0) },
        { question: true, pass_id: 7, emitted_at: "not a date" }, null, undefined]) {
        const g = cv.newPassGuard();
        assert.equal(cv.acceptPending(g, msg, T0), false, JSON.stringify(msg));
        assert.equal(g.pending, null);
    }
});

test("U9: a replayed pending for a pass that already ended is ignored (control: the next pass is accepted)", () => {
    const g = cv.newPassGuard();
    assert.equal(cv.acceptPending(g, pending(7), T0), true);
    cv.settlePass(g, { pass_id: 7 });
    assert.equal(g.pending, null);
    assert.equal(cv.acceptPending(g, pending(7), T0 + 10), false);   // the replay after a reconnect
    assert.equal(cv.acceptPending(g, pending(6), T0 + 10), false);   // an older one
    assert.equal(g.pending, null);
    assert.equal(cv.acceptPending(g, pending(8), T0 + 10), true);    // control
});

test("U9: a pending older than FILLER_WAIT_MS by emitted_at is ignored (control: exactly at the limit is shown)", () => {
    const g = cv.newPassGuard();
    assert.equal(cv.acceptPending(g, pending(7, 0), T0 + cv.FILLER_WAIT_MS + 1), false);
    assert.equal(g.pending, null);
    assert.equal(cv.acceptPending(g, pending(7, 0), T0 + cv.FILLER_WAIT_MS), true);
    assert.equal(g.pending.until, T0 + cv.FILLER_WAIT_MS);
});

test("U9: a second pending for the same or an older pass does not restart the wait", () => {
    const g = cv.newPassGuard();
    assert.equal(cv.acceptPending(g, pending(7, 0), T0), true);
    assert.equal(cv.acceptPending(g, pending(7, 5000), T0 + 5000), false);
    assert.equal(cv.acceptPending(g, pending(5, 5000), T0 + 5000), false);
    assert.equal(g.pending.until, T0 + cv.FILLER_WAIT_MS);
    assert.equal(cv.acceptPending(g, pending(9, 5000), T0 + 5000), true);   // control: a later pass
    assert.equal(g.pending.passId, 9);
});

test("U9: an emitted_at in the future cannot stretch the wait past now + FILLER_WAIT_MS", () => {
    const g = cv.newPassGuard();
    assert.equal(cv.acceptPending(g, pending(3, 60000), T0), true);
    assert.equal(g.pending.until, T0 + cv.FILLER_WAIT_MS);
});

// ------------------------------------------------------------ U6 / U7: settlePass, expirePending
test("U6: the coach result for the waited pass, or a later one, ends the wait; an older one does not", () => {
    const g = cv.newPassGuard();
    cv.acceptPending(g, pending(7), T0);
    cv.settlePass(g, { pass_id: 6 });
    assert.equal(g.pending?.passId, 7);
    assert.equal(g.done, 6);
    cv.settlePass(g, { pass_id: 7 });
    assert.equal(g.pending, null);
    assert.equal(g.done, 7);
    cv.acceptPending(g, pending(8), T0);
    cv.settlePass(g, { pass_id: 9 });
    assert.equal(g.pending, null);
    cv.settlePass(g, { pass_id: 2 });
    assert.equal(g.done, 9);   // done never moves back
});

test("U6: an old service with no pass ids: any result ends the wait", () => {
    const g = cv.newPassGuard();
    cv.acceptPending(g, pending(7), T0);
    cv.settlePass(g, { say_next: "x" });
    assert.equal(g.pending, null);
    assert.equal(g.done, -1);
});

test("U7: the wait runs out at FILLER_WAIT_MS, not before", () => {
    const g = cv.newPassGuard();
    cv.acceptPending(g, pending(7), T0);
    assert.equal(cv.expirePending(g, T0 + cv.FILLER_WAIT_MS - 1), false);
    assert.equal(g.pending?.passId, 7);
    assert.equal(cv.expirePending(g, T0 + cv.FILLER_WAIT_MS), true);
    assert.equal(g.pending, null);
    assert.equal(cv.expirePending(g, T0 + cv.FILLER_WAIT_MS + 1), false);   // nothing left to expire
});

test("isStaleCoach: an older coach result never replaces the one on screen (control: same or newer does)", () => {
    const g = cv.newPassGuard();
    assert.equal(cv.isStaleCoach(g, { pass_id: 5 }), false);
    assert.equal(cv.isStaleCoach(g, { pass_id: 4 }), true);
    assert.equal(g.lastCoach, 5);
    assert.equal(cv.isStaleCoach(g, { pass_id: 5 }), false);
    assert.equal(cv.isStaleCoach(g, { pass_id: 6 }), false);
    assert.equal(cv.isStaleCoach(g, {}), false);   // no pass id: an old service, shown
    assert.equal(g.lastCoach, 6);
});

// ------------------------------------------------------------ U5 / U8: fillers
test("U5/U8: a filler shows only on a live call with the coach listening", () => {
    assert.equal(cv.fillerVisible({ filler: "Hold on", mode: "live", coachState: "live" }), true);
    for (const s of [{ filler: "", mode: "live", coachState: "live" },
        { filler: "Hold on", mode: "ended", coachState: "live" },
        { filler: "Hold on", mode: "live", coachState: "degraded" },
        { filler: "Hold on", mode: "live", coachState: "paused" }]) {
        assert.equal(cv.fillerVisible(s), false, JSON.stringify(s));
    }
});

test("U5: pickFiller never repeats the last line when there is a choice", () => {
    const list = ["A", "B", "C"];
    for (const r of [0, 0.34, 0.5, 0.99, 0.999999]) {
        assert.notEqual(cv.pickFiller(list, "A", () => r), "A", String(r));
        assert.ok(list.includes(cv.pickFiller(list, "A", () => r)));
    }
    assert.equal(cv.pickFiller(["A"], "A", () => 0.7), "A");   // one line: it repeats
    assert.equal(cv.pickFiller([], "A", () => 0.7), "");
});

test("cleanFillers: drops prices, counts, blanks, long lines and repeats; at most ten; defaults when empty", () => {
    const raw = ["Hold on one sec", "It is $20 today", "We have 3 left", "", "  One moment please  ",
        "Let me look, that is £5 off", "x".repeat(81), "Hold on one sec", 42, null, "Only € off"];
    assert.deepEqual(cv.cleanFillers(raw), ["Hold on one sec", "One moment please"]);
    assert.deepEqual(cv.cleanFillers(["Only 5 left", "€ off"]), cv.DEFAULT_FILLERS);
    assert.deepEqual(cv.cleanFillers(undefined), cv.DEFAULT_FILLERS);
    assert.notEqual(cv.cleanFillers(undefined), cv.DEFAULT_FILLERS);   // a copy, not the shared list
    const twelve = Array.from({ length: 12 }, (_, i) => `Phrase ${String.fromCharCode(65 + i)}`);
    assert.deepEqual(cv.cleanFillers(twelve), twelve.slice(0, 10));
    assert.equal(cv.cleanFillers(["y".repeat(80)]).length, 1);   // control: 80 characters is allowed
});

// ------------------------------------------------------------ U1-U3: hotStrip and badges
const priced = { 11: { price: 9.5, currency: "USD" } };

test("U1: an ok block gives ranked rows with names, prices and only clearance / new seller badges", () => {
    const strip = cv.hotStrip({
        status: "ok", state: " TX ", days: 30, items: [
            { id: 12, name: "Second", rank: 2, badges: ["hot", "new_seller"] },
            { id: 11, name: " First ", rank: 1, badges: ["clearance", "hot", "bogus"] },
        ],
    }, priced);
    assert.deepEqual(strip, {
        status: "ok", state: "TX", days: 30, items: [
            { id: 11, name: "First", rank: 1, badges: ["clearance"], price: 9.5, currency: "USD" },
            { id: 12, name: "Second", rank: 2, badges: ["new_seller"], price: null, currency: "" },
        ],
    });
});

test("U1: bad rows (rank out of 1..10, no name, no id, a repeated id) are dropped", () => {
    const strip = cv.hotStrip({
        status: "ok", state: "TX", days: 30, items: [
            { id: 1, name: "Ok", rank: 1 }, { id: 2, name: "Zero", rank: 0 }, { id: 3, name: "Eleven", rank: 11 },
            { id: 4, name: "", rank: 2 }, { name: "No id", rank: 3 }, { id: 1, name: "Again", rank: 4 },
            { id: 5, name: "Ten", rank: 10 }, null,
        ],
    });
    assert.deepEqual(strip.items.map((i) => [i.id, i.rank]), [[1, 1], [5, 10]]);
});

test("U2: a thin block is one muted line with the state; U3: no_state, off, no state or malformed is hidden", () => {
    assert.deepEqual(cv.hotStrip({ status: "thin", state: "TX", days: 30, items: [] }),
        { status: "thin", state: "TX", days: 30, items: [] });
    for (const hot of [{ status: "no_state", state: "", days: 30, items: [] }, { status: "off", state: "TX", days: 30, items: [] },
        { status: "ok", state: "", days: 30, items: [{ id: 1, name: "A", rank: 1 }] },
        { status: "ok", state: "TX", days: 30, items: "nope" }, { status: "ok", state: "TX", days: 30, items: [] },
        undefined, null, "ok"]) {
        assert.equal(cv.hotStrip(hot), null, JSON.stringify(hot));
    }
    assert.equal(cv.hotStrip({ status: "thin", state: "TX", days: -3 }).days, null);
});

test("badgesOf: allowlist in a fixed order, hot from a valid hot_rank or a bare mark", () => {
    assert.deepEqual(cv.badgesOf({ badges: ["new_seller", "clearance", "x"], hot_rank: 2 }),
        [{ kind: "clearance" }, { kind: "new_seller" }, { kind: "hot", rank: 2 }]);
    assert.deepEqual(cv.badgesOf({ badges: ["hot"], hot_rank: 99 }), [{ kind: "hot", rank: null }]);
    assert.deepEqual(cv.badgesOf({ hot_rank: 0 }), []);
    assert.deepEqual(cv.badgesOf({ badges: "clearance" }), []);
    assert.deepEqual(cv.badgesOf(null), []);
});

test("pricesFromContext: reads id -> price from the signed list; garbage gives no prices", () => {
    const claims = { products: [{ id: 11, price: 9.5, currency: "USD" }, { id: 12, price: "9" }, { id: "x", price: 1 }] };
    const b64 = Buffer.from(JSON.stringify(claims)).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    assert.deepEqual(cv.pricesFromContext(`h.${b64}.s`), { 11: { price: 9.5, currency: "USD" } });
    assert.deepEqual(cv.pricesFromContext("h.!!!.s"), {});
    assert.deepEqual(cv.pricesFromContext(undefined), {});
});
