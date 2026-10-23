// Catalogue names the customer said (22 Oct 2026: "Lumora candles"). The dialler
// service spots a brand, vendor or category in what was heard and sends a
// "mention" on the coach feed; the panel asks Odoo's own product search for that
// name, with the call's customer, and shows the cards under "Customer mentioned".
// Odoo decides what is shown and at what price: a product the customer may not
// buy is never sent (it is only counted), and a card with no price says so.
// The real service runs (dialer_service_harness.mjs). Run: `node --test ariane_dialer/static/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { deferred, flush, startService } from "./dialer_service_harness.mjs";

const dir = mkdtempSync(join(tmpdir(), "mentions-cv-"));
copyFileSync(fileURLToPath(new URL("../src/coach_view.js", import.meta.url)), join(dir, "coach_view.mjs"));
const cv = await import(pathToFileURL(join(dir, "coach_view.mjs")).href);
rmSync(dir, { recursive: true, force: true });

const PRICED = { id: 31, name: "Lumora Pillar Candle", code: "LPC", price: 12.5, currency: "USD", stock: { free: 40, uom: "Units" } };
const DROPSHIP = {
    id: 32, name: "Lumora Jar Candle", code: "LJC", price: null, price_note: "Price not set for this customer",
    stock: { free: 9, on_hand: 9, note: "Ships from the vendor · 9 at the vendor" },
};
const MENTION = { type: "mention", term: "Lumora", kind: "brand", heard: "loo more" };

async function demoCall(h) {
    await h.dialer.call({ phone: "+14155550101", partnerId: 5, demo: true });
    await flush();
    const ws = h.socket();
    assert.ok(ws, "the coach feed was not opened");
    ws.onopen?.();
    return ws;
}

const productAsks = (h) => h.rpcCalls.filter((c) => c.route === "/ariane_dialer/products");

// ------------------------------------------------------------ the card rules
test("M1: a card with no price says 'Price needed for this customer' (control: a priced card, and a price of 0, do not)", () => {
    const unpriced = cv.mentionCard(DROPSHIP);
    assert.equal(unpriced.price, null);
    assert.equal(unpriced.priceLabel, "Price needed for this customer");
    const priced = cv.mentionCard(PRICED);
    assert.equal(priced.price, 12.5);
    assert.equal(priced.priceLabel, "");
    assert.equal(cv.mentionCard({ ...PRICED, price: 0 }).priceLabel, "", "a price of 0 is a price");
});

test("M1: a drop-ship card says 'Ships from the vendor' (control: a card from the company's own shelves does not)", () => {
    assert.match(cv.mentionCard(DROPSHIP).ships, /^Ships from the vendor/);
    assert.equal(cv.mentionCard(PRICED).ships, "");
    assert.equal(cv.mentionCard({ ...PRICED, stock: { note: "Back in stock Friday" } }).ships, "");
});

test("M1: an odd card from the network is dropped, never a throw (control: a plain card is kept)", () => {
    for (const bad of [null, undefined, 7, "x", {}, { id: "31", name: "x" }, { id: 31 }, { id: 31, name: "" }]) {
        assert.equal(cv.mentionCard(bad), null, JSON.stringify(bad));
    }
    assert.equal(cv.mentionCard({ id: 31, name: "Plain" }).name, "Plain");
    assert.equal(cv.mentionCard({ id: 31, name: "Plain", price: "9.99" }).priceLabel, "Price needed for this customer");
});

// ------------------------------------------------------------ the service
test("M2: a mention asks Odoo's product search for the term with the call's customer, and shows the cards", async () => {
    const h = await startService();
    h.rpcRoutes["/ariane_dialer/products"] = () => ({ products: [PRICED, DROPSHIP], hidden: 2 });
    const ws = await demoCall(h);
    assert.deepEqual(h.state.mentions, [], "control: no mention, no cards");
    assert.equal(productAsks(h).length, 0);
    ws.emit(MENTION);
    await flush();
    const asks = productAsks(h);
    assert.equal(asks.length, 1);
    assert.equal(asks[0].params.query, "Lumora");
    assert.equal(asks[0].params.partner_id, 5);
    assert.equal(asks[0].params.kind, "brand", "the kind tells Odoo which match to rank first");
    const [m] = h.state.mentions;
    assert.equal(m.term, "Lumora");
    assert.equal(m.status, "ok");
    assert.equal(m.hidden, 2, "the products the customer cannot buy are counted, as /products counts them");
    assert.deepEqual(m.products.map((c) => [c.id, c.priceLabel]), [[31, ""], [32, "Price needed for this customer"]]);
    assert.match(m.products[1].ships, /^Ships from the vendor/);
});

