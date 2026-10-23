/** @odoo-module **/
// What the coach card shows, as plain functions: badges, the "Selling in
// {state}" strip, the filler lines and the pass-id guard. No Odoo imports, so
// the rules can be checked on their own. Every input comes from the network:
// a missing or odd field means no badge, no strip or no filler, never a throw.

export const BADGES = ["clearance", "new_seller"]; // allowlist, in display order
export const MAX_HOT_RANK = 10;
export const FILLER_WAIT_MS = 25000; // the dialler service's coach timeout
export const SEARCH_FILLER_MS = 600; // a product search slower than this gets a filler
const MAX_FILLERS = 10;
const MAX_FILLER_LEN = 80;
// A filler can never carry a price: a line with a digit or a currency sign is dropped.
const PRICE_LIKE = /[\d\p{Sc}]/u;
export const DEFAULT_FILLERS = [
    "Give me a moment to check that for you.",
    "Let me pull up those details.",
    "That's a good question, let me confirm.",
];

function isRank(n) {
    return Number.isInteger(n) && n >= 1 && n <= MAX_HOT_RANK;
}

function isPassId(n) {
    return Number.isInteger(n) && n >= 0;
}

// The badges of one product line, a recommendation or a strip row. Only Odoo's
// own allowlisted values count, in a fixed order; "hot" comes from hot_rank
// (or a bare "hot" mark without a rank).
export function badgesOf(item) {
    const marks = Array.isArray(item?.badges) ? item.badges : [];
    const out = BADGES.filter((kind) => marks.includes(kind)).map((kind) => ({ kind }));
    const rank = item?.hot_rank;
    if (isRank(rank)) out.push({ kind: "hot", rank });
    else if (marks.includes("hot")) out.push({ kind: "hot", rank: null });
    return out;
}

// id -> {price, currency} from the signed product list the browser already
// carries. Display only: the dialler service checks the signature, not us.
export function pricesFromContext(token) {
    const out = {};
    try {
        const part = String(token || "").split(".")[1];
        if (!part) return out;
        const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
        const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
        const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
        for (const p of Array.isArray(claims?.products) ? claims.products : []) {
            if (Number.isInteger(p?.id) && typeof p.price === "number") {
                out[p.id] = { price: p.price, currency: typeof p.currency === "string" ? p.currency : "" };
            }
        }
    } catch {
        // an unreadable list: the strip shows no prices
    }
    return out;
}

// The strip from coach_context's 'hot' block: null (hidden), 'thin' (one muted
// line) or 'ok' with ranked rows. 'no_state', 'off', missing or malformed: hidden.
export function hotStrip(hot, prices = {}) {
    if (!hot || typeof hot !== "object") return null;
    const state = typeof hot.state === "string" ? hot.state.trim() : "";
    if (!state) return null;
    const days = Number.isInteger(hot.days) && hot.days > 0 ? hot.days : null;
    if (hot.status === "thin") return { status: "thin", state, days, items: [] };
    if (hot.status !== "ok" || !Array.isArray(hot.items)) return null;
    const seen = new Set();
    const items = hot.items
        .filter((i) => i && Number.isInteger(i.id) && isRank(i.rank) && typeof i.name === "string" && i.name.trim())
        .sort((a, b) => a.rank - b.rank)
        .filter((i) => !seen.has(i.id) && seen.add(i.id))
        .map((i) => ({
            id: i.id,
            name: i.name.trim(),
            rank: i.rank,
            // the rank is the row's own number; only clearance / new seller go on it
            badges: badgesOf({ badges: i.badges }).filter((b) => b.kind !== "hot").map((b) => b.kind),
            price: prices[i.id]?.price ?? null,
            currency: prices[i.id]?.currency || "",
        }));
    return items.length ? { status: "ok", state, days, items } : null;
}

// The tenant's filler lines from session(), cleaned; the three defaults when
// there are none left.
export function cleanFillers(raw) {
    const out = [];
    for (const f of Array.isArray(raw) ? raw : []) {
        if (typeof f !== "string") continue;
        const s = f.trim();
        if (!s || s.length > MAX_FILLER_LEN || PRICE_LIKE.test(s) || out.includes(s)) continue;
        out.push(s);
        if (out.length >= MAX_FILLERS) break;
    }
    return out.length ? out : [...DEFAULT_FILLERS];
}

