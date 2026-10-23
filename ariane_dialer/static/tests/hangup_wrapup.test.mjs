// Ending a call and finishing it (owner request, 21 Oct 2026):
// H1-H4  Hang up works while the call is still connecting, so a call is never
//        placed (and charged) after the rep pressed Hang up.
// N1-N3  The rep cannot finish a call without a written note.
// R1-R3  A page reload does not skip the wrap-up: the rep's own unfinished
//        call comes back into the panel.
// The real dialer_service.js / panel.js run with stubbed Odoo imports
// (dialer_service_harness.mjs). Run: `node --test ariane_dialer/static/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { deferred, flush, loadPanel, startService } from "./dialer_service_harness.mjs";

const PHONE = "+14155550101";
const PANEL_XML = readFileSync(fileURLToPath(new URL("../src/panel.xml", import.meta.url)), "utf8");

function wrapupPosted(h, callId) {
    return h.rpcCalls.some((c) => c.route === "/ariane_dialer/call/update"
        && c.params.call_id === callId && c.params.state === "wrapup");
}

// ------------------------------------------------------------ hang up while connecting
test("H1: Hang up while the call is being authorised: nothing is dialled, the call goes to wrap-up", async () => {
    const h = await startService();
    const auth = deferred();
    h.apiRoutes["/v1/dialer/authorize"] = () => auth.promise;
    const run = h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    await flush();
    assert.equal(h.state.mode, "connecting");
    await h.dialer.hangup();
    assert.equal(h.state.mode, "wrapup");
    auth.resolve({ authorization_id: "auth", live: false });
    await run;
    assert.equal(h.phoneCalls.length, 0, "the phone dialled after the rep hung up");
    assert.equal(h.state.mode, "wrapup");
    assert.ok(wrapupPosted(h, 77), "the call record was not moved to wrap-up");
});

test("H2: Hang up while the phone is connecting: the call it then places is cut at once", async () => {
    const conn = deferred();
    const placed = {
        parameters: { CallSid: "CA-late" }, disconnected: false,
        on() {}, mute() {}, disconnect() { this.disconnected = true; },
    };
    const h = await startService({ connect: () => conn.promise });
    const run = h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    await flush();
    assert.equal(h.state.mode, "connecting");
    await h.dialer.hangup();
    conn.resolve(placed);
    await run;
    assert.equal(placed.disconnected, true, "the call placed after Hang up was left running");
    assert.equal(h.state.mode, "wrapup", "the panel went back to a live call after Hang up");
});

test("H3: Hang up before the call record exists: the record still goes to wrap-up and nothing is dialled", async () => {
    const h = await startService();
    const started = deferred();
    h.rpcRoutes["/ariane_dialer/call/start"] = () => started.promise;
    const run = h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    await flush();
    await h.dialer.hangup();
    assert.equal(h.state.mode, "wrapup");
    started.resolve({ call_id: 88 });
    await run;
    assert.equal(h.state.callId, 88);
    assert.ok(wrapupPosted(h, 88), "the call record was left in 'dialling'");
    assert.equal(h.apiCalls.filter((c) => c.path === "/v1/dialer/authorize").length, 0, "authorised after Hang up");
    assert.equal(h.phoneCalls.length, 0);
});

test("H4: control: a call nobody hangs up is dialled and rings", async () => {
    const h = await startService();
    await h.dialer.call({ phone: PHONE, partnerId: 5, demo: false });
    assert.equal(h.phoneCalls.length, 1);
    assert.equal(h.state.mode, "ringing");
    await h.dialer.hangup();   // with a call object: Twilio's own disconnect
    assert.equal(h.phoneCalls[0].disconnected, true);
});

test("H5: panel.xml shows Hang up while connecting (Mute and Keypad only once there is a call)", () => {
    const start = PANEL_XML.indexOf('class="o_ad_controls"');
    assert.ok(start > 0, "panel.xml has no controls row");
    const open = PANEL_XML.lastIndexOf("<div", start);
    const row = PANEL_XML.slice(open, PANEL_XML.indexOf("</div>", start));
    assert.match(row.slice(0, row.indexOf(">")), /state\.mode === 'connecting'/, "the controls row is hidden while connecting");
    const end = row.slice(row.indexOf("o_ad_ctl_end") - 40);
    assert.match(end, /^[^<]*<button class="o_ad_ctl o_ad_ctl_end" t-on-click="\(\) => dialer\.hangup\(\)">/,
        "the Hang up button is gated or not wired to hangup()");
    for (const ctl of ["toggleMute", "this.ui.keypad = !this.ui.keypad"]) {
        const at = row.indexOf(ctl);
        const tag = row.slice(row.lastIndexOf("<button", at), at);
        assert.match(tag, /state\.mode !== 'connecting'/, `${ctl} shows before there is a call`);
    }
});

