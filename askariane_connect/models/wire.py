"""The wire format between this Odoo and Ask Ariane (ADR-0046 §6).

Pure functions plus one HTTP helper. Nothing here reads the database, so the
test vector in the tests can check the signing byte for byte.

Never log a code, a verifier, a signature, a private key or a body from here:
callers log ids and outcomes only.
"""
import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odoo.tools import config

CONNECT_PAGE = 'https://askariane.ai/connect/odoo'
API_BASE = 'https://api.askariane.ai'
SIGNATURE_PREFIX = 'askariane-v1'
ROTATE_PREFIX = 'askariane-v1-rotate'
PATH_PREFIX = '/v1/connect/odoo/'
TIMEOUT = 15
MAX_BODY = 64 * 1024
NONCE_RE = re.compile(r'^[A-Za-z0-9_-]{22,64}$')

# Fixed error codes Ask Ariane may answer with. Anything else is shown and
# handled as "unavailable", so no remote text reaches a screen or a log.
ERROR_CODES = frozenset((
    'bad_request', 'body_too_large', 'encoding_refused', 'query_refused', 'bad_signature',
    'unknown_key', 'stale_timestamp', 'replay', 'generation_mismatch', 'origin_mismatch',
    'key_expired', 'connection_inactive', 'schema_invalid', 'company_not_approved',
    'rate_limited', 'code_invalid', 'verifier_mismatch', 'enrolment_invalid', 'unavailable',
))


def connect_page():
    """Overridable only from the Odoo server config file, never from the database or a request."""
    return (config.get('askariane_connect_page') or CONNECT_PAGE).rstrip('?')


def api_base():
    return (config.get('askariane_connect_api_base') or API_BASE).rstrip('/')


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def b64u_decode(text):
    text = text.strip()
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def pkce_challenge(verifier):
    return b64u(hashlib.sha256(verifier.encode('ascii')).digest())


def new_private_key():
    """A fresh Ed25519 private key as b64u of its raw 32 bytes."""
    raw = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return b64u(raw)


def load_private_key(private_b64u):
    raw = b64u_decode(private_b64u)
    if len(raw) != 32:
        raise ValueError('bad key length')
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_of(private_b64u):
    pub = load_private_key(private_b64u).public_key()
    return b64u(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def sign(private_b64u, message):
    if isinstance(message, str):
        message = message.encode('utf-8')
    return b64u(load_private_key(private_b64u).sign(message))


def origin_of(url):
    """scheme://host[:port] of a URL, lower case, no path and no trailing slash.
    Only https counts: anything else, or a URL with a user name or password in
    it, gives None."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != 'https' or not parts.hostname or parts.username or parts.password:
        return None
    host = parts.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    if port and port != 443:
        return f'https://{host}:{port}'
    return f'https://{host}'


def canonical_body(obj):
    return json.dumps(obj, separators=(',', ':'), sort_keys=True, ensure_ascii=True).encode()


def signed_string(method, path, key_id, generation, origin, timestamp, nonce, body):
    return '\n'.join((
        SIGNATURE_PREFIX, method, path, key_id, str(int(generation)), origin, str(timestamp), nonce,
        hashlib.sha256(body).hexdigest(),
    ))


def rotate_proof(new_private_b64u, key_id, generation, nonce):
    """The new key's proof for a rotate call: it signs the NEXT generation and
    this very request's nonce."""
    message = '\n'.join((ROTATE_PREFIX, key_id, str(int(generation) + 1), nonce))
    return sign(new_private_b64u, message)


def signed_headers(name, body, private_b64u, key_id, generation, origin, timestamp, nonce):
    path = PATH_PREFIX + name
    message = signed_string('POST', path, key_id, generation, origin, timestamp, nonce, body)
    return {
        'Content-Type': 'application/json',
        'X-Ariane-Key-Id': key_id,
        'X-Ariane-Generation': str(int(generation)),
        'X-Ariane-Origin': origin,
        'X-Ariane-Timestamp': str(timestamp),
        'X-Ariane-Nonce': nonce,
        'X-Ariane-Signature': sign(private_b64u, message),
    }


def _read(response):
    """(status, dict) from a response. A body that is not a small JSON object
    reads as {} so no remote text is ever trusted or shown."""
    status = response.status_code
    try:
        if len(response.content or b'') > MAX_BODY:
            return status, {}
        data = response.json()
    except ValueError:
        return status, {}
    return status, data if isinstance(data, dict) else {}


def error_code(data):
    code = data.get('error') if isinstance(data, dict) else None
    return code if code in ERROR_CODES else 'unavailable'


def post_exchange(obj):
    """The one unsigned call: the code exchange. (status, dict); status 0 means
    Ask Ariane could not be reached."""
    body = canonical_body(obj)
    try:
        response = requests.post(api_base() + PATH_PREFIX + 'exchange', data=body,
                                 headers={'Content-Type': 'application/json'},
                                 timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return 0, {'error': 'unavailable'}
    return _read(response)


def post_signed(name, body_for_nonce, private_b64u, key_id, generation, origin):
    """Sign and send one request. ``body_for_nonce(nonce)`` builds the body, so
    a rotate proof can bind this request's nonce. Sends exactly the bytes that
    were hashed: no query string, no Content-Encoding."""
    nonce = secrets.token_urlsafe(24)
    timestamp = int(time.time())
    body = canonical_body(body_for_nonce(nonce))
    if len(body) > MAX_BODY:
        return 0, {'error': 'body_too_large'}
    headers = signed_headers(name, body, private_b64u, key_id, generation, origin, timestamp, nonce)
    try:
        response = requests.post(api_base() + PATH_PREFIX + name, data=body, headers=headers,
                                 timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return 0, {'error': 'unavailable'}
    return _read(response)