test("M3: the same name twice asks once (control: a new name asks again); the newest is first, at most 3", async () => {
    const h = await startService();
    h.rpcRoutes["/ariane_dialer/products"] = () => ({ products: [PRICED], hidden: 0 });
    const ws = await demoCall(h);
    ws.emit(MENTION);
    ws.emit({ ...MENTION, term: "lumora" });           // a replay after a reconnect, any case
    await flush();
    assert.equal(productAsks(h).length, 1);
    for (const term of ["Whisko", "Pipes", "Candle Display"]) ws.emit({ ...MENTION, term, kind: "category" });
    await flush();
    assert.equal(productAsks(h).length, 4);
    assert.deepEqual(h.state.mentions.map((m) => m.term), ["Candle Display", "Pipes", "Whisko"]);
});

test("M4: an empty, odd or overlong name asks nothing (control: a plain one asks)", async () => {
    const h = await startService();
    const ws = await demoCall(h);
    for (const term of ["", " ", "x", null, 7, "B".repeat(41)]) ws.emit({ ...MENTION, term });
    await flush();
    assert.equal(productAsks(h).length, 0);
    assert.deepEqual(h.state.mentions, []);
    ws.emit(MENTION);
    ws.emit({ ...MENTION, term: "B".repeat(40) });       // control: the longest name Odoo signs
    await flush();
    assert.equal(productAsks(h).length, 2);
});

test("M5: a search that fails leaves the name with no cards, never a throw (control: the next name still asks)", async () => {
    const h = await startService();
    let n = 0;
    h.rpcRoutes["/ariane_dialer/products"] = () => {
        n += 1;
        if (n === 1) throw new Error("odoo down");
        return { products: [PRICED], hidden: 0 };
    };
    const ws = await demoCall(h);
    ws.emit(MENTION);
    await flush();
    assert.equal(h.state.mentions[0].status, "error");
    assert.deepEqual(h.state.mentions[0].products, []);
    ws.emit({ ...MENTION, term: "Whisko" });
    await flush();
    assert.equal(h.state.mentions[0].status, "ok");
});

test("M6: an answer that lands after the call was saved is not shown on the next one (control: in time it is)", async () => {
    const h = await startService();
    const slow = deferred();
    const next = deferred();
    const answers = [slow.promise, next.promise];
    h.rpcRoutes["/ariane_dialer/products"] = () => answers.shift();
    const ws = await demoCall(h);
    ws.emit(MENTION);
    await flush();
    assert.equal(h.state.mentions[0].status, "loading");
    await h.dialer.hangup();
    await h.dialer.wrapup({ outcome: "interested", note: "abc" });
    assert.deepEqual(h.state.mentions, [], "the saved call's names were not cleared");
    // the next call, another customer, hears the same name
    await h.dialer.call({ phone: "+14155550102", partnerId: 6, demo: true });
    await flush();
    const ws2 = h.socket();
    assert.notEqual(ws2, ws);
    ws2.onopen?.();
    ws2.emit(MENTION);
    await flush();
    assert.equal(productAsks(h)[1].params.partner_id, 6);
    slow.resolve({ products: [PRICED], hidden: 0 });         // the first customer's answer, late
    await flush();
    assert.equal(h.state.mentions.length, 1);
    assert.equal(h.state.mentions[0].status, "loading", "the first customer's cards were shown on the second call");
    assert.deepEqual(h.state.mentions[0].products, []);
    next.resolve({ products: [DROPSHIP], hidden: 0 });         // control: its own answer is shown
    await flush();
    assert.deepEqual(h.state.mentions[0].products.map((c) => c.id), [32]);
});

test("M7: a mention from a closed coach feed asks nothing (control: the open feed's does)", async () => {
    const h = await startService();
    const old = await demoCall(h);
    old.emit(MENTION);
    await flush();
    assert.equal(productAsks(h).length, 1);
    await h.dialer.hangup();
    old.emit({ type: "call_state", state: "ended", status: "completed" });
    old.emit({ ...MENTION, term: "Whisko" });
    await flush();
    assert.equal(productAsks(h).length, 1);
});

