"""Finishing a call (owner request, 21 Oct 2026), server side:

- a wrap-up needs a written note (the panel's "Write a note to finish" is only
  the first line of defence; the route refuses too);
- after a page reload the rep's own unfinished call is handed back to the panel
  (``/ariane_dialer/call/pending``), never another rep's;
- the AI summary of a call is posted once, as a short note, on the contact
  (never the full transcript), and goes again if the transcript is purged.

Every refusal has a positive control in the same test.
"""
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged, new_test_user

from .common import GenericRulesMixin
from .test_rules_v02 import INGEST, KEY, _sign

WRAPUP = '/ariane_dialer/call/wrapup'
PENDING = '/ariane_dialer/call/pending'


@tagged('post_install', '-at_install')
class TestHangupNotes(GenericRulesMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rep = new_test_user(cls.env, login='hnrep1', password='hnrep1pass',  # pragma: allowlist secret
                                groups='ariane_dialer.group_dialer_user')
        cls.rep2 = new_test_user(cls.env, login='hnrep2', password='hnrep2pass',  # pragma: allowlist secret
                                 groups='ariane_dialer.group_dialer_user')
        # a dialler manager can READ every rep's calls: only the route's own filter keeps theirs out
        cls.manager = new_test_user(cls.env, login='hnmgr', password='hnmgrpass1',  # pragma: allowlist secret
                                    groups='ariane_dialer.group_dialer_manager')
        cls.partner = cls.env['res.partner'].create({'name': 'Note Stores', 'phone': '+1 415 555 0177'})
        cls.icp = cls.env['ir.config_parameter'].sudo()
        cls.icp.set_param('ariane_dialer.tenant_id', 'tenant-test')

    def rpc(self, route, **params):
        return self.make_jsonrpc_request(route, params)

    def rpc_error(self, route, **params):
        res = self.url_open(route, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'}).json()
        self.assertIn('error', res, f'{route} was not refused')
        return res['error']['data']['message']

    def _start(self, login='hnrep1', password='hnrep1pass', preview=True):
        self.authenticate(login, password)
        return self.rpc('/ariane_dialer/call/start', phone='+14155550177',
                        partner_id=self.partner.id, preview=preview)['call_id']

    # ------------------------------------------------------------ N: a note is needed
    def test_wrapup_refuses_a_missing_or_blank_note(self):
        call_id = self._start()
        call = self.env['ariane.dialer.call'].browse(call_id)
        for note in (None, '', '   ', ' ab ', '\n\t\n'):
            params = {'call_id': call_id, 'outcome': 'interested'}
            if note is not None:
                params['note'] = note
            self.assertIn('Write a note to finish', self.rpc_error(WRAPUP, **params), repr(note))
            call.invalidate_recordset()
            self.assertNotEqual(call.state, 'done', repr(note))
        self.assertFalse(self.partner.message_ids.filtered(lambda m: 'Call: Interested' in (m.body or '')))
        # positive control: the same call finishes once there is a note
        self.rpc(WRAPUP, call_id=call_id, outcome='interested', note='Wants 10 cases')
        call.invalidate_recordset()
        self.assertEqual((call.state, call.note), ('done', 'Wants 10 cases'))

    def test_a_retried_wrapup_replays_without_asking_for_the_note_again(self):
        call_id = self._start()
        first = self.rpc(WRAPUP, call_id=call_id, outcome='interested', note='abc')
        # a retry that lost its body is answered with the first result
        self.assertEqual(self.rpc(WRAPUP, call_id=call_id, outcome='interested'), first)

    # ------------------------------------------------------------ R: reload hands the call back
    def test_pending_returns_the_reps_own_unfinished_call_only(self):
        call_id = self._start()
        self.rpc('/ariane_dialer/call/update', call_id=call_id, state='wrapup')
        got = self.rpc(PENDING)
        self.assertEqual((got['call_id'], got['partner_id'], got['state']), (call_id, self.partner.id, 'wrapup'))
        self.assertEqual(got['phone'], '+14155550177')
        # another rep never gets it, nor a manager who may read every call
        self.authenticate('hnrep2', 'hnrep2pass')
        self.assertIsNone(self.rpc(PENDING))
        self.authenticate('hnmgr', 'hnmgrpass1')
        self.assertEqual(self.env['ariane.dialer.call'].with_user(self.manager).search_count(
            [('id', '=', call_id)]), 1, 'precondition: the manager can read the call')
        self.assertIsNone(self.rpc(PENDING))
        # once finished it is not handed back
        self.authenticate('hnrep1', 'hnrep1pass')
        self.rpc(WRAPUP, call_id=call_id, outcome='interested', note='Wants 10 cases')
        self.assertIsNone(self.rpc(PENDING))

    def test_pending_hands_back_a_stale_live_call_but_not_a_fresh_one(self):
        Call = self.env['ariane.dialer.call']
        fresh = self._start(preview=False)
        self.rpc('/ariane_dialer/call/update', call_id=fresh, state='live')
        # a call still live in another tab is left alone
        self.assertIsNone(self.rpc(PENDING))
        # a call left 'live' long ago (the page closed mid-call) comes back
        Call.browse(fresh).write({'started_at': fields.Datetime.now() - timedelta(hours=3)})
        got = self.rpc(PENDING)
        self.assertEqual((got['call_id'], got['state']), (fresh, 'live'))

    def test_pending_prefers_the_newest_unfinished_call(self):
        older = self._start()
        self.rpc('/ariane_dialer/call/update', call_id=older, state='wrapup')
        self.env['ariane.dialer.call'].browse(older).write(
            {'started_at': fields.Datetime.now() - timedelta(hours=1)})
        newer = self._start()
        self.rpc('/ariane_dialer/call/update', call_id=newer, state='wrapup')
        self.assertEqual(self.rpc(PENDING)['call_id'], newer)

    # ------------------------------------------------------------ T: summary on the contact
    def _call(self, partner=None):
        return self.env['ariane.dialer.call'].create({
            'phone': '+14155550177', 'user_id': self.rep.id,
            'partner_id': (partner or self.partner).id, 'state': 'done'})

    def _push(self, call, rev=1, state='ready', summary='Wants 10 cases of Zeta, call back Friday',
              text='Rep: hi there\nCustomer: SECRET-TRANSCRIPT-LINE'):
        payload = {'tenant_id': 'tenant-test', 'call_id': call.id, 'provider_call_id': 'CA-sum',
                   'uid': call.user_id.id, 'company_id': call.company_id.id,
                   'revision': rev, 'transcript_state': state, 'text': text, 'summary': summary,
                   'segments': [{'start': 0, 'end': 1, 'speaker': 'customer', 'text': 'SECRET-TRANSCRIPT-LINE'}]}
        body = json.dumps(payload).encode()
        res = self.url_open(INGEST, data=body, headers={
            'Content-Type': 'application/json', 'X-Ariane-Signature': _sign(body)})
        call.invalidate_recordset()
        return res.status_code, res.json().get('code')

    def _summary_notes(self, partner=None):
        partner = partner or self.partner
        partner.invalidate_recordset(['message_ids'])
        return partner.message_ids.filtered(lambda m: 'Wants 10 cases of Zeta' in (m.body or ''))

    def test_ingest_posts_the_summary_once_on_the_contact_not_the_transcript(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self.assertEqual(self._push(call), (200, 'ok'))
        notes = self._summary_notes()
        self.assertEqual(len(notes), 1, 'the summary was not posted on the contact')
        self.assertNotIn('SECRET-TRANSCRIPT-LINE', notes.body, 'the full transcript went on the contact')
        self.assertIn(f'/odoo/ariane.dialer.call/{call.id}', notes.body, 'no link to the call record')
        self.assertEqual(notes.subtype_id, self.env.ref('mail.mt_note'))
        self.assertEqual(notes.author_id, self.rep.partner_id)
        # a re-sent or newer push does not post it again
        self.assertEqual(self._push(call, rev=1), (200, 'unchanged'))
        self.assertEqual(self._push(call, rev=2), (200, 'ok'))
        self.assertEqual(len(self._summary_notes()), 1, 'the summary was posted twice')

    def test_ingest_posts_nothing_without_a_summary_or_a_contact(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        before = len(self.partner.message_ids)
        self.assertEqual(self._push(call, summary=''), (200, 'ok'))
        self.partner.invalidate_recordset(['message_ids'])
        self.assertEqual(len(self.partner.message_ids), before)
        # positive control: the next revision, with a summary, posts it
        self.assertEqual(self._push(call, rev=2), (200, 'ok'))
        self.assertEqual(len(self._summary_notes()), 1)

    def test_a_purge_takes_the_summary_note_off_the_contact(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self._push(call)
        self.assertEqual(len(self._summary_notes()), 1)
        self.assertEqual(self._push(call, rev=2, state='purged'), (200, 'ok'))
        self.assertFalse(self._summary_notes(), 'a purged call left its summary on the contact')
