"""Disconnect and neutralize."""
from requests import ConnectionError as RequestsConnectionError

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tools.misc import file_open

from ..models import wire
from .common import ORIGIN, ConnectMixin, params, recorded, verify_signed_call


@tagged('post_install', '-at_install')
class TestDisconnect(ConnectMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_connect()

    def _panel(self, user):
        return self.env['askariane.connect.panel'].with_user(self.admin1).create({}).with_user(user)

    def test_disconnect_wipes_even_when_the_call_fails(self):
        self.store_connection()
        self.assertTrue(params(self.env))
        with recorded({'disconnect': [RequestsConnectionError('down')]}) as rec:
            action = self._panel(self.admin1).action_disconnect()
        self.assertEqual(rec.names(), ['disconnect'])
        self.assertEqual(params(self.env), {}, 'every key parameter is gone')
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('askariane.ai', action['params']['message'])

    def test_disconnect_wipes_on_error_answer(self):
        self.store_connection()
        with recorded({'disconnect': [(401, {'error': 'unknown_key'})]}):
            action = self._panel(self.admin1).action_disconnect()
        self.assertEqual(params(self.env), {})
        self.assertEqual(action['params']['type'], 'warning')

    def test_disconnect_signed_and_confirmed(self):
        private_key = self.store_connection()
        with recorded({'disconnect': [(200, {'revoked': True})]}) as rec:
            action = self._panel(self.admin1).action_disconnect()
        body = verify_signed_call(self, rec.calls[0], wire.public_key_of(private_key), 'k_abc123', 1, ORIGIN)
        self.assertEqual(body, {})
        self.assertEqual(rec.calls[0]['body'], b'{}')
        self.assertEqual(params(self.env), {})
        self.assertEqual(action['params']['type'], 'success')

    def test_non_system_user_cannot_disconnect(self):
        private_key = self.store_connection()
        with recorded({'disconnect': [(200, {'revoked': True})]}) as rec:
            with self.assertRaises(AccessError):
                self._panel(self.plain).action_disconnect()
            with self.assertRaises(AccessError):
                self.env['askariane.connect'].with_user(self.plain)._disconnect()
            self.assertEqual(rec.calls, [], 'no HTTP call')
            self.assertEqual(params(self.env)['askariane_connect.private_key'], private_key, 'key kept')
            # Positive control: an administrator can.
            self._panel(self.admin1).action_disconnect()
        self.assertEqual(rec.names(), ['disconnect'])
        self.assertEqual(params(self.env), {})

    def test_disconnect_with_changed_origin_sends_nothing_but_wipes(self):
        self.store_connection()
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://copy.example.net')
        with recorded({'disconnect': [(200, {'revoked': True})]}) as rec:
            action = self._panel(self.admin1).action_disconnect()
        self.assertEqual(rec.calls, [])
        self.assertEqual(params(self.env), {})
        self.assertEqual(action['params']['type'], 'warning')


@tagged('post_install', '-at_install')
class TestNeutralize(ConnectMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_connect()

    def test_neutralize_sql_wipes_keys_and_enrolments(self):
        # Attempts first (Connect refuses once connected), then a full connection.
        self.start(self.admin1)
        self.start(self.admin2)
        self.store_connection()
        self.env['askariane.connect']._store_connection({'askariane_connect.pending_private_key':
                                                         wire.new_private_key()})
        self.env.flush_all()
        self.env.cr.execute('SELECT count(*) FROM askariane_connect_enrolment')
        self.assertGreaterEqual(self.env.cr.fetchone()[0], 2)
        self.assertEqual(len(params(self.env)), 9)
        uuid_before = self.env['ir.config_parameter'].sudo().get_param('database.uuid')
        with file_open('askariane_connect/data/neutralize.sql') as handle:
            self.env.cr.execute(handle.read().strip())
        self.env.invalidate_all()
        self.env.registry.clear_cache()
        self.assertEqual(params(self.env), {})
        self.env.cr.execute('SELECT count(*) FROM askariane_connect_enrolment')
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.env.cr.execute('SELECT count(*) FROM askariane_connect_enrolment_company_rel')
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        # Positive control: other parameters survive.
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param('database.uuid'), uuid_before)
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param('web.base.url'), ORIGIN)
        self.assertIsNone(self.env['askariane.connect']._connection())

    def test_odoo_finds_the_neutralize_file(self):
        from odoo.modules import neutralize
        queries = list(neutralize.get_neutralization_queries(['askariane_connect']))
        self.assertEqual(len(queries), 1)
        self.assertIn('askariane_connect_enrolment', queries[0])
