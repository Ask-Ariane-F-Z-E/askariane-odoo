/** @odoo-module **/
// Dialler state and actions, shared by the systray button, the side panel and
// the phone-field call buttons. Telephony and the live coach come from the
// Ask Ariane dialler service; without it the panel runs in preview mode.
import { reactive } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { loadJS } from "@web/core/assets";
import { user } from "@web/core/user";
import { _t } from "@web/core/l10n/translation";
import {
    acceptPending, badgesOf, cleanFillers, expirePending, FILLER_WAIT_MS, hotStrip, isStaleCoach,
    MAX_MENTIONS, MENTION_CARDS, MENTION_TERM_LEN, mentionCard, newPassGuard, pickFiller, pricesFromContext,
    SEARCH_FILLER_MS, settlePass,
} from "./coach_view";
import { API_PING_MS, createHealth } from "./call_health";

const SDK_URL = "/ariane_dialer/static/lib/twilio/twilio.min.js";
const CARD_MAX_AGE_MS = 4000;
const FINAL_NOTES_WAIT_MS = 120000;

export function toE164(raw) {
    const s = (raw || "").trim();
    const digits = s.replace(/\D/g, "");
    if (s.startsWith("+")) return "+" + digits;
    if (digits.length === 10) return "+1" + digits;
    if (digits.length === 11 && digits.startsWith("1")) return "+" + digits;
    return digits ? "+" + digits : "";
}

function many2oneId(value) {
    return Array.isArray(value) ? value[0] : value?.id || null;
}