// ------------------------------------------------------------ a note is needed to finish
async function panelWith(ui, state = {}) {
    const Panel = await loadPanel();
    const p = Object.create(Panel.prototype);
    p.state = { outcomes: [{ code: "interested" }, { code: "callback", needs_followup: true }], callId: 77, ...state };
    p.ui = { outcome: "interested", note: "", followUp: "", closureReason: "", saving: false, ...ui };
    return p;
}

test("N1: Save stays off until the rep writes a note (blank, spaces or 2 letters are not a note)", async () => {
    for (const note of ["", "   ", " ab ", "\n\n"]) {
        assert.equal((await panelWith({ note })).canSave, false, `saved with note ${JSON.stringify(note)}`);
    }
    assert.equal((await panelWith({ note: "Wants 10 cases" })).canSave, true);
    assert.equal((await panelWith({ note: "abc" })).canSave, true, "3 letters is the minimum");
});

test("N2: control: the other rules still hold with a note (outcome, follow-up, a call to save)", async () => {
    const note = "Wants 10 cases";
    assert.equal((await panelWith({ note, outcome: "" })).canSave, false);
    assert.equal((await panelWith({ note, outcome: "callback" })).canSave, false);
    assert.equal((await panelWith({ note, outcome: "callback", followUp: "2030-01-01T10:00" })).canSave, true);
    assert.equal((await panelWith({ note, saving: true })).canSave, false);
    assert.equal((await panelWith({ note }, { callId: null })).canSave, false);
});

test("N3: panel.xml tells the rep why Save is off: 'Write a note to finish'", async () => {
    const wrap = PANEL_XML.slice(PANEL_XML.indexOf("<!-- wrap-up -->"));
    const hint = wrap.match(/<div t-if="([^"]+)"[^>]*>Write a note to finish<\/div>/);
    assert.ok(hint, "no 'Write a note to finish' hint in the wrap-up");
    assert.match(hint[1], /!noteOk/);
    const blank = await panelWith({ note: " " });
    assert.equal(blank.noteOk, false);
    assert.equal((await panelWith({ note: "Wants 10 cases" })).noteOk, true);
});

// ------------------------------------------------------------ reload does not skip the wrap-up
const PENDING = { call_id: 91, phone: PHONE, partner_id: 5, lead_id: false, state: "wrapup" };

test("R1: on start, the rep's own unfinished call comes back into the wrap-up", async () => {
    const h = await startService({ rpc: { "/ariane_dialer/call/pending": () => PENDING } });
    assert.equal(h.state.mode, "wrapup", "the reloaded page skipped the wrap-up");
    assert.equal(h.state.callId, 91);
    assert.equal(h.state.partnerId, 5);
    assert.equal(h.state.open, true);
    // the rep cannot start another call before finishing it
    await h.dialer.call({ phone: "+14155550102", partnerId: 6, demo: false });
    assert.equal(h.phoneCalls.length, 0);
    assert.equal(h.state.callId, 91);
});

test("R2: a call left 'live' by the reload is moved to wrap-up on the server too", async () => {
    const h = await startService({ rpc: { "/ariane_dialer/call/pending": () => ({ ...PENDING, state: "live" }) } });
    assert.equal(h.state.mode, "wrapup");
    assert.ok(wrapupPosted(h, 91));
});

test("R3: control: nothing unfinished leaves the panel idle; opening the panel checks again", async () => {
    let pending = null;
    const h = await startService({ rpc: { "/ariane_dialer/call/pending": () => pending } });
    assert.equal(h.state.mode, "idle");
    assert.equal(h.state.callId, null);
    pending = PENDING;   // e.g. another tab ended a call
    h.dialer.toggle();
    await flush();
    assert.equal(h.state.mode, "wrapup");
    assert.equal(h.state.callId, 91);
});
