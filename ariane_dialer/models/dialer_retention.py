"""Retention: old transcripts and call-health timelines are removed so the
dialler's data does not grow forever.

Runs as a daily ir.cron INSIDE the tenant's own Odoo (data/dialer_cron.xml),
never on Ask Ariane's servers. Settings (ir.config_parameter, Settings ->
Sales -> Dialler): transcript days (default 90) and timeline days (default
30); 0 keeps forever. An unreadable or negative value keeps forever too: a bad
setting must never delete more than it says.

What goes, for a call that STARTED before the limit, company by company:
- transcripts: the transcript text and its ariane.dialer.call.segment lines;
  the call gets a tombstone (transcript_purged_at) and the ingest refuses a
  later transcript for it;
- call health: the JSON event timeline; the numbers stay.
What stays: the call itself, its outcome, note, AI summary and next action,
and the short summary note on the contact. Nothing outside these two models
is read or written. At most RETENTION_BATCH calls per kind and company per
run; the cron framework runs it again (with a commit in between) while there
are more (ir.cron._notify_progress).
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

RETENTION_BATCH = 500
MAX_DAYS = 36500
RETENTION = {
    # kind: (parameter, default days)
    'transcript': ('ariane_dialer.transcript_retention_days', 90),
    'health': ('ariane_dialer.health_retention_days', 30),
}


def retention_days(env, kind):
    """Days to keep, 0 = keep forever. Unset = the default."""
    key, default = RETENTION[kind]
    raw = env['ir.config_parameter'].sudo().get_param(key)
    if raw in (None, False, ''):
        return default
    try:
        days = int(str(raw).strip())
    except ValueError:
        _logger.warning('ariane_dialer retention: %s=%r is not a number: keeping everything', key, raw)
        return 0
    if not 0 <= days <= MAX_DAYS:
        _logger.warning('ariane_dialer retention: %s=%r is out of range: keeping everything', key, raw)
        return 0
    return days


class ArianeDialerCall(models.Model):
    _inherit = 'ariane.dialer.call'

    @api.model
    def _cron_dialer_retention(self, batch=RETENTION_BATCH):
        done = remaining = 0
        for company in self.env['res.company'].sudo().search([], order='id'):
            d, r = self._dialer_retention_company(company, batch)
            done, remaining = done + d, remaining + r
        self.env['ir.cron']._notify_progress(done=done, remaining=remaining)
        return done, remaining

    @api.model
    def _dialer_retention_domain(self, kind, company, cutoff):
        domain = [('company_id', '=', company.id), ('started_at', '<', cutoff)]
        if kind == 'transcript':
            return domain + [('transcript_purged_at', '=', False),
                             '|', ('transcript', '!=', False), ('segment_ids', '!=', False)]
        return domain + [('health_timeline', '!=', False)]

    @api.model
    def _dialer_retention_company(self, company, batch=RETENTION_BATCH):
        """One company's run: (calls changed, calls still due). Superuser, but
        every search names this company, so another company's calls are never read."""
        Call = self.sudo().with_context(active_test=False)
        now = fields.Datetime.now()
        done = remaining = 0
        counts = {}
        for kind in ('transcript', 'health'):
            days = retention_days(self.env, kind)
            if not days:
                counts[kind] = 0
                continue
            domain = self._dialer_retention_domain(kind, company, fields.Datetime.subtract(now, days=days))
            calls = Call.search(domain, order='started_at, id', limit=batch)
            if kind == 'transcript':
                calls.segment_ids.unlink()
                calls.write({'transcript': False, 'transcript_purged_at': now})
            else:
                calls.write({'health_timeline': False, 'health_timeline_purged_at': now})
            counts[kind] = len(calls)
            done += len(calls)
            remaining += Call.search_count(domain) if len(calls) == batch else 0
        if done or remaining:
            _logger.info('ariane_dialer retention: company %s: %s transcripts, %s timelines removed; %s calls left',
                         company.id, counts['transcript'], counts['health'], remaining)
        return done, remaining
