"""The callback and confirmation routes over real HTTP (step 4, 6, 7)."""
import json
import re
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from .common import BASE_URL, ConnectMixin, ack_answer, exchange_answer, params, recorded

CALLBACK = '/askariane/connect/callback'
PANEL_URL = '/odoo/action-askariane_connect.action_connect_panel'


@tagged('post_install', '-at_install')
class TestCallbackHttp(ConnectMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_connect()

    def setUp(self):
        super().setUp()
        # HttpCase sets web.base.url to its own http address; the flow needs https.
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', BASE_URL)

    def _callback(self, state, code='the-one-time-code'):
        return self.url_open(f'{CALLBACK}?code={code}&state={state}', allow_redirects=False)

    def _status(self, enrolment):
        enrolment.invalidate_recordset()
        return enrolment.status

    def test_correct_callback_exchanges_once_and_redirects_clean(self):
        enrolment, query = self.start(self.admin1)
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            response = self._callback(query['state'], code='secret-code-123')
        self.assertEqual(response.status_code, 303)
        location = response.headers['Location']
        self.assertTrue(location.endswith(f'/askariane/connect/confirm/{enrolment.id}'), location)
        self.assertNotIn('secret-code-123', location)
        self.assertNotIn(query['state'], location)
        self.assertNotIn('?', location)
        self.assertEqual(response.headers.get('Referrer-Policy'), 'no-referrer')
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
        self.assertEqual(rec.names(), ['exchange'])
        self.assertEqual(json.loads(rec.calls[0]['body'])['code'], 'secret-code-123')
        self.assertEqual(self._status(enrolment), 'exchanged')

    def test_wrong_state_is_refused_without_exchange(self):
        enrolment, query = self.start(self.admin1)
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            response = self._callback('A' * 22)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
            self.assertEqual(response.headers.get('Referrer-Policy'), 'no-referrer')
            self.assertEqual(rec.calls, [])
            self.assertEqual(self._status(enrolment), 'started')
            # Positive control: the right state works.
            self.assertEqual(self._callback(query['state']).status_code, 303)
        self.assertEqual(rec.names(), ['exchange'])

    def test_expired_enrolment_is_refused_without_exchange(self):
        enrolment, query = self.start(self.admin1)
        enrolment.write({'expires_at': fields.Datetime.now() - timedelta(seconds=1)})
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            response = self._callback(query['state'])
            self.assertEqual(response.status_code, 400)
            self.assertEqual(rec.calls, [])
            self.assertEqual(self._status(enrolment), 'started')
            # Positive control: the same attempt inside its window works.
            enrolment.write({'expires_at': fields.Datetime.now() + timedelta(minutes=5)})
            self.assertEqual(self._callback(query['state']).status_code, 303)
        self.assertEqual(rec.names(), ['exchange'])

    def test_other_admin_is_refused_without_exchange(self):
        enrolment, query = self.start(self.admin1)
        self.authenticate('aac_admin2', 'aac_admin2')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            response = self._callback(query['state'])
            self.assertEqual(response.status_code, 400)
            self.assertEqual(rec.calls, [])
            self.assertEqual(self._status(enrolment), 'started')
            # Positive control: the initiator can.
            self.authenticate('aac_admin1', 'aac_admin1')
            self.assertEqual(self._callback(query['state']).status_code, 303)
        self.assertEqual(rec.names(), ['exchange'])

    def test_second_use_is_refused(self):
        enrolment, query = self.start(self.admin1)
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            self.assertEqual(self._callback(query['state']).status_code, 303)
            second = self._callback(query['state'])
        self.assertEqual(second.status_code, 400)
        self.assertEqual(rec.names(), ['exchange'], 'exchange was called exactly once')

    def test_non_system_user_cannot_use_callback(self):
        enrolment, query = self.start(self.admin1)
        # The initiator loses Settings rights before coming back.
        self.admin1.write({'groups_id': [(3, self.env.ref('base.group_system').id)]})
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            response = self._callback(query['state'])
            self.assertEqual(response.status_code, 403)
            self.authenticate('aac_plain', 'aac_plain')
            self.assertEqual(self._callback(query['state']).status_code, 403)
            self.assertEqual(rec.calls, [])
            self.assertEqual(self._status(enrolment), 'started')
            # Positive control: rights back, the callback works.
            self.admin1.write({'groups_id': [(4, self.env.ref('base.group_system').id)]})
            self.authenticate('aac_admin1', 'aac_admin1')
            self.assertEqual(self._callback(query['state']).status_code, 303)
        self.assertEqual(rec.names(), ['exchange'])

    def test_exchange_refusal_marks_failed_and_redirects_clean(self):
        enrolment, query = self.start(self.admin1)
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(400, {'error': 'code_invalid'})]}):
            response = self._callback(query['state'])
        self.assertEqual(response.status_code, 303)
        self.assertNotIn('code', response.headers['Location'].split('/confirm/')[0])
        self.assertEqual(self._status(enrolment), 'failed')
        page = self.url_open(f'/askariane/connect/confirm/{enrolment.id}')
        self.assertIn('code_invalid', page.text)

    def test_confirm_page_and_post(self):
        enrolment, query = self.start(self.admin1)
        self.authenticate('aac_admin1', 'aac_admin1')
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}):
            self._callback(query['state'])
        # Another administrator cannot see the page or post to it.
        self.authenticate('aac_admin2', 'aac_admin2')
        other = self.url_open(f'/askariane/connect/confirm/{enrolment.id}', allow_redirects=False)
        self.assertEqual(other.status_code, 403)
        self.assertNotIn('owner@example.com', other.text)
        # The initiator sees who the data goes to.
        self.authenticate('aac_admin1', 'aac_admin1')
        page = self.url_open(f'/askariane/connect/confirm/{enrolment.id}', allow_redirects=False)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get('Referrer-Policy'), 'no-referrer')
        self.assertEqual(page.headers.get('Cache-Control'), 'no-store')
        self.assertIn('owner@example.com', page.text)
        self.assertIn(self.company_a.name, page.text)
        self.assertIn('open_invoices', page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        # admin2 posting with a valid token of their own is refused.
        self.authenticate('aac_admin2', 'aac_admin2')
        with recorded({'ack': [(200, ack_answer(enrolment))]}) as rec:
            refused = self.url_open(f'/askariane/connect/confirm/{enrolment.id}/decide',
                                    data={'csrf_token': self._csrf_for_session(), 'decision': 'confirm'},
                                    allow_redirects=False)
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(rec.calls, [])
            self.assertEqual(params(self.env), {})
            # The initiator confirms.
            self.authenticate('aac_admin1', 'aac_admin1')
            page = self.url_open(f'/askariane/connect/confirm/{enrolment.id}')
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            done = self.url_open(f'/askariane/connect/confirm/{enrolment.id}/decide',
                                 data={'csrf_token': csrf, 'decision': 'confirm'}, allow_redirects=False)
        self.assertEqual(done.status_code, 303)
        self.assertTrue(done.headers['Location'].endswith(PANEL_URL))
        self.assertEqual(rec.names(), ['ack'])
        self.env.invalidate_all()
        self.assertEqual(params(self.env)['askariane_connect.tenant_id'], 't_examplexyz')
        self.assertEqual(self._status(enrolment), 'done')

    def _csrf_for_session(self):
        """A CSRF token valid for the current session, taken from a page that
        renders one (the web client's home page carries it)."""
        page = self.url_open('/odoo')
        match = re.search(r'csrf_token\W+([0-9a-f]{40}o[0-9]*)', page.text)
        self.assertTrue(match, 'the web client page carries a CSRF token for this session')
        return match.group(1)
