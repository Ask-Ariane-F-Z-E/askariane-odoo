/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PhoneField } from "@web/views/fields/phone/phone_field";

patch(PhoneField.prototype, {
    setup() {
        super.setup(...arguments);
        this.arianeDialer = useService("ariane_dialer");
        this.arianeDialerState = useState(this.arianeDialer.state);
    },
    get arianeDialerEnabled() {
        return this.arianeDialerState.enabled && this.props.record.data[this.props.name];
    },
    onArianeDial(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        this.arianeDialer.fromRecord(this.props.record, this.props.name, { demo: false });
    },
    onArianeDemoDial(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        this.arianeDialer.fromRecord(this.props.record, this.props.name, { demo: true });
    },
});