// ------------------------------------------------------------ the template (parse level)
const PANEL_XML = readFileSync(fileURLToPath(new URL("../src/panel.xml", import.meta.url)), "utf8");

function section(xml, marker) {
    const start = xml.indexOf(marker);
    assert.ok(start >= 0, `panel.xml has no ${marker}`);
    const open = xml.lastIndexOf("<section", start);
    const end = xml.indexOf("</section>", start);
    return xml.slice(open, end);
}

test("M8: panel.xml shows 'Customer mentioned: <term>' and each card's price label and vendor note, from fields the service sets", async () => {
    const block = section(PANEL_XML, "o_ariane_mentions");
    assert.match(block, /^<section t-if="state\.mode !== 'idle' and state\.mentions\.length"/);
    assert.match(block, /t-foreach="state\.mentions" t-as="m"/);
    assert.match(block, /<t t-if="m\.speaker === 'rep'">Rep said<\/t><t t-else="">Customer mentioned<\/t>: <t t-esc="m\.term"\/>/);
    assert.match(block, /t-foreach="m\.products" t-as="c"/);
    assert.match(block, /t-esc="c\.priceLabel"/);
    assert.match(block, /t-esc="c\.ships"/);
    assert.match(block, /t-if="c\.price !== null"/);
    // every field the template reads is one the service sets
    const h = await startService();
    h.rpcRoutes["/ariane_dialer/products"] = () => ({ products: [PRICED, DROPSHIP], hidden: 1 });
    const ws = await demoCall(h);
    ws.emit(MENTION);
    await flush();
    const m = h.state.mentions[0];
    const mFields = new Set([...block.matchAll(/\bm\.(\w+)/g)].map((x) => x[1]));
    const cFields = new Set([...block.matchAll(/\bc\.(\w+)/g)].map((x) => x[1]));
    assert.ok(mFields.size >= 3 && cFields.size >= 5, [...mFields, ...cFields].join());
    for (const f of mFields) assert.ok(f in m, `m.${f} is not set by the service`);
    for (const f of cFields) assert.ok(f in m.products[0], `c.${f} is not set by mentionCard`);
});

test("M9: a mention with no buyable match says which name found nothing, not the generic empty line", () => {
    const block = section(PANEL_XML, "o_ariane_mentions");
    const none = block.match(/<div t-elif="m\.status === 'none'"[^>]*>([\s\S]*?)<\/div>/);
    assert.ok(none, "no 'none' line in the mentions section");
    assert.equal(none[1], `No match for '<t t-esc="m.term"/>' that this customer can buy.`);
});

test("M10: a name the rep said is labelled 'Rep said'; when the customer then says it, the same card becomes theirs and nothing is asked again", async () => {
    const h = await startService();
    const ws = await demoCall(h);
    ws.emit({ ...MENTION, speaker: "rep" });
    await flush();
    assert.equal(h.state.mentions.length, 1);
    assert.equal(h.state.mentions[0].speaker, "rep");
    ws.emit({ ...MENTION, speaker: "rep" });                   // the rep again: nothing changes
    await flush();
    assert.equal(h.state.mentions[0].speaker, "rep");
    ws.emit({ ...MENTION, speaker: "customer" });
    await flush();
    assert.equal(h.state.mentions.length, 1);
    assert.equal(h.state.mentions[0].speaker, "customer");
    assert.equal(productAsks(h).length, 1, "the customer's mention of the same name asked Odoo again");
    ws.emit({ ...MENTION, speaker: "rep" });                   // once the customer's, never back to the rep's
    await flush();
    assert.equal(h.state.mentions[0].speaker, "customer");
});

test("M10: a mention with no speaker or an odd one is the customer's (control: 'rep' is the rep's)", async () => {
    const h = await startService();
    const ws = await demoCall(h);
    ws.emit(MENTION);
    ws.emit({ ...MENTION, term: "Whisko", speaker: 7 });
    ws.emit({ ...MENTION, term: "Pipewell", speaker: "rep" });
    await flush();
    const by = Object.fromEntries(h.state.mentions.map((m) => [m.term, m.speaker]));
    assert.deepEqual(by, { Lumora: "customer", Whisko: "customer", Pipewell: "rep" });
});
