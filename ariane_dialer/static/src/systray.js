/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class DialerSystray extends Component {
    static template = "ariane_dialer.Systray";
    static props = {};
    setup() {
        this.dialer = useService("ariane_dialer");
        this.state = useState(this.dialer.state);
    }
}

registry.category("systray").add("ariane_dialer.systray", { Component: DialerSystray }, { sequence: 90 });
