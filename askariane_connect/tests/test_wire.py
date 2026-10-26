"""The wire format: the contract's test vector byte for byte, the signed
headers, the origin rule."""
import json

from odoo.tests import TransactionCase, tagged

from ..models import wire
from .common import recorded, verify, verify_signed_call

VECTOR_PRIVATE = wire.b64u(bytes(range(32)))
VECTOR_PUBLIC = 'A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg'
VECTOR_SIGNATURE = '_3HhhvBc25lLO-0g7Ciw-KUkyJGvmVLfn-PtDr7tPD9EsZeq3HkimBJgSKsgGRrV8FR6fZvfloLkToEGLGS-Cw'
VECTOR_NONCE = 'n' * 22


@tagged('post_install', '-at_install')
class TestWire(TransactionCase):

    def test_contract_test_vector(self):
        self.assertEqual(wire.public_key_of(VECTOR_PRIVATE), VECTOR_PUBLIC)
        headers = wire.signed_headers('disconnect', b'{}', VECTOR_PRIVATE, 'k_test', 1,
                                      'https://example.odoo.com', 1700000000, VECTOR_NONCE)
        self.assertEqual(headers['X-Ariane-Signature'], VECTOR_SIGNATURE)
        self.assertEqual(headers['X-Ariane-Generation'], '1')
        self.assertEqual(headers['X-Ariane-Timestamp'], '1700000000')
        self.assertEqual(wire.canonical_body({}), b'{}')
        message = wire.signed_string('POST', '/v1/connect/odoo/disconnect', 'k_test', 1,
                                     'https://example.odoo.com', 1700000000, VECTOR_NONCE, b'{}')
        self.assertEqual(message, 'askariane-v1\nPOST\n/v1/connect/odoo/disconnect\nk_test\n1\n'
                                  'https://example.odoo.com\n1700000000\n' + VECTOR_NONCE + '\n'
                                  '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a')
        verify(VECTOR_PUBLIC, VECTOR_SIGNATURE, message)
        # Positive control: a different nonce gives a different signature.
        other = wire.signed_headers('disconnect', b'{}', VECTOR_PRIVATE, 'k_test', 1,
                                    'https://example.odoo.com', 1700000000, 'm' * 22)
        self.assertNotEqual(other['X-Ariane-Signature'], VECTOR_SIGNATURE)

    def test_canonical_body_is_sorted_compact_ascii(self):
        self.assertEqual(wire.canonical_body({'b': 1, 'a': 'é'}), b'{"a":"\\u00e9","b":1}')

    def test_post_signed_sends_the_hashed_bytes_with_contract_headers(self):
        private_key = wire.new_private_key()
        with recorded({'snapshot': [(200, {'accepted': True, 'snapshot_id': 's1'})]}) as rec:
            status, data = wire.post_signed('snapshot', lambda nonce: {'z': 1, 'a': [1, 2]}, private_key,
                                            'k_1', 7, 'https://erp.example.com')
        self.assertEqual((status, data['accepted']), (200, True))
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call['url'], 'https://api.askariane.ai/v1/connect/odoo/snapshot')
        self.assertEqual(call['timeout'], 15)
        body = verify_signed_call(self, call, wire.public_key_of(private_key), 'k_1', 7,
                                  'https://erp.example.com')
        self.assertEqual(body, {'a': [1, 2], 'z': 1})
        self.assertEqual(call['body'], b'{"a":[1,2],"z":1}')

    def test_nonce_fresh_per_request(self):
        private_key = wire.new_private_key()
        with recorded({'snapshot': [(200, {})]}) as rec:
            wire.post_signed('snapshot', lambda nonce: {}, private_key, 'k', 1, 'https://a.example.com')
            wire.post_signed('snapshot', lambda nonce: {}, private_key, 'k', 1, 'https://a.example.com')
        nonces = [call['headers']['X-Ariane-Nonce'] for call in rec.calls]
        self.assertEqual(len(set(nonces)), 2)

    def test_pkce_challenge_is_s256(self):
        import base64
        import hashlib
        verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(wire.pkce_challenge(verifier), expected)
        self.assertEqual(wire.pkce_challenge(verifier), 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')

    def test_origin_of(self):
        self.assertEqual(wire.origin_of('https://ERP.Example.com/odoo/path?x=1'), 'https://erp.example.com')
        self.assertEqual(wire.origin_of('https://erp.example.com:443/'), 'https://erp.example.com')
        self.assertEqual(wire.origin_of('https://erp.example.com:8443'), 'https://erp.example.com:8443')
        self.assertIsNone(wire.origin_of('http://erp.example.com'))
        self.assertIsNone(wire.origin_of('https://user:pw@erp.example.com'))
        self.assertIsNone(wire.origin_of(''))
        self.assertIsNone(wire.origin_of(False))

    def test_unknown_error_codes_read_as_unavailable(self):
        self.assertEqual(wire.error_code({'error': 'replay'}), 'replay')
        self.assertEqual(wire.error_code({'error': '<script>'}), 'unavailable')
        self.assertEqual(wire.error_code({}), 'unavailable')
        self.assertEqual(json.loads(wire.canonical_body({'error': 'x'})), {'error': 'x'})
