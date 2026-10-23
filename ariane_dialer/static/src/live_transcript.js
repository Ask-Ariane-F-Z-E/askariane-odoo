/** @odoo-module **/
// The live call on a contact's form: what is being said, line by line, and
// what the coach suggests. It shows only while the dialler's current call is
// with this contact (or one of its people). When the server reports the
// transcript saved, the form reloads so the saved call appears in the list.
import { Component, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

let reloadedFor = null; // shared by every copy of the widget on the page

export class LiveTranscript extends Component {
    static template = "ariane_dialer.LiveTranscript";
    static props = { ...standardWidgetProps };

    setup() {
        this.dialer = useService("ariane_dialer");
        this.state = useState(this.dialer.state);
        // Reload once per saved call, not on every visit to the contact.
        useEffect(
            (saved, mine, callId) => {
                if (saved && mine && reloadedFor !== callId) {
                    reloadedFor = callId;
                    this.reload();
                }
            },
            () => [this.state.savedToOdoo, this.isMine, this.state.callId]
        );
    }

    get partnerIds() {
        const record = this.props.record;
        const children = record.data.child_ids?.records?.map((r) => r.resId) || [];
        return record.resId ? [record.resId, ...children] : [];
    }

    get isMine() {
        return this.dialer.isLiveFor(this.partnerIds);
    }

    async reload() {
        const record = this.props.record;
        if (await record.isDirty()) return; // never throw away what the rep is typing
        await record.load();
    }
}

registry.category("view_widgets").add("ariane_live_transcript", { component: LiveTranscript });
