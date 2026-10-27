"""Shared helpers. No test reaches the network: requests.post is always
replaced by a recorder that answers from a script."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from odoo.tests import new_test_user

from ..models import wire
from ..models import connect_service as svc

BASE_URL = 'https://erp.example.com'
ORIGIN = 'https://erp.example.com'


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self.content = json.dumps(data).encode() if data is not None else b''

    def json(self):
        if not self.content:
            raise ValueError('no body')
        return json.loads(self.content)


class Recorder:
    """Stands in for requests.post. ``answers`` maps the last path segment
    (exchange, ack, snapshot, rotate, disconnect) to a list of answers, used in
    order; an answer is (status, dict) or an exception instance to raise."""

    def __init__(self, answers=None):
        self.answers = {name: list(items) for name, items in (answers or {}).items()}
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=None, allow_redirects=True, **kwargs):
        name = url.rsplit('/', 1)[-1]
        self.calls.append({'url': url, 'name': name, 'body': data, 'headers': dict(headers or {}),
                           'timeout': timeout, 'kwargs': kwargs})
        queue = self.answers.get(name) or [(500, {'error': 'unavailable'})]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(*answer)

    def names(self):
        return [call['name'] for call in self.calls]


@contextmanager
def recorded(answers=None):
    recorder = Recorder(answers)
    with patch.object(wire.requests, 'post', recorder):
        yield recorder


def iso_in(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def verify(public_b64u, signature_b64u, message):
    """Raises when the signature does not verify."""
    key = Ed25519PublicKey.from_public_bytes(wire.b64u_decode(public_b64u))
    key.verify(wire.b64u_decode(signature_b64u), message.encode() if isinstance(message, str) else message)


def verify_signed_call(test, call, public_b64u, key_id, generation, origin):
    """Checks one recorded signed call against the contract and returns its body."""
    headers = call['headers']
    test.assertEqual(headers['Content-Type'], 'application/json')
    test.assertEqual(headers['X-Ariane-Key-Id'], key_id)
    test.assertEqual(headers['X-Ariane-Generation'], str(generation))
    test.assertEqual(headers['X-Ariane-Origin'], origin)
    test.assertRegex(headers['X-Ariane-Nonce'], r'^[A-Za-z0-9_-]{22,64}$')
    test.assertRegex(headers['X-Ariane-Timestamp'], r'^\d+$')
    test.assertNotIn('Content-Encoding', headers)
    test.assertNotIn('?', call['url'])
    body = call['body']
    test.assertIsInstance(body, bytes)
    obj = json.loads(body)
    test.assertEqual(body, wire.canonical_body(obj), 'exactly the canonical bytes are sent')
    message = wire.signed_string('POST', '/v1/connect/odoo/' + call['name'], key_id, generation, origin,
                                 headers['X-Ariane-Timestamp'], headers['X-Ariane-Nonce'], body)
    verify(public_b64u, headers['X-Ariane-Signature'], message)
    return obj


def exchange_answer(enrolment, **overrides):
    data = {
        'enrolment_id': '6f1c1f9e-1111-4c4c-9a9a-000000000001',
        'key_id': 'k_abc123',
        'account_email': 'owner@example.com',
        'tenant_display_name': 'Example account',
        'scope': ['snapshot.v1'],
        'company_ids': sorted(enrolment.sudo().company_ids.ids),
        'ack_expires_at': iso_in(0.02),
    }
    data.update(overrides)
    return data


def ack_answer(enrolment, **overrides):
    data = {
        'tenant_id': 't_examplexyz',
        'key_id': 'k_abc123',
        'generation': 1,
        'company_ids': sorted(enrolment.sudo().company_ids.ids),
        'expires_at': iso_in(90),
    }
    data.update(overrides)
    return data


def params(env):
    """Every askariane_connect.* parameter, read straight from the table."""
    env.flush_all()
    env.cr.execute("SELECT key, value FROM ir_config_parameter WHERE key LIKE 'askariane\\_connect.%%'")
    return dict(env.cr.fetchall())


class ConnectMixin:
    """Users, an https base URL, and helpers to walk the flow."""

    @classmethod
    def _setup_connect(cls):
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', BASE_URL)
        cls.company_a = cls.env.company
        cls.admin1 = new_test_user(cls.env, login='aac_admin1', groups='base.group_user,base.group_system')
        cls.admin2 = new_test_user(cls.env, login='aac_admin2', groups='base.group_user,base.group_system')
        cls.plain = new_test_user(cls.env, login='aac_plain', groups='base.group_user')

    def start(self, user=None, companies=None):
        """Connect as ``user``; returns (enrolment, query dict)."""
        user = user or self.admin1
        env = self.env(user=user)
        companies = companies or self.company_a
        url = env['askariane.connect.enrolment']._start(companies.with_env(env))
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        enrolment = self.env['askariane.connect.enrolment'].sudo().search(
            [('state_hash', '=', wire.sha256_hex(query['state']))])
        self.assertEqual(len(enrolment), 1)
        return enrolment, query

    def exchange(self, enrolment, query, user=None, **answer):
        env = self.env(user=user or self.admin1)
        with recorded({'exchange': [(200, exchange_answer(enrolment, **answer))]}):
            record, outcome = env['askariane.connect.enrolment']._take_for_callback(query['state'])
            self.assertEqual(outcome, 'ok')
            self.assertTrue(record._exchange('the-one-time-code'))
        return record

    def store_connection(self, company_ids=None, expires_days=80, origin=ORIGIN, generation=1):
        private_key = wire.new_private_key()
        self.env['askariane.connect']._store_connection({
            svc.P_PRIVATE_KEY: private_key,
            svc.P_KEY_ID: 'k_abc123',
            svc.P_GENERATION: str(generation),
            svc.P_TENANT_ID: 't_examplexyz',
            svc.P_ORIGIN: origin,
            svc.P_COMPANY_IDS: ','.join(str(cid) for cid in (company_ids or [self.company_a.id])),
            svc.P_EXPIRES_AT: iso_in(expires_days),
            svc.P_ACCOUNT_EMAIL: 'owner@example.com',
        })
        return private_key


__all__ = ['requests', 'BASE_URL', 'ORIGIN', 'Recorder', 'recorded', 'iso_in', 'verify',
           'verify_signed_call', 'exchange_answer', 'ack_answer', 'params', 'ConnectMixin']
