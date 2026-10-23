/** @odoo-module **/
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { fillerVisible } from "./coach_view";
import { noteOk } from "./wrapup_rules";

const HEALTH_HIDDEN_KEY = "ariane_dialer.health_hidden";

// A rep's "hide Call health" choice, kept in this browser only.
function readHealthHidden() {
    try {
        return globalThis.localStorage?.getItem(HEALTH_HIDDEN_KEY) === "1";
    } catch {
        return false;
    }
}

function writeHealthHidden(hidden) {
    try {
        globalThis.localStorage?.setItem(HEALTH_HIDDEN_KEY, hidden ? "1" : "0");
    } catch {
        // private window or blocked storage: the choice lasts for this page only
    }
}

const HANGUP_TEXT = {
    rep: "You hung up", customer: "The customer hung up", cancelled: "Cancelled before it connected",
    error: "Ended by an error",
};

export class DialerPanel extends Component {
    static template = "ariane_dialer.Panel";
    static props = {};

    setup() {
        this.dialer = useService("ariane_dialer");
        this.action = useService("action");
        this.state = useState(this.dialer.state);
        this.ui = useState({ dial: "", keypad: false, qty: {}, outcome: "", note: "", followUp: "", closureReason: "", now: Date.now(), saving: false, adding: false,
            healthOpen: false, healthHidden: readHealthHidden() });
        this.search = useDebounced((q) => this.dialer.search(q), 250);
        const tick = setInterval(() => (this.ui.now = Date.now()), 1000);
        onWillUnmount(() => clearInterval(tick));
        // Outcomes are the tenant's own (e.g. activity outcomes from a tenant module), so they come from the server.
        onWillStart(async () => {
            if (this.state.enabled) await this.dialer.loadOutcomes().catch(() => {});
            if (this.state.enabled && this.state.mode === "idle") this.dialer.loadQueue();
        });
    }

    get outcome() {
        return this.state.outcomes.find((o) => o.code === this.ui.outcome) || null;
    }

    // The owner's rule: a call is not finished until the rep wrote what happened.
    get noteOk() {
        return noteOk(this.ui.note);
    }

    get canSave() {
        const o = this.outcome;
        if (!o || this.ui.saving || !this.state.callId) return false;
        if (!this.noteOk) return false;
        if (o.needs_followup && !this.ui.followUp) return false;
        if (o.needs_closure_reason && !this.ui.closureReason.trim()) return false;
        return true;
    }

    // ------------------------------------------------------------ call health
    // Shown during the call and through wrap-up. A rep may hide it; a manager
    // always has it (the hide button is not offered to them).
    get healthShown() {
        return Boolean(this.state.health) && this.state.mode !== "idle"
            && (this.state.isManager || !this.ui.healthHidden);
    }

    get healthHideable() {
        return !this.state.isManager;
    }

    setHealthHidden(hidden) {
        this.ui.healthHidden = Boolean(hidden) && this.healthHideable;
        writeHealthHidden(this.ui.healthHidden);
    }

    ms(v) {
        return v === null || v === undefined ? "–" : `${Math.round(v)} ms`;
    }

    pct(v) {
        return v === null || v === undefined ? "–" : `${Number(v).toFixed(1)} %`;
    }

    hangupText(by) {
        return HANGUP_TEXT[by] || "";
    }

    eventAt(e) {
        return `${(e.t / 1000).toFixed(1)}s`;
    }

    // ------------------------------------------------------------ call list
    get queueLeft() {
        return this.state.queue.filter((i) => !this.state.skipped.includes(i.key));
    }

    get next() {
        return this.dialer.nextInQueue();
    }

    dueText(item) {
        // A date (generic) or a UTC datetime (a tenant's due time), shown in the rep's time.
        const d = new Date(item.due.length > 10 ? item.due.replace(" ", "T") + "Z" : item.due + "T00:00:00");
        if (isNaN(d)) return item.due;
        const sameDay = d.toDateString() === new Date().toDateString();
        if (item.due.length <= 10) return sameDay ? "today" : d.toLocaleDateString([], { day: "numeric", month: "short" });
        return sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
            : d.toLocaleDateString([], { day: "numeric", month: "short" });
    }

