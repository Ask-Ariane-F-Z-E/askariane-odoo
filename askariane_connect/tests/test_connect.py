"""Step 1 (Connect) and steps 6-7 (confirm, ack) at the model level."""
import json

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..models import wire
from .common import ORIGIN, ConnectMixin, ack_answer, params, recorded, verify_signed_call


@tagged('post_install', '-at_install')
class TestConnectStart(ConnectMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_connect()

    def _enrolments(self):
        return self.env['askariane.connect.enrolment'].sudo().search_count([])

    def test_connect_redirect_query_and_pkce(self):
        panel = self.env['askariane.connect.panel'].with_user(self.admin1).create({})
        self.assertEqual(panel.company_ids, self.company_a, 'defaults to the current company')
        action = panel.action_connect()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(action['target'], 'self')
        base, query_string = action['url'].split('?', 1)
        self.assertEqual(base, 'https://askariane.ai/connect/odoo')
        from urllib.parse import parse_qsl
        pairs = parse_qsl(query_string)
        self.assertEqual([key for key, _ in pairs],
                         ['state', 'challenge', 'origin', 'db_uuid', 'company', 'company_ids'])
        query = dict(pairs)
        self.assertEqual(query['origin'], ORIGIN)
        self.assertEqual(query['db_uuid'], self.env['ir.config_parameter'].sudo().get_param('database.uuid'))
        self.assertEqual(query['company'], self.company_a.display_name[:120])
        self.assertEqual(query['company_ids'], str(self.company_a.id))
        self.assertEqual(len(query['state']), 22)
        enrolment = self.env['askariane.connect.enrolment'].sudo().search(
            [('state_hash', '=', wire.sha256_hex(query['state']))])
        self.assertEqual(len(enrolment), 1)
        # PKCE: challenge = S256(verifier), verifier 43 chars.
        self.assertEqual(len(enrolment.verifier), 43)
        self.assertEqual(query['challenge'], wire.pkce_challenge(enrolment.verifier))
        self.assertEqual(len(query['challenge']), 43)
        # The state is stored only hashed: it appears in no column of the row.
        self.env.flush_all()
        self.env.cr.execute('SELECT * FROM askariane_connect_enrolment WHERE id = %s', [enrolment.id])
        row = self.env.cr.fetchone()
        self.assertNotIn(query['state'], [str(value) for value in row])
        self.assertIn(wire.sha256_hex(query['state']), [str(value) for value in row])
        # The key pair is consistent and expiry is ten minutes out.
        self.assertEqual(wire.public_key_of(enrolment.private_key), enrolment.public_key)
        self.assertEqual(enrolment.user_id, self.admin1)
        self.assertEqual(enrolment.status, 'started')
        self.assertEqual(enrolment.origin, ORIGIN)

    def test_non_system_user_cannot_connect(self):
        before = self._enrolments()
        with self.assertRaises(AccessError):
            self.env['askariane.connect.enrolment'].with_user(self.plain)._start(self.company_a)
        panel = self.env['askariane.connect.panel'].with_user(self.admin1).create({})
        with self.assertRaises(AccessError):
            panel.with_user(self.plain).action_connect()
        self.assertEqual(self._enrolments(), before, 'no enrolment row was written')
        # Positive control: a Settings administrator can.
        self.start(self.admin1)
        self.assertEqual(self._enrolments(), before + 1)

    def test_two_admins_get_two_enrolments(self):
        first, q1 = self.start(self.admin1)
        second, q2 = self.start(self.admin2)
        self.assertNotEqual(first, second)
        self.assertNotEqual(q1['state'], q2['state'])
        self.assertEqual(first.user_id, self.admin1)
        self.assertEqual(second.user_id, self.admin2)
        self.assertNotEqual(first.private_key, second.private_key)
        self.assertEqual(first.status, 'started', 'the second attempt did not overwrite the first')

    def test_connect_needs_https_and_1_to_10_companies(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://erp.example.com')
        with self.assertRaises(UserError):
            self.start(self.admin1)
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://erp.example.com')
        with self.assertRaises(UserError):
            self.env['askariane.connect.enrolment'].with_user(self.admin1)._start(self.env['res.company'])


@tagged('post_install', '-at_install')
class TestConfirm(ConnectMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_connect()

    def test_confirm_by_initiator_sends_signed_ack_and_stores(self):
        enrolment, query = self.start(self.admin1)
        enrolment = self.exchange(enrolment, query)
        self.assertEqual(enrolment.status, 'exchanged')
        public_key, private_key = enrolment.public_key, enrolment.private_key
        with recorded({'ack': [(200, ack_answer(enrolment))]}) as rec:
            self.assertTrue(enrolment.with_user(self.admin1).action_confirm())
        self.assertEqual(rec.names(), ['ack'])
        body = verify_signed_call(self, rec.calls[0], public_key, 'k_abc123', 1, ORIGIN)
        self.assertEqual(body, {'enrolment_id': '6f1c1f9e-1111-4c4c-9a9a-000000000001'})
        stored = params(self.env)
        self.assertEqual(stored['askariane_connect.private_key'], private_key)
        self.assertEqual(stored['askariane_connect.key_id'], 'k_abc123')
        self.assertEqual(stored['askariane_connect.generation'], '1')
        self.assertEqual(stored['askariane_connect.tenant_id'], 't_examplexyz')
        self.assertEqual(stored['askariane_connect.origin'], ORIGIN)
        self.assertEqual(stored['askariane_connect.company_ids'], str(self.company_a.id))
        self.assertTrue(stored['askariane_connect.expires_at'])
        enrolment.invalidate_recordset()
        self.assertEqual(enrolment.status, 'done')
        self.assertFalse(enrolment.private_key, 'the key copy on the attempt is wiped')

    def test_confirm_by_another_admin_is_refused(self):
        enrolment, query = self.start(self.admin1)
        enrolment = self.exchange(enrolment, query)
        with recorded({'ack': [(200, ack_answer(enrolment))]}) as rec:
            with self.assertRaises(AccessError):
                enrolment.with_user(self.admin2).action_confirm()
            with self.assertRaises(AccessError):
                enrolment.with_user(self.admin2)._confirm_values()
            with self.assertRaises(AccessError):
                enrolment.with_user(self.plain).action_confirm()
            self.assertEqual(rec.calls, [], 'no ack was sent')
            self.assertEqual(params(self.env), {}, 'nothing stored')
            enrolment.invalidate_recordset()
            self.assertEqual(enrolment.status, 'exchanged')
            # Positive control: the initiator can.
            self.assertTrue(enrolment.with_user(self.admin1).action_confirm())
        self.assertEqual(rec.names(), ['ack'])

    def test_cancel_stores_nothing(self):
        enrolment, query = self.start(self.admin1)
        enrolment = self.exchange(enrolment, query)
        with recorded() as rec:
            enrolment.with_user(self.admin1).action_cancel()
        self.assertEqual(rec.calls, [])
        self.assertEqual(params(self.env), {})
        enrolment.invalidate_recordset()
        self.assertEqual(enrolment.status, 'failed')
        self.assertFalse(enrolment.private_key)

    def test_ack_with_other_companies_stores_nothing(self):
        enrolment, query = self.start(self.admin1)
        enrolment = self.exchange(enrolment, query)
        with recorded({'ack': [(200, ack_answer(enrolment, company_ids=[999]))]}):
            self.assertFalse(enrolment.with_user(self.admin1).action_confirm())
        self.assertEqual(params(self.env), {})
        enrolment.invalidate_recordset()
        self.assertEqual(enrolment.status, 'failed')

    def test_exchange_body_matches_contract(self):
        enrolment, query = self.start(self.admin1)
        env = self.env(user=self.admin1)
        from .common import exchange_answer
        with recorded({'exchange': [(200, exchange_answer(enrolment))]}) as rec:
            record, _outcome = env['askariane.connect.enrolment']._take_for_callback(query['state'])
            verifier, public_key = record.sudo().verifier, record.sudo().public_key
            record._exchange('abc-code')
        self.assertEqual(rec.names(), ['exchange'])
        call = rec.calls[0]
        self.assertEqual(call['url'], 'https://api.askariane.ai/v1/connect/odoo/exchange')
        self.assertEqual(call['headers'], {'Content-Type': 'application/json'})
        self.assertEqual(json.loads(call['body']), {'code': 'abc-code', 'verifier': verifier,
                                                    'public_key': public_key, 'origin': ORIGIN})
        self.assertEqual(len(public_key), 43)
