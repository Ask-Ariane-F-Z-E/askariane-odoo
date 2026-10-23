// Loads the REAL static/src/dialer_service.js in plain Node, with small stubs for
// what it imports from Odoo (@odoo/owl, @web/...) and for the browser (fetch,
// WebSocket, Twilio, timers). The service's own code runs unchanged: only its
// import paths are rewritten, and one line is put in front that hands it the
// fake clock, network and phone. Not a test file itself (no ".test." in the
// name); dialer_service.test.mjs uses it.
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SRC = fileURLToPath(new URL("../src/", import.meta.url));
export const T0 = Date.parse("2026-10-21T12:00:00Z");

// The service's imports, each mapped to a stub. A missing one means the service
// changed its imports: the harness says so instead of loading something else.
const IMPORTS = {
    "@odoo/owl": "owl.mjs",
    "@web/core/registry": "registry.mjs",
    "@web/core/network/rpc": "rpc.mjs",
    "@web/core/assets": "assets.mjs",
    "@web/core/user": "user.mjs",
    "@web/core/l10n/translation": "translation.mjs",
    "./coach_view": "coach_view.mjs",
    "./call_health": "call_health.mjs",
};

const flushOnce = () => new Promise((resolve) => setImmediate(resolve));
export async function flush(rounds = 20) {
    for (let i = 0; i < rounds; i++) await flushOnce();
}

// A clock the test moves by hand: setTimeout / clearTimeout / Date.now.
class Clock {
    constructor() {
        this.now = T0;
        this.timers = new Map();
        this.seq = 0;
        this.setTimeout = (fn, ms = 0) => {
            const id = ++this.seq;
            this.timers.set(id, { at: this.now + Math.max(0, Number(ms) || 0), fn });
            return id;
        };
        this.clearTimeout = (id) => {
            this.timers.delete(id);
        };
        this.Date = { now: () => this.now, parse: (s) => Date.parse(s) };
    }

    // Moves the clock on by ms, firing every timer that falls due, in order.
    async advance(ms) {
        const end = this.now + ms;
        for (;;) {
            let next = null;
            for (const [id, t] of this.timers) {
                if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
            }
            if (!next) break;
            this.timers.delete(next[0]);
            this.now = next[1].at;
            next[1].fn();
            await flush();
        }
        this.now = end;
        await flush();
    }
}