// A filler, never the same one twice in a row (when there is a choice).
export function pickFiller(list, last, rand = Math.random) {
    const pool = list.length > 1 ? list.filter((f) => f !== last) : list;
    return pool[Math.min(pool.length - 1, Math.floor(rand() * pool.length))] || "";
}

// ------------------------------------------------------------ pass-id guard
// One guard per coach feed. 'done' is the newest pass that ended (coach or
// coach_idle); 'pending' is the pass the rep is waiting on, if any.
export function newPassGuard() {
    return { done: -1, lastCoach: -1, pending: null };
}

// coach_pending: shown only for a customer question, a pass not yet ended, and
// an event at most FILLER_WAIT_MS old. A replay after a reconnect fails one of these.
export function acceptPending(guard, msg, now) {
    if (msg?.question !== true || !isPassId(msg.pass_id)) return false;
    if (msg.pass_id <= guard.done) return false;
    if (guard.pending && msg.pass_id <= guard.pending.passId) return false;
    const at = Date.parse(msg.emitted_at);
    if (!Number.isFinite(at) || now - at > FILLER_WAIT_MS) return false;
    guard.pending = { passId: msg.pass_id, until: Math.min(at, now) + FILLER_WAIT_MS };
    return true;
}

// coach / coach_idle: the pass ended. Ends the wait when it is the pass waited
// on or a later one. An old service sends no pass ids: any result ends the wait.
export function settlePass(guard, msg) {
    const id = msg?.pass_id;
    if (!isPassId(id)) {
        guard.pending = null;
        return;
    }
    guard.done = Math.max(guard.done, id);
    if (guard.pending && id >= guard.pending.passId) guard.pending = null;
}

// A coach result older than the one on screen never replaces it.
export function isStaleCoach(guard, msg) {
    const id = msg?.pass_id;
    if (!isPassId(id)) return false;
    if (id < guard.lastCoach) return true;
    guard.lastCoach = id;
    return false;
}

// The wait ran out with no answer: the previous line comes back.
export function expirePending(guard, now) {
    if (guard.pending && now >= guard.pending.until) {
        guard.pending = null;
        return true;
    }
    return false;
}

// A filler shows only on a live call with the coach listening (U5, U8).
export function fillerVisible(state) {
    return Boolean(state.filler) && state.mode === "live" && state.coachState === "live";
}

// ------------------------------------------------------------ customer mentioned
// A catalogue name the customer said (a "mention" on the coach feed) brings up
// that name's products from Odoo's own search (/ariane_dialer/products). Odoo
// decides which products and at what price: one the customer may not buy is
// never sent, only counted. A card with no price says so, never a guess.
export const MAX_MENTIONS = 3;
export const MENTION_TERM_LEN = 40; // the longest name Odoo signs (VOCAB_TERM_LEN)
export const MENTION_CARDS = 3;
export const PRICE_NEEDED = "Price needed for this customer";
export const SHIPS_FROM_VENDOR = "Ships from the vendor";

// One product card from the search, cut to what the mention card shows; null
// for anything that is not a card.
export function mentionCard(p) {
    if (!p || typeof p !== "object" || !Number.isInteger(p.id)) return null;
    const name = typeof p.name === "string" ? p.name.trim() : "";
    if (!name) return null;
    const priced = typeof p.price === "number" && Number.isFinite(p.price);
    const note = typeof p.stock?.note === "string" ? p.stock.note : "";
    const compliance = p.compliance && p.compliance.status !== "allowed" && typeof p.compliance.text === "string"
        ? p.compliance.text : "";
    return {
        id: p.id,
        name,
        code: typeof p.code === "string" ? p.code : "",
        price: priced ? p.price : null,
        currency: priced && typeof p.currency === "string" ? p.currency : "",
        priceLabel: priced ? "" : PRICE_NEEDED,
        ships: note.startsWith(SHIPS_FROM_VENDOR) ? note : "",
        compliance,
    };
}
