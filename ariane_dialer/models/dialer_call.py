import json

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import SQL

from .call_health import HANGUP_BY

# Call rows change only through this module's own code: the panel routes and
# the signed ingest. The ACL lets a rep act on their own calls, so a direct RPC
# write would otherwise reach the transcript, the provider id and the stored
# wrap-up. The marker is a Python object: a context sent over RPC is JSON and
# can never hold it.
_TRUSTED = object()


def trusted(records):
    """``records`` in a context that may create and write dialler calls."""
    return records.with_context(ariane_dialer_trusted=_TRUSTED)

DISPOSITIONS = [
    ('interested', 'Interested'),
    ('quote_sent', 'Quote sent'),
    ('callback', 'Callback'),
    ('no_answer', 'No answer'),
    ('voicemail', 'Voicemail'),
    ('wrong_number', 'Wrong number'),
    ('do_not_call', 'Do not call'),
    ('not_qualified', 'Not qualified'),
    ('other', 'Other'),
]

TRANSCRIPT_STATES = [
    ('none', 'None'),
    ('ready', 'Ready'),
    ('withheld', 'Withheld'),
    ('purged', 'Purged'),
]


class ArianeDialerCall(models.Model):
    _name = 'ariane.dialer.call'
    _description = 'Dialler call'
    _order = 'started_at desc, id desc'
    _rec_name = 'phone'

    user_id = fields.Many2one('res.users', 'Rep', required=True, default=lambda s: s.env.user, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company, index=True)
    partner_id = fields.Many2one('res.partner', 'Customer', index=True)
    commercial_partner_id = fields.Many2one('res.partner', 'Customer Company', related='partner_id.commercial_partner_id',
                                            store=True, index=True)
    lead_id = fields.Many2one('crm.lead', 'Lead', index=True)
    phone = fields.Char(required=True)
    direction = fields.Selection([('out', 'Outbound'), ('in', 'Inbound')], default='out', required=True)
    state = fields.Selection([
        ('preview', 'Preview (no telephony)'),
        ('dialing', 'Dialling'),
        ('live', 'On call'),
        ('wrapup', 'Wrap-up'),
        ('done', 'Done'),
    ], default='dialing', required=True)
    started_at = fields.Datetime(default=fields.Datetime.now, required=True)
    ended_at = fields.Datetime()
    duration = fields.Integer('Duration (s)', compute='_compute_duration', store=True)
    disposition = fields.Selection(DISPOSITIONS, help='Cross-tenant bucket of the outcome.')
    outcome_code = fields.Char('Outcome code', help="The tenant's own outcome, e.g. an activity outcome from a tenant module.")
    outcome_label = fields.Char('Outcome')
    note = fields.Text()
    follow_up_at = fields.Datetime('Follow-up (UTC)')
    closure_reason = fields.Char()
    # Set only by a signed push from the dialler service, never by the browser.
    external_call_id = fields.Char('Provider call id', index=True, copy=False, readonly=True)
    sale_order_id = fields.Many2one('sale.order', 'Quotation')
    # Idempotency: the first result of a wrap-up / an add-to-quote is replayed on retry.
    wrapup_result = fields.Text(copy=False, readonly=True)
    quote_add_key = fields.Char(copy=False, readonly=True)
    # {add_key: result} for the last QUOTE_KEYS adds, so any retried add is replayed.
    quote_add_result = fields.Text(copy=False, readonly=True)
    # The tracked request the panel showed at lookup (0 = none shown). The tenant's
    # log refuses to complete a different one.
    expected_request = fields.Integer(copy=False, readonly=True)

    # Transcript (pushed by the dialler service after the call; see the ingest route)
    transcript_state = fields.Selection(TRANSCRIPT_STATES, default='none', required=True, readonly=True)
    transcript_revision = fields.Integer(default=0, readonly=True, copy=False)
    transcript = fields.Text()
    summary = fields.Text()
    next_action = fields.Text()
    language = fields.Char()
    transcript_source = fields.Char()
    recording_ref = fields.Char('Recording id', help='Provider id of the recording. Never a URL.')
    segment_ids = fields.One2many('ariane.dialer.call.segment', 'call_id', 'Transcript lines')
    # The short summary note the ingest posted on the contact: posted once per
    # call, removed again when the transcript is withheld or purged.
    summary_message_id = fields.Many2one('mail.message', 'Summary note', readonly=True, copy=False,
                                         ondelete='set null')
    # Retention (see dialer_retention.py): when the daily job removed the
    # transcript text and lines. A later transcript push is refused after it.
    transcript_purged_at = fields.Datetime('Transcript removed on', readonly=True, copy=False)

    # Call health (see call_health.py): a compact summary the rep's panel sends
    # once, at the end of the call. Numbers and a capped JSON timeline only.
    health_saved_at = fields.Datetime('Health saved', readonly=True, copy=False)
    health_samples = fields.Integer('Quality samples', readonly=True, copy=False)
    health_rtt_min = fields.Float('Edge round trip min (ms)', readonly=True, copy=False)
    health_rtt_avg = fields.Float('Edge round trip avg (ms)', readonly=True, copy=False)
    health_rtt_p95 = fields.Float('Edge round trip p95 (ms)', readonly=True, copy=False)
    health_jitter_min = fields.Float('Jitter min (ms)', readonly=True, copy=False)
    health_jitter_avg = fields.Float('Jitter avg (ms)', readonly=True, copy=False)
    health_jitter_p95 = fields.Float('Jitter p95 (ms)', readonly=True, copy=False)
    health_loss_min = fields.Float('Packet loss min (%)', readonly=True, copy=False)
    health_loss_avg = fields.Float('Packet loss avg (%)', readonly=True, copy=False)
    health_loss_p95 = fields.Float('Packet loss p95 (%)', readonly=True, copy=False)
    health_mos_min = fields.Float('MOS min', readonly=True, copy=False)
    health_mos_avg = fields.Float('MOS avg', readonly=True, copy=False)
    health_mos_p95 = fields.Float('MOS p95', readonly=True, copy=False)
    # Connect timings in ms; 0 = that step was not reached.
    health_authorize_ms = fields.Integer('Click to authorised (ms)', readonly=True, copy=False)
    health_connect_ms = fields.Integer('Authorised to connected (ms)', readonly=True, copy=False)
    health_ringing_ms = fields.Integer('Connected to ringing (ms)', readonly=True, copy=False)
    health_answer_ms = fields.Integer('Ringing to answered (ms)', readonly=True, copy=False)
    health_hangup_by = fields.Selection(HANGUP_BY, 'Hung up by', readonly=True, copy=False)
    health_warning_count = fields.Integer('Quality warnings', readonly=True, copy=False)
    health_api_avg_ms = fields.Integer('API connection avg (ms)', readonly=True, copy=False)
    health_api_p95_ms = fields.Integer('API connection p95 (ms)', readonly=True, copy=False)
    health_api_errors = fields.Integer('API connection failures', readonly=True, copy=False)
    health_coach_avg_ms = fields.Integer('Coach answer avg (ms)', readonly=True, copy=False)
    health_coach_p95_ms = fields.Integer('Coach answer p95 (ms)', readonly=True, copy=False)
    health_timeline = fields.Text('Event timeline (JSON)', readonly=True, copy=False)
    health_events_dropped = fields.Integer('Timeline events dropped', readonly=True, copy=False)
    # Retention: when the daily job removed the timeline (the numbers stay).
    health_timeline_purged_at = fields.Datetime('Timeline removed on', readonly=True, copy=False)

    _sql_constraints = [
        ('external_call_id_uniq', 'unique(company_id, external_call_id)',
         'A provider call id is used once per company.'),
    ]

    def _check_trusted(self):
        if not self.env.su and self.env.context.get('ariane_dialer_trusted') is not _TRUSTED:
            raise AccessError('Dialler calls are changed through the dialler only.')

    @api.model_create_multi
    def create(self, vals_list):
        self._check_trusted()
        return super().create(vals_list)

    def write(self, vals):
        self._check_trusted()
        return super().write(vals)

    @api.depends('started_at', 'ended_at')
    def _compute_duration(self):
        for call in self:
            if call.started_at and call.ended_at:
                call.duration = max(0, int((call.ended_at - call.started_at).total_seconds()))
            else:
                call.duration = 0

    def _lock(self):
        """Serialise wrap-up, add-to-quote and transcript writes on this call."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(SQL('SELECT id FROM ariane_dialer_call WHERE id = %s FOR UPDATE', self.id))
        self.invalidate_recordset()

    def _stored(self, field):
        return json.loads(self[field]) if self[field] else None

    @api.model
    def _dialer_compliance(self, partner, product):
        """Kept for callers of v0.1. Use ariane.dialer.rules._dialer_availability."""
        return self.env['ariane.dialer.rules']._dialer_availability(partner, product)


class ArianeDialerCallSegment(models.Model):
    _name = 'ariane.dialer.call.segment'
    _description = 'Dialler call transcript line'
    _order = 'call_id, sequence, id'

    call_id = fields.Many2one('ariane.dialer.call', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='call_id.company_id', store=True)
    sequence = fields.Integer(required=True, default=0)
    start_s = fields.Float('Start (s)')
    end_s = fields.Float('End (s)')
    speaker = fields.Selection([('rep', 'Rep'), ('customer', 'Customer'), ('unknown', 'Unknown')],
                               default='unknown', required=True)
    text = fields.Text(required=True)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Only dialler users can read calls; without the group a plain partner read
    # (no field list) would raise on this field and fail the whole read.
    ariane_call_ids = fields.Many2many('ariane.dialer.call', compute='_compute_ariane_call_ids',
                                       string='Dialler calls', groups='ariane_dialer.group_dialer_user')

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_ariane_call_ids(self):
        """Calls for the company and all its people, newest first, capped at 200.
        Runs as the reader, so the dialler's record rules decide what is listed."""
        Call = self.env['ariane.dialer.call']
        for partner in self:
            commercial = partner.commercial_partner_id
            partner.ariane_call_ids = Call.search(
                [('commercial_partner_id', '=', commercial.id)], limit=200) if commercial.id else Call