export const dialerService = {
    dependencies: ["notification"],
    async start(env, { notification }) {
        const state = reactive({
            enabled: false,
            open: false,
            mode: "idle", // idle | connecting | ringing | live | wrapup
            preview: true,
            backend: null, // {api_url, rep_token} once connected
            error: "",
            phone: "",
            partnerId: null,
            leadId: null,
            callId: null,
            startedAt: null,
            muted: false,
            info: null,
            query: "",
            results: [],
            hidden: 0, // products the search found but this customer may not buy
            searching: false,
            outcomes: [], // the tenant's own outcomes, from the server
            quote: null,
            cards: [],
            coachState: "off", // off | live | degraded | paused
            blocked: "",
            ringingIn: false,
            // AI-rep demo (test mode only): a voice agent calls a test number and
            // the coach runs on that call. The server refuses it outside test mode.
            demoMode: false,
            demoCallSid: null,
            transcript: [], // [{speaker: rep|customer, text, start}]
            coach: null, // {say_next, ask_next, notes, recommendations, flags, summary?, next_action?}
            callStatus: "",
            savedToOdoo: null,
            // The rep's own call is transcribed live (test mode only): the coach
            // stays open after hang-up until "ended" brings the final notes.
            liveCoach: false,
            shopLinks: {}, // product id -> its page on the website, from Odoo (never from the coach)
            demoAllowed: false, // may start the AI-rep demo from a phone field
            // Power dialler: the rep's own calls due now, from the server. Skips
            // last for this page only; nothing is written when a rep skips.
            queue: [],
            queueLoading: false,
            skipped: [],
            queueKey: null, // the list row the call on screen came from
            // "Selling in {state}": what sells in the customer's delivery state,
            // from Odoo with the coach list; only while the live coach is on.
            hot: null, // null | {status: ok|thin, state, days, items}
            // Catalogue names the customer said, newest first: Odoo's own search
            // for each, with this customer ({term, kind, heard, status, products, hidden}).
            mentions: [],
            // A line for the rep to say while the coach or a product search works.
            filler: "",
            // Call health (call_health.js): what the "Call health" section shows,
            // during the call and through wrap-up. null = no call measured.
            health: null,
            isManager: false, // managers can always open Call health
        });
        let device = null;
        let incomingCall = null;
        let activeCall = null;
        let socket = null;
        let lastSeq = 0;
        // Each openCoach() is one generation; a socket or retry timer from an
        // older call can never write into the panel of a newer one.
        let coachGen = 0;
        let retryTimer = null;
        let searchSeq = 0;
        let mentionSeq = 0; // each mention's own id: a late answer finds only its own entry
        let queueLoad = null; // the call-list read in flight
        let queueStale = false; // a reload was asked for while one was in flight
        // The tenant's filler lines (session()), or the defaults until it answers.
        let fillers = cleanFillers(null);
        let lastFiller = "";
        let passGuard = newPassGuard(); // per coach feed: which pass the rep waits on
        let pendingTimer = null;
        let searchSlow = false; // a product search has taken over SEARCH_FILLER_MS
        // The dial in progress ({cancelled}). Hang up while it is still
        // connecting sets cancelled; call() checks it after every wait, so no call
        // is placed (or left running) after the rep pressed Hang up.
        let dialing = null;
        // This call's health tracker (call_health.js) and its API ping timer.
        let health = null;
        let pingTimer = null;

        state.enabled = await user.hasGroup("ariane_dialer.group_dialer_user");
        state.demoAllowed = state.enabled && (await user.hasGroup("ariane_dialer.group_dialer_demo"));
        state.isManager = state.enabled && (await user.hasGroup("ariane_dialer.group_dialer_manager"));

        // ------------------------------------------------------- call health
        function startHealth() {
            stopPing();
            health = createHealth(() => Date.now());
            syncHealth();
        }

        function syncHealth() {
            state.health = health ? health.view() : null;
        }

        function healthEvent(name, extra) {
            if (!health) return;
            health.event(name, extra);
            syncHealth();
        }

        function healthHangup(by, event, extra) {
            if (!health) return;
            health.hangup(by);
            if (event) health.event(event, extra);
            syncHealth();
        }

        // "API connection": an Odoo round trip every API_PING_MS while the call
        // rings or is live. Each tick belongs to one tracker: a newer call's
        // tracker never gets an older call's ping.
        function startPing() {
            if (pingTimer || !health) return;
            const mine = health;
            const tick = () => {
                pingTimer = setTimeout(async () => {
                    if (mine !== health) return;
                    const t = Date.now();
                    try {
                        await rpc("/ariane_dialer/ping", {});
                        if (mine === health) mine.apiPing(Date.now() - t);
                    } catch (e) {
                        if (mine === health) mine.apiError(e?.code || e?.status || "failed");
                    }
                    if (mine !== health) return;
                    syncHealth();
                    if (pingTimer) tick();
                }, API_PING_MS);
            };
            tick();
        }

        function stopPing() {
            if (pingTimer) {
                clearTimeout(pingTimer);
                pingTimer = null;
            }
        }

        // The summary goes to the call record once, at the end of the wrap-up.
        function sendHealth(callId) {
            const h = health;
            if (!h || !callId) return;
            rpc("/ariane_dialer/call/health", { call_id: callId, summary: h.summary() }).catch(() => {});
        }

        async function api(path, body) {
            const res = await fetch(state.backend.api_url + path, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Authorization: `Bearer ${state.backend.rep_token}`,
                },
                body: JSON.stringify(body || {}),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
                err.code = data.error;
                err.status = res.status;
                throw err;
            }
            return data;
        }

        async function mintSession() {
            const s = await rpc("/ariane_dialer/session");
            fillers = cleanFillers(s?.fillers);
            if (!s.configured) return null;
            const res = await fetch(s.api_url + "/v1/dialer/session", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ assertion: s.assertion }),
            });
            if (!res.ok) throw new Error(`dialler session refused (${res.status})`);
            const data = await res.json();
            return { api_url: s.api_url, ...data };
        }

        // The phone is set up once, when Odoo opens, so pressing "Call" does not
        // wait for sign-in and the Twilio library. Callers share one attempt.
        let connecting = null;
        function connect() {
            if (device || !state.enabled) return Promise.resolve();
            if (!connecting) connecting = setUpPhone().finally(() => (connecting = null));
            return connecting;
        }

        async function setUpPhone() {
            let session;
            try {
                session = await mintSession();
            } catch (e) {
                state.error = "Can't reach the dialler — preview mode only";
                return;
            }
            if (!session) {
                state.preview = true;
                return;
            }
            await loadJS(SDK_URL);
            // liveCoach: the tenant transcribes and coaches the rep's own calls,
            // so the customer's signed product list must go with the dial.
            state.backend = {
                api_url: session.api_url, rep_token: session.rep_token, liveCoach: Boolean(session.live_coach),
            };
            device = new window.Twilio.Device(session.twilio_token, {
                codecPreferences: ["opus", "pcmu"],
                closeProtection: true,
                tokenRefreshMs: 60000,
            });
            device.on("tokenWillExpire", async () => {
                try {
                    const fresh = await mintSession();
                    state.backend.rep_token = fresh.rep_token;
                    device.updateToken(fresh.twilio_token);
                } catch {
                    state.error = "Dialler sign-in expired — reload the page";
                }
            });
            device.on("error", (e) => {
                state.error = `Phone error: ${e.message || e.code}`;
            });
            device.on("incoming", (call) => onIncoming(call));
            state.preview = false;
            state.error = "";
            // Registering only lets calls come IN; an outbound dial never waits for it.
            device.register().catch((e) => {
                state.error = `Phone error: ${e.message || e.code}`;
            });
        }

        function resetCall() {
            Object.assign(state, {
                mode: "idle", phone: "", partnerId: null, leadId: null, callId: null,
                startedAt: null, muted: false, info: null, query: "", results: [],
                quote: null, cards: [], coachState: "off", blocked: "", ringingIn: false,
                hidden: 0, demoCallSid: null, transcript: [], coach: null, callStatus: "",
                savedToOdoo: null, liveCoach: false, shopLinks: {}, queueKey: null,
                hot: null, filler: "", mentions: [],
            });
            searchSlow = false;
            incomingCall = null;
            activeCall = null;
            dialing = null;
            closeCoach();
            stopPing();
            health = null;
            state.health = null;
        }

        // A dial the rep hung up on while it was connecting. Hang up has already
        // put the panel in wrap-up; this only makes sure the call record follows.
        function abandoned(dial) {
            if (!dial.cancelled) return false;
            healthHangup("cancelled");
            state.hot = null; // no call was placed: no strip
            if (state.callId) rpc("/ariane_dialer/call/update", { call_id: state.callId, state: "wrapup" });
            return true;
        }

        // After a page reload, the rep's own call that was never finished comes
        // back into the wrap-up: a reload must not skip the notes.
        async function resumeOpenCall() {
            if (!state.enabled || state.mode !== "idle") return;
            let open;
            try {
                open = await rpc("/ariane_dialer/call/pending", {});
            } catch {
                return;
            }
            if (!open?.call_id || state.mode !== "idle") return;
            Object.assign(state, {
                open: true, mode: "wrapup", callId: open.call_id, phone: open.phone || "",
                partnerId: open.partner_id || null, leadId: open.lead_id || null,
                blocked: "Finish your last call first: pick an outcome and write a note.",
            });
            if (open.state !== "wrapup") rpc("/ariane_dialer/call/update", { call_id: open.call_id, state: "wrapup" });
            lookup().catch(() => {});
        }

        async function lookup() {
            state.info = await rpc("/ariane_dialer/lookup", {
                phone: state.phone, partner_id: state.partnerId, lead_id: state.leadId,
            });
            if (state.info?.partner && !state.partnerId) state.partnerId = state.info.partner.id;
        }

        function wireCall(call) {
            activeCall = call;
            // The server starts listening before the customer answers, so a live
            // call's feed opens as soon as Twilio names the call.
            let coachOpen = false;
            call.on("ringing", () => {
                if (health) {
                    health.mark("ringing");
                    syncHealth();
                    startPing();
                }
                if (state.liveCoach && call.parameters?.CallSid && !coachOpen) {
                    coachOpen = true;
                    openCoach(call.parameters.CallSid);
                }
            });
            call.on("accept", () => {
                state.mode = "live";
                state.startedAt = Date.now();
                if (health) {
                    health.mark("answered");
                    syncHealth();
                    startPing();
                }
                rpc("/ariane_dialer/call/update", {
                    call_id: state.callId, state: "live",
                    external_call_id: call.parameters?.CallSid,
                });
                if (!coachOpen) {
                    coachOpen = true;
                    openCoach(call.parameters?.CallSid);
                }
            });
            const end = () => {
                stopPing();
                if (state.mode === "wrapup" || state.mode === "idle") return;
                state.mode = "wrapup";
                rpc("/ariane_dialer/call/update", { call_id: state.callId, state: "wrapup" });
                if (!state.liveCoach || !state.startedAt) {
                    // Nobody answered: there is nothing to wait for.
                    closeCoach();
                    state.liveCoach = false;
                    return;
                }
                // A live call's final notes still come, but never wait for them forever:
                // if the audio feed never reached the server, no "ended" ever will.
                const callId = state.callId;
                setTimeout(() => {
                    if (state.callId !== callId || state.callStatus === "ended") return;
                    closeCoach();
                    state.liveCoach = false;
                    state.coachState = "off";
                    state.blocked = "The live notes did not arrive. Write your own notes below.";
                }, FINAL_NOTES_WAIT_MS);
            };
            // Who hung up: the rep's own Hang up has already said "rep"; a
            // disconnect nobody here asked for is the customer's.
            call.on("disconnect", () => {
                healthHangup("customer", "disconnect");
                end();
            });
            call.on("cancel", () => {
                healthHangup("cancelled", "cancel");
                end();
            });
            call.on("reject", () => {
                healthHangup("rep", "reject");
                end();
            });
            call.on("error", (e) => {
                state.error = `Call error: ${e.message || e.code}`;
                healthHangup("error", "error", { code: e?.code, detail: e?.message });
                end();
            });
            call.on("warning", (name) => {
                state.error = `Call quality: ${name}`;
                if (health) {
                    health.warning(name);
                    syncHealth();
                }
            });
            call.on("warning-cleared", (name) => {
                state.error = "";
                if (health) {
                    health.warningCleared(name);
                    syncHealth();
                }
            });
            call.on("sample", (sample) => {
                if (health && activeCall === call) {
                    health.sample(sample);
                    syncHealth();
                }
            });
        }

        async function onIncoming(call) {
            if (state.mode !== "idle") {
                call.reject();
                return;
            }
            state.open = true;
            state.mode = "ringing";
            startHealth();
            healthEvent("incoming");
            state.phone = call.parameters?.From || "";
            await lookup();
            const r = await rpc("/ariane_dialer/call/start", {
                phone: state.phone, partner_id: state.partnerId, direction: "in",
                external_call_id: call.parameters?.CallSid,
            });
            state.callId = r.call_id;
            wireCall(call);
            incomingCall = call;
            state.ringingIn = true;
        }

        // ------------------------------------------------------------- fillers
        // A filler is wanted while a customer question waits on the coach, or a
        // product search is slow. It is picked once per wait; the template shows
        // it only on a live call with the coach listening (fillerVisible).
        function syncFiller() {
            if (!passGuard.pending && !searchSlow) {
                state.filler = "";
            } else if (!state.filler) {
                state.filler = pickFiller(fillers, lastFiller);
                lastFiller = state.filler;
            }
        }

        function clearPendingTimer() {
            if (pendingTimer) {
                clearTimeout(pendingTimer);
                pendingTimer = null;
            }
        }

        function onPending(msg) {
            if (!acceptPending(passGuard, msg, Date.now())) return;
            clearPendingTimer();
            const guard = passGuard;
            pendingTimer = setTimeout(() => {
                pendingTimer = null;
                if (guard === passGuard && expirePending(guard, Date.now())) syncFiller();
            }, Math.max(0, Math.min(FILLER_WAIT_MS, passGuard.pending.until - Date.now())));
            syncFiller();
        }

        function onPassEnd(msg) {
            settlePass(passGuard, msg);
            if (!passGuard.pending) clearPendingTimer();
            syncFiller();
        }

        // ---------------------------------------------------------- live coach
        // The demo call has no browser leg, so its life comes from the server.
        function onCallState(msg) {
            const s = msg.state;
            state.callStatus = s;
            if (["dialling", "initiated", "queued", "ringing"].includes(s)) {
                if (state.mode !== "live") state.mode = "ringing";
            } else if (["live", "in-progress", "answered"].includes(s)) {
                if (state.mode !== "live") {
                    state.mode = "live";
                    state.startedAt = Date.now();
                    rpc("/ariane_dialer/call/update", { call_id: state.callId, state: "live" });
                }
            } else if (s === "failed") {
                state.blocked = msg.detail || "The AI rep could not be started.";
            } else if (s === "ended") {
                state.savedToOdoo = msg.saved_to_odoo ?? null;
                if (msg.status && msg.status !== "completed") state.blocked = `Call ${msg.status}.`;
                if (state.mode !== "wrapup" && state.mode !== "idle") {
                    state.mode = "wrapup";
                    rpc("/ariane_dialer/call/update", { call_id: state.callId, state: "wrapup" });
                }
                closeCoach(); // the final notes arrived before "ended"
            }
        }

        // A name the customer (or the rep) said: Odoo's product search for it, with this
        // customer, exactly as the rep's own search (no new trust path). Once a
        // name per call; a replay after a reconnect asks nothing.
        async function onMention(msg) {
            const term = typeof msg.term === "string" ? msg.term.trim() : "";
            if (term.length < 2 || term.length > MENTION_TERM_LEN) return;
            const key = term.toLowerCase();
            // who said it: "rep" only when the feed says so (an older feed sent customers' only)
            const speaker = msg.speaker === "rep" ? "rep" : "customer";
            const known = state.mentions.find((m) => m.term.toLowerCase() === key);
            if (known) {
                if (speaker === "customer") known.speaker = "customer"; // the customer said it too: their card now
                return;
            }
            const id = ++mentionSeq;
            const entry = {
                id, term, speaker, kind: typeof msg.kind === "string" ? msg.kind : "",
                heard: typeof msg.heard === "string" ? msg.heard : "", status: "loading", products: [], hidden: 0,
            };
            state.mentions = [entry, ...state.mentions].slice(0, MAX_MENTIONS);
            let found = null;
            try {
                found = await rpc("/ariane_dialer/products", {
                    query: term, partner_id: state.partnerId, limit: MENTION_CARDS,
                    kind: entry.kind, // a heard brand finds the brand's products first, not names containing it
                });
            } catch {
                found = null;
            }
            // the call was saved (or the name scrolled off) meanwhile: nothing to show,
            // even when the next call hears the same name (another customer, other prices)
            const shown = state.mentions.find((m) => m.id === id);
            if (!shown) return;
            if (!found || !Array.isArray(found.products)) {
                shown.status = "error";
                return;
            }
            shown.products = found.products.map(mentionCard).filter(Boolean);
            shown.hidden = Number.isInteger(found.hidden) && found.hidden > 0 ? found.hidden : 0;
            shown.status = shown.products.length ? "ok" : "none";
        }

        function openCoach(callSid) {
            if (!state.backend || !callSid) return;
            const url = state.backend.api_url.replace(/^http/, "ws") + `/ws/coach/${encodeURIComponent(callSid)}`;
            closeCoach();
            const gen = coachGen;
            let attempt = 0;
            let refused = false;
            const open = () => {
                retryTimer = null;
                if (gen !== coachGen) return;
                const ws = new WebSocket(url);
                socket = ws;
                ws.onopen = () => {
                    if (gen === coachGen) healthEvent("coach_open", attempt ? { code: `retry ${attempt}` } : undefined);
                    attempt = 0;
                    ws.send(JSON.stringify({ token: state.backend.rep_token, last_seq: lastSeq }));
                };
                ws.onmessage = (ev) => {
                    if (gen !== coachGen) return;
                    let msg;
                    try {
                        msg = JSON.parse(ev.data);
                    } catch {
                        return;
                    }
                    if (msg.seq) lastSeq = Math.max(lastSeq, msg.seq);
                    if (msg.type === "coach_state") {
                        state.coachState = msg.state;
                        if (health) {
                            health.coachState(msg.state);
                            syncHealth();
                        }
                        if (msg.state === "off") refused = true; // not ours, or gone: never retry
                    }
                    if (msg.type === "transcript") {
                        state.transcript.push({ speaker: msg.speaker, text: msg.text, start: msg.start, seq: msg.seq });
                    }
                    if (msg.type === "coach") {
                        if (health) {
                            health.coachAnswer(msg.pass_id);
                            syncHealth();
                        }
                        if (!isStaleCoach(passGuard, msg)) state.coach = msg;
                        onPassEnd(msg);
                    }
                    if (msg.type === "coach_pending") {
                        health?.coachPending(msg.pass_id);
                        onPending(msg);
                    }
                    if (msg.type === "coach_idle") onPassEnd(msg);
                    if (msg.type === "call_state") onCallState(msg);
                    if (msg.type === "mention") onMention(msg);
                    if (msg.type === "card") {
                        const age = Date.now() - Date.parse(msg.card.emitted_at || Date.now());
                        if (age > CARD_MAX_AGE_MS) return; // stale: dropped, never shown late
                        state.cards = [msg.card, ...state.cards.filter((c) => c.kind !== msg.card.kind)].slice(0, 2);
                    }
                };
                ws.onclose = () => {
                    if (gen !== coachGen || refused) return;
                    // A demo or live-transcribed call keeps listening through wrap-up
                    // until "ended" brings the final notes.
                    const waiting = state.demoCallSid || state.liveCoach
                        ? state.callStatus !== "ended" : state.mode === "live";
                    if (!waiting) return;
                    state.coachState = "paused";
                    attempt += 1;
                    healthEvent("coach_reconnect", { code: `attempt ${attempt}` });
                    retryTimer = setTimeout(open, Math.min(8000, 500 * 2 ** attempt));
                };
            };
            lastSeq = 0;
            passGuard = newPassGuard();
            state.coachState = "live";
            healthEvent("coach_connect");
            open();
        }

        function closeCoach() {
            coachGen += 1;
            // a closed feed answers nothing: whatever the rep was waiting on is over
            passGuard.pending = null;
            clearPendingTimer();
            syncFiller();
            if (retryTimer) {
                clearTimeout(retryTimer);
                retryTimer = null;
            }
            if (socket) {
                socket.onclose = null;
                socket.close();
                socket = null;
            }
        }

        // ------------------------------------------------------------- actions
        const dialer = {
            state,
            // Badge pills for a product line, recommendation or strip row: Odoo's own
            // marks only. Pass the component's state so a change re-renders it.
            badges(item, s = state) {
                const where = s.hot?.state || s.info?.partner?.ship_state || s.info?.partner?.state || "";
                return badgesOf(item).map((b) => {
                    let label;
                    if (b.kind === "clearance") label = _t("Clearance");
                    else if (b.kind === "new_seller") label = _t("New seller");
                    else if (b.rank) label = where ? _t("Hot #%s in %s", b.rank, where) : _t("Hot #%s", b.rank);
                    else label = where ? _t("Hot in %s", where) : _t("Hot");
                    return { kind: b.kind, label };
                });
            },
            suggestedNote() {
                const c = state.coach;
                if (!c) return "";
                const parts = [];
                if (c.summary) parts.push(c.summary);
                if (c.notes?.length) parts.push(c.notes.map((n) => `- ${n}`).join("\n"));
                if (c.next_action) parts.push(`Next: ${c.next_action}`);
                return parts.join("\n\n");
            },
            toggle() {
                state.open = !state.open;
                if (state.open) {
                    connect();
                    if (state.mode === "idle") {
                        dialer.loadQueue();
                        resumeOpenCall();
                    }
                }
            },
            // A reload asked for mid-read (say, by a wrap-up) reads again when the
            // first read lands, so the list never shows what was there before the save.
            loadQueue() {
                if (!state.enabled) return Promise.resolve();
                if (queueLoad) {
                    queueStale = true;
                    return queueLoad;
                }
                state.queueLoading = true;
                queueLoad = (async () => {
                    try {
                        do {
                            queueStale = false;
                            try {
                                state.queue = (await rpc("/ariane_dialer/queue", {})).items;
                            } catch {
                                state.queue = []; // a failed read still honours a reload asked for meanwhile
                            }
                        } while (queueStale);
                    } finally {
                        state.queueLoading = false;
                        queueLoad = null;
                    }
                })();
                return queueLoad;
            },
            // The next row to call: not skipped, has a number, not the call on screen.
            nextInQueue() {
                return state.queue.find((i) => i.phone && !state.skipped.includes(i.key) && i.key !== state.queueKey) || null;
            },
            skip(item) {
                state.skipped = [...state.skipped, item.key];
            },
            callFromQueue(item) {
                if (!item?.phone) return;
                // a call is already open: call() says "finish the current call first";
                // the open call keeps its own row
                if (state.mode !== "idle") return dialer.call({ phone: item.phone, demo: false });
                const placed = dialer.call({ phone: item.phone, partnerId: item.partner_id || null, leadId: item.lead_id || null, demo: false });
                // set after call() has claimed the panel (it refuses while another call is open)
                if (state.mode !== "idle") state.queueKey = item.key;
                return placed;
            },
            // demo: the AI rep calls instead of the rep. The panel's dial pad uses its
            // toggle; a phone field's buttons always say which one they mean.
            async call({ phone, partnerId = null, leadId = null, demo = state.demoMode }) {
                if (!state.enabled) return;
                if (demo && !state.demoAllowed) {
                    notification.add("You are not set up for AI rep calls.", { type: "warning" });
                    return;
                }
                if (state.mode !== "idle") {
                    notification.add("Finish the current call first.", { type: "warning" });
                    state.open = true;
                    return;
                }
                state.open = true;
                state.phone = phone;
                state.partnerId = partnerId;
                state.leadId = leadId;
                state.mode = "connecting";
                startHealth();
                const dial = { cancelled: false };
                dialing = dial;
                const ready = connect();
                // The customer card loads while the call is placed. Only a number
                // typed on the dial pad waits for it: the call record needs the customer.
                const looked = lookup().catch(() => {
                    state.error = "Could not load the customer card";
                });
                if (!state.partnerId) await looked;
                await ready;
                const e164 = toE164(phone);
                const started = await rpc("/ariane_dialer/call/start", {
                    phone: e164 || phone, partner_id: state.partnerId, lead_id: leadId,
                    preview: state.preview,
                });
                state.callId = started.call_id;
                if (abandoned(dial)) return;
                if (state.preview) {
                    // No telephony: nothing to measure.
                    health = null;
                    syncHealth();
                }
                if (demo && state.preview) {
                    state.blocked = "Not dialled: the dialler service is not connected.";
                    state.mode = "wrapup";
                    return;
                }
                if (demo) {
                    try {
                        if (!state.partnerId) throw new Error("Open a customer first: the coach needs their prices.");
                        const ctx = await rpc("/ariane_dialer/coach_context", { call_id: state.callId });
                        if (!ctx.configured) throw new Error("The coach is not set up on this database.");
                        state.shopLinks = ctx.links || {};
                        state.hot = hotStrip(ctx.hot, pricesFromContext(ctx.context));
                        if (abandoned(dial)) return;
                        const r = await api("/v1/dialer/demo-call", {
                            e164, partner_id: state.partnerId, odoo_call_id: state.callId, context: ctx.context,
                        });
                        if (abandoned(dial)) {
                            // the AI rep call was placed after Hang up: end it at once
                            api(`/v1/dialer/demo-call/${encodeURIComponent(r.call_sid)}/end`, {}).catch(() => {});
                            return;
                        }
                        state.demoCallSid = r.call_sid;
                        state.mode = "ringing";
                        healthEvent("demo_call", { code: r.call_sid });
                        rpc("/ariane_dialer/call/update", {
                            call_id: state.callId, state: "ringing", external_call_id: r.call_sid,
                        });
                        openCoach(r.call_sid);
                    } catch (e) {
                        healthHangup("error", "error", { code: e.code || e.status, detail: e.message });
                        state.blocked = `Not dialled: ${e.message}`;
                        state.mode = "wrapup";
                        state.hot = null; // no call was placed: no strip
                    }
                    return;
                }
                if (state.preview) {
                    // No telephony yet: the rep dials on their usual phone and logs here.
                    state.mode = "live";
                    state.startedAt = Date.now();
                    return;
                }
                try {
                    // With the customer's signed product list the server may transcribe
                    // and coach this call live (tenant switch, test numbers only).
                    let context;
                    let hot = null;
                    if (state.partnerId && state.backend?.liveCoach) {
                        const ctx = await rpc("/ariane_dialer/coach_context", { call_id: state.callId }).catch(() => null);
                        if (ctx?.configured) {
                            context = ctx.context;
                            state.shopLinks = ctx.links || {};
                            hot = hotStrip(ctx.hot, pricesFromContext(ctx.context));
                        }
                    }
                    if (abandoned(dial)) return;
                    const auth = await api("/v1/dialer/authorize", {
                        e164, partner_id: state.partnerId, odoo_call_id: state.callId, context,
                    });
                    if (abandoned(dial)) return;
                    if (health) {
                        health.mark("authorized");
                        syncHealth();
                    }
                    state.liveCoach = Boolean(auth.live);
                    // the strip goes with the live coach: a call the service will not coach shows none
                    state.hot = state.liveCoach ? hot : null;
                    const call = await device.connect({
                        params: { To: e164, authorization_id: auth.authorization_id },
                    });
                    if (abandoned(dial)) {
                        // Placed after Hang up: cut it before it rings.
                        call.disconnect();
                        return;
                    }
                    if (health) {
                        health.mark("connected");
                        syncHealth();
                    }
                    state.mode = "ringing";
                    wireCall(call);
                } catch (e) {
                    if (abandoned(dial)) return;
                    healthHangup("error", "error", { code: e.code || e.status, detail: e.message });
                    state.blocked = e.status === 403 || e.status === 409
                        ? `Not dialled: ${e.message}` : `Could not dial: ${e.message}`;
                    state.mode = "wrapup";
                    state.hot = null; // no call was placed: no strip
                }
            },
            answer() {
                incomingCall?.accept();
                incomingCall = null;
                state.ringingIn = false;
            },
            async hangup() {
                if (state.demoCallSid) {
                    // Ask Twilio to end it; the screen moves on only when "ended" arrives.
                    try {
                        await api(`/v1/dialer/demo-call/${encodeURIComponent(state.demoCallSid)}/end`, {});
                    } catch (e) {
                        state.error = `Could not hang up: ${e.message}`;
                    }
                    return;
                }
                if (activeCall) {
                    healthHangup("rep", "hangup");
                    activeCall.disconnect();
                    return;
                }
                if (state.mode === "idle") return;
                healthHangup("cancelled", "hangup");
                // No call object yet: the dial is still connecting. Stop it; call()
                // sees the flag after its next wait and places nothing (or cuts what
                // it just placed). A server-side end of the provider call is not done
                // here: that needs the dialler service.
                if (dialing) dialing.cancelled = true;
                state.mode = "wrapup";
                if (state.callId) rpc("/ariane_dialer/call/update", { call_id: state.callId, state: "wrapup" });
            },
            toggleMute() {
                if (!activeCall) return;
                state.muted = !state.muted;
                activeCall.mute(state.muted);
            },
            digit(d) {
                activeCall?.sendDigits(d);
            },
            async search(query) {
                state.query = query;
                const mine = ++searchSeq;
                searchSlow = false; // a newer search replaces the older one's wait
                syncFiller();
                if ((query || "").trim().length < 2) {
                    state.results = [];
                    state.hidden = 0;
                    state.searching = false;
                    return;
                }
                state.searching = true;
                // A slow search gets a filler line, so the rep is not left silent.
                const slow = setTimeout(() => {
                    if (mine !== searchSeq) return;
                    searchSlow = true;
                    syncFiller();
                }, SEARCH_FILLER_MS);
                try {
                    const results = await rpc("/ariane_dialer/products", { query, partner_id: state.partnerId });
                    if (mine === searchSeq) {
                        state.results = results.products;
                        state.hidden = results.hidden;
                    }
                } finally {
                    clearTimeout(slow);
                    if (mine === searchSeq) {
                        state.searching = false;
                        searchSlow = false;
                        syncFiller();
                    }
                }
            },
            async openProduct(productId) {
                const card = await rpc("/ariane_dialer/product", { product_id: productId, partner_id: state.partnerId });
                state.results = [card, ...state.results.filter((r) => r.id !== productId)];
            },
            async addToQuote(productId, qty, addKey) {
                // addKey is made once per click, so a retried request adds one line, not two.
                const r = await rpc("/ariane_dialer/quote/add", {
                    partner_id: state.partnerId, product_id: productId, qty,
                    order_id: state.quote?.order_id, call_id: state.callId, add_key: addKey,
                });
                state.quote = r;
                notification.add(`Added to ${r.order_name} — Odoo price ${r.line_price.toFixed(2)} ${r.currency}`, {
                    type: "success",
                });
            },
            async loadOutcomes() {
                if (!state.outcomes.length) state.outcomes = await rpc("/ariane_dialer/outcomes", {});
                return state.outcomes;
            },
            async wrapup({ outcome, note, followUpAt, closureReason }) {
                const callId = state.callId;
                const r = await rpc("/ariane_dialer/call/wrapup", {
                    call_id: callId, outcome, note,
                    follow_up_at: followUpAt || null, closure_reason: closureReason || "",
                    // The request the rep was told this call completes (0 = none).
                    expected_request: state.info?.open_request?.id || 0,
                });
                // The call's health summary goes once the wrap-up is saved: its
                // timeline then holds the whole call, the save included.
                healthEvent("wrapup_saved");
                sendHealth(callId);
                if (r.do_not_call === "needs_manager") {
                    notification.add("Logged as Do not call. A manager must block the number.", { type: "warning" });
                }
                // A row just called is not offered again on this page, even where
                // the tenant leaves its activity open (the generic module does).
                if (state.queueKey) state.skipped = [...state.skipped, state.queueKey];
                resetCall();
                // The saved call's row leaves the list (done) or moves (follow-up).
                await dialer.loadQueue();
            },
            setPartner(partnerId) {
                state.partnerId = partnerId;
                lookup();
            },
            fromRecord(record, fieldName, { demo = false } = {}) {
                const phone = record.data[fieldName];
                const model = record.resModel;
                let partnerId = many2oneId(record.data.partner_id);
                let leadId = null;
                if (model === "res.partner") partnerId = record.resId;
                if (model === "crm.lead") leadId = record.resId;
                return dialer.call({ phone, partnerId, leadId, demo });
            },
            // The call on screen belongs to this contact (or one of its people).
            isLiveFor(partnerIds) {
                return state.mode !== "idle" && Boolean(state.partnerId) && partnerIds.includes(state.partnerId);
            },
        };
        connect(); // ready before the first click; a failure leaves preview mode on
        resumeOpenCall();
        return dialer;
    },
};

registry.category("services").add("ariane_dialer", dialerService);