export function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function b64url(obj) {
    return Buffer.from(JSON.stringify(obj)).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export const HOT_BLOCK = {
    status: "ok", state: "TX", days: 30,
    items: [{ id: 11, name: "Hot Pipe", rank: 1, badges: ["clearance"] }],
};
export const SIGNED_CONTEXT = `h.${b64url({ products: [{ id: 11, price: 9.5, currency: "USD" }] })}.s`;

let instances = 0;

// Starts the service as Odoo would, and waits for its phone set-up to finish.
// opts: fillers (session()'s lines), liveCoach (the tenant switch), rpc / api
// (route -> handler overrides; a handler may throw or return a promise).
export async function startService(opts = {}) {
    const id = `h${++instances}`;
    const clock = new Clock();
    const sockets = [];
    const rpcCalls = [];
    const apiCalls = [];
    const phoneCalls = []; // calls placed through the fake Twilio device
    const rpcRoutes = {
        "/ariane_dialer/session": () => ({
            configured: true, api_url: "https://dialer.test", assertion: "a",
            fillers: opts.fillers ?? ["Hold on one sec", "One moment please"],
        }),
        "/ariane_dialer/lookup": () => ({ partner: { id: 5, state: "TX" } }),
        "/ariane_dialer/call/start": () => ({ call_id: 77 }),
        "/ariane_dialer/call/update": () => ({}),
        "/ariane_dialer/coach_context": () => ({ configured: true, context: SIGNED_CONTEXT, links: {}, hot: HOT_BLOCK }),
        "/ariane_dialer/products": () => ({ products: [], hidden: 0 }),
        // the rep's own unfinished call, read once the panel starts (none by default)
        "/ariane_dialer/call/pending": () => null,
        "/ariane_dialer/ping": () => ({ ok: true }),
        "/ariane_dialer/call/health": () => ({ ok: true, code: "saved" }),
        "/ariane_dialer/call/wrapup": () => ({ ok: true }),
        "/ariane_dialer/queue": () => ({ items: [] }),
        ...(opts.rpc || {}),
    };
    const apiRoutes = {
        "/v1/dialer/session": () => ({ rep_token: "rep", twilio_token: "tw", live_coach: Boolean(opts.liveCoach) }),
        "/v1/dialer/demo-call": () => ({ call_sid: "CA-demo" }),
        "/v1/dialer/authorize": () => ({ authorization_id: "auth", live: Boolean(opts.liveCoach) }),
        ...(opts.api || {}),
    };

    class FakeWebSocket {
        constructor(url) {
            this.url = url;
            this.sent = [];
            this.closed = false;
            sockets.push(this);
        }

        send(data) {
            this.sent.push(data);
        }

        close() {
            this.closed = true;
        }

        // the server sends one message
        emit(msg) {
            this.onmessage?.({ data: JSON.stringify(msg) });
        }
    }

    class FakeCall {
        constructor(params) {
            this.params = params;
            this.parameters = { CallSid: "CA-live" };
            this.handlers = {};
        }

        on(name, fn) {
            this.handlers[name] = fn;
        }

        fire(name, ...args) {
            this.handlers[name]?.(...args);
        }

        // Twilio Voice SDK: one "sample" a second while the call has media.
        sample(s) {
            this.fire("sample", {
                rtt: 80, jitter: 5, packetsLostFraction: 0, mos: 4.4, audioInputLevel: 1200, audioOutputLevel: 900, ...s,
            });
        }

        disconnect() {
            this.disconnected = true;
            // the SDK fires "disconnect" for a local hang-up too
            if (opts.disconnectFires) this.fire("disconnect", this);
        }

        mute() {}
    }

    class FakeDevice {
        constructor() {
            this.handlers = {};
        }

        on(name, fn) {
            this.handlers[name] = fn;
        }

        register() {
            return Promise.resolve();
        }

        async connect({ params }) {
            if (opts.connect) return opts.connect(params);
            const call = new FakeCall(params);
            phoneCalls.push(call);
            return call;
        }
    }

    const fetch = async (url, init) => {
        const path = String(url).replace("https://dialer.test", "");
        const body = init?.body ? JSON.parse(init.body) : {};
        apiCalls.push({ path, body });
        const route = apiRoutes[path];
        try {
            if (!route) throw Object.assign(new Error(`no route ${path}`), { status: 404 });
            const data = await route(body);
            return { ok: true, status: 200, json: async () => data };
        } catch (e) {
            const status = e.status || 500;
            return { ok: false, status, json: async () => ({ detail: e.message }) };
        }
    };

    globalThis.__dialerHarness = globalThis.__dialerHarness || {};
    globalThis.__dialerHarness[id] = {
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, Date: clock.Date,
        fetch, WebSocket: FakeWebSocket, window: { Twilio: { Device: FakeDevice } },
        rpc: async (route, params) => {
            rpcCalls.push({ route, params });
            const handler = rpcRoutes[route];
            if (!handler) throw new Error(`no rpc route ${route}`);
            return handler(params);
        },
    };

    const dir = mkdtempSync(join(tmpdir(), "dialer-service-"));
    const h = `globalThis.__dialerHarness[${JSON.stringify(id)}]`;
    writeFileSync(join(dir, "owl.mjs"), "export const reactive = (o) => o;\n");
    writeFileSync(join(dir, "registry.mjs"), "export const registry = { category: () => ({ add() {} }) };\n");
    writeFileSync(join(dir, "rpc.mjs"), `export const rpc = (...a) => ${h}.rpc(...a);\n`);
    writeFileSync(join(dir, "assets.mjs"), "export const loadJS = async () => {};\n");
    // opts.groups: the groups the user has (default: all)
    writeFileSync(join(dir, "user.mjs"), opts.groups
        ? `export const user = { hasGroup: async (g) => ${JSON.stringify(opts.groups)}.includes(g) };\n`
        : "export const user = { hasGroup: async () => true };\n");
    writeFileSync(join(dir, "translation.mjs"),
        "export const _t = (s, ...a) => { let i = 0; return s.replace(/%s/g, () => String(a[i++])); };\n");
    copyFileSync(join(SRC, "coach_view.js"), join(dir, "coach_view.mjs"));
    copyFileSync(join(SRC, "call_health.js"), join(dir, "call_health.mjs"));
    let code = readFileSync(join(SRC, "dialer_service.js"), "utf8");
    for (const [from, to] of Object.entries(IMPORTS)) {
        const needle = `from "${from}";`;
        if (!code.includes(needle)) throw new Error(`dialer_service.js no longer imports ${from}: update the harness`);
        code = code.replace(needle, `from "./${to}";`);
    }
    code = `const { setTimeout, clearTimeout, Date, fetch, WebSocket, window } = ${h};\n${code}`;
    writeFileSync(join(dir, "dialer_service.mjs"), code);
    const mod = await import(pathToFileURL(join(dir, "dialer_service.mjs")).href);
    rmSync(dir, { recursive: true, force: true });

    const dialer = await mod.dialerService.start({}, { notification: { add() {} } });
    await flush();
    if (dialer.state.preview || !dialer.state.backend) throw new Error("harness: the phone did not come up");
    return {
        dialer, state: dialer.state, clock, sockets, rpcCalls, apiCalls, phoneCalls, rpcRoutes, apiRoutes,
        socket: () => sockets[sockets.length - 1],
    };
}

// Loads the REAL static/src/panel.js with stub Odoo imports, so a test can read
// its plain getters (canSave) on an object made from its prototype. Local
// imports ("./x") are copied next to it as they are.
const PANEL_STUBS = {
    "@odoo/owl": "export class Component {}\nexport const onWillStart = () => {};\n"
        + "export const onWillUnmount = () => {};\nexport const useState = (o) => o;\n",
    "@web/core/registry": "export const registry = { category: () => ({ add() {} }) };\n",
    "@web/core/utils/hooks": "export const useService = () => ({});\n",
    "@web/core/utils/timing": "export const useDebounced = (f) => f;\n",
};

export async function loadPanel() {
    const dir = mkdtempSync(join(tmpdir(), "dialer-panel-"));
    let code = readFileSync(join(SRC, "panel.js"), "utf8");
    code = code.replace(/from "([^"]+)";/g, (all, from) => {
        if (from.startsWith("./")) {
            const name = from.slice(2);
            copyFileSync(join(SRC, `${name}.js`), join(dir, `${name}.mjs`));
            return `from "./${name}.mjs";`;
        }
        if (!(from in PANEL_STUBS)) throw new Error(`panel.js imports ${from}: add a stub to the harness`);
        const file = from.replace(/[^\w]/g, "_") + ".mjs";
        writeFileSync(join(dir, file), PANEL_STUBS[from]);
        return `from "./${file}";`;
    });
    writeFileSync(join(dir, "panel.mjs"), code);
    const mod = await import(pathToFileURL(join(dir, "panel.mjs")).href);
    rmSync(dir, { recursive: true, force: true });
    return mod.DialerPanel;
}