    callNext() {
        const item = this.next;
        if (item) this.dialer.callFromQueue(item);
    }

    // Dials the customer the button named, and only if the refreshed list still
    // has them: a list that changed during the save must not dial someone else.
    async saveAndNext() {
        const item = this.next;
        await this.save();
        if (!item || this.state.mode !== "idle" || this.state.skipped.includes(item.key)) return;
        const fresh = this.state.queue.find((i) => i.key === item.key && i.phone);
        if (fresh) this.dialer.callFromQueue(fresh);
    }

    get elapsed() {
        if (!this.state.startedAt) return "";
        const s = Math.max(0, Math.floor((this.ui.now - this.state.startedAt) / 1000));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }

    get initials() {
        const name = this.state.info?.partner?.name || "";
        return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
    }

    // ------------------------------------------------------------ coach card
    // Read through this component's state, so a change re-renders the panel.
    get fillerShown() {
        return fillerVisible(this.state);
    }

    badges(item) {
        return this.dialer.badges(item, this.state);
    }

    hasClearance(item) {
        return this.badges(item).some((b) => b.kind === "clearance");
    }

    money(v, cur) {
        if (v === null || v === undefined) return "";
        return `${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${cur || ""}`.trim();
    }

    // Quantity starts at the product's minimum order; the server refuses less.
    qty(p) {
        return this.ui.qty[p.id] || p.moq || 1;
    }

    onQty(p, ev) {
        this.ui.qty[p.id] = Math.max(p.moq || 1, Number(ev.target.value) || 1);
        ev.target.value = this.ui.qty[p.id];
    }

    quotable(p) {
        return ["allowed", "review", "unknown"].includes(p.compliance?.status) && p.price !== null;
    }

    onSearch(ev) {
        this.search(ev.target.value);
    }

    dial() {
        if (this.ui.dial.trim()) this.dialer.call({ phone: this.ui.dial.trim() });
    }

    key(d) {
        this.dialer.digit(d);
    }

    async add(p) {
        // One add at a time. A failed add keeps its key, so clicking again after
        // a lost answer replays the first result instead of adding a second line.
        if (this.ui.adding) return;
        this.pendingKeys ||= {};
        const key = (this.pendingKeys[p.id] ||=
            crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
        this.ui.adding = true;
        try {
            await this.dialer.addToQuote(p.id, this.qty(p), key);
            delete this.pendingKeys[p.id];
        } finally {
            this.ui.adding = false;
        }
    }

    async addRec(r) {
        // A coach pick goes through the same Odoo pricing and rules as a search result.
        if (this.ui.adding) return;
        this.pendingKeys ||= {};
        const key = (this.pendingKeys[r.product_id] ||=
            crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
        this.ui.adding = true;
        try {
            // No quantity: Odoo starts the line at the product's minimum order.
            await this.dialer.addToQuote(r.product_id, null, key);
            delete this.pendingKeys[r.product_id];
        } finally {
            this.ui.adding = false;
        }
    }

    useAiNotes() {
        this.ui.note = this.dialer.suggestedNote();
    }

    speaker(line) {
        // On a demo call our side is the AI rep and the person who answered is the customer.
        return line.speaker === "rep" ? (this.state.demoCallSid ? "AI rep" : "You") : "Customer";
    }

    openQuote() {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "sale.order", res_id: this.state.quote.order_id, views: [[false, "form"]] });
    }

    openPartner() {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "res.partner", res_id: this.state.info.partner.id, views: [[false, "form"]] });
    }

    async save() {
        if (!this.canSave) return;
        this.ui.saving = true;
        try {
            // datetime-local is the rep's local time; the server stores UTC.
            const closes = this.outcome.closes;
            const followUpAt = this.ui.followUp && !closes ? new Date(this.ui.followUp).toISOString() : null;
            await this.dialer.wrapup({
                outcome: this.ui.outcome, note: this.ui.note, followUpAt,
                closureReason: closes ? this.ui.closureReason : "",
            });
            Object.assign(this.ui, { outcome: "", note: "", followUp: "", closureReason: "", dial: "" });
        } finally {
            this.ui.saving = false;
        }
    }
}

registry.category("main_components").add("ariane_dialer.Panel", { Component: DialerPanel });
