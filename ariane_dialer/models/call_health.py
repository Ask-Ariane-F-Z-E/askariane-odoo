"""Call health: the compact summary the panel sends at the end of a call.

The panel measures the call in the browser (Twilio Voice SDK samples, connect
timings, an API ping, the coach's answer time) and keeps the raw samples in
memory only. Odoo stores what this module lets through: a handful of numbers
and a capped event timeline as JSON text. Never per-second samples, never a
file. Everything the browser sends is checked here again: a value of the wrong
type or out of range is dropped (stored as 0, shown as "not measured"), the
timeline is cut to HEALTH_MAX_EVENTS events and HEALTH_MAX_BYTES of JSON.
"""
import json
import math

HEALTH_MAX_EVENTS = 300
HEALTH_MAX_BYTES = 20000
# The whole request; a bigger body is refused before it is read.
HEALTH_BODY_MAX = 64 * 1024
EVENT_NAME_MAX = 40
EVENT_CODE_MAX = 40
EVENT_DETAIL_MAX = 120
# A call is never longer than this; a timing or offset beyond it is a bad value.
MS_MAX = 6 * 3600 * 1000
HANGUP_BY = [
    ('rep', 'Rep'),
    ('customer', 'Customer'),
    ('cancelled', 'Cancelled before it connected'),
    ('error', 'Error'),
    ('unknown', 'Unknown'),
]
# metric -> (low, high): Twilio's own units. rtt and jitter in ms, loss in
# percent of packets, MOS on its 1-5 scale.
METRICS = {'rtt': (0, 60000), 'jitter': (0, 60000), 'loss': (0, 100), 'mos': (1, 5)}
STATS = ('min', 'avg', 'p95')
TIMINGS = ('authorize_ms', 'connect_ms', 'ringing_ms', 'answer_ms')


def _number(value, low, high):
    """value when it is a real number inside [low, high], else None. A bool is
    not a number here (JSON true would otherwise read as 1)."""
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def _ms(value):
    """A duration in ms, at least 1 (0 is kept for "not measured")."""
    value = _number(value, 0, MS_MAX)
    return max(1, int(round(value))) if value is not None else 0


def _count(value, high=10 ** 6):
    value = _number(value, 0, high)
    return int(value) if value is not None else 0


def _short(value, limit):
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)[:limit] or None


def clean_events(events):
    """The timeline as compact JSON text, or False. Each event keeps t (ms from
    the click), ev (its name) and, when given, code and detail, all cut short.
    Events past the caps are dropped from the end and counted."""
    if not isinstance(events, list):
        return False, 0
    out, dropped = [], 0
    for raw in events:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        name = _short(raw.get('ev'), EVENT_NAME_MAX)
        t = _number(raw.get('t'), 0, MS_MAX)
        if not name or t is None:
            dropped += 1
            continue
        event = {'t': int(t), 'ev': name}
        for key, limit in (('code', EVENT_CODE_MAX), ('detail', EVENT_DETAIL_MAX)):
            value = _short(raw.get(key), limit)
            if value:
                event[key] = value
        out.append(event)
    if len(out) > HEALTH_MAX_EVENTS:
        dropped += len(out) - HEALTH_MAX_EVENTS
        out = out[:HEALTH_MAX_EVENTS]
    text = json.dumps(out, separators=(',', ':'))
    while out and len(text.encode()) > HEALTH_MAX_BYTES:
        cut = max(1, len(out) // 10)
        out, dropped = out[:-cut], dropped + cut
        text = json.dumps(out, separators=(',', ':'))
    return (text if out else False), dropped


def health_values(summary):
    """The call's health fields from the panel's summary dict. Anything not
    listed here (raw samples included) is ignored."""
    vals = {'health_samples': _count(summary.get('samples'))}
    for metric, (low, high) in METRICS.items():
        block = summary.get(metric)
        block = block if isinstance(block, dict) else {}
        for stat in STATS:
            value = _number(block.get(stat), low, high) if vals['health_samples'] else None
            vals[f'health_{metric}_{stat}'] = value if value is not None else 0.0
    timings = summary.get('timings')
    timings = timings if isinstance(timings, dict) else {}
    for key in TIMINGS:
        vals[f'health_{key}'] = _ms(timings.get(key))
    hangup = summary.get('hangup_by')
    vals['health_hangup_by'] = hangup if hangup in dict(HANGUP_BY) else 'unknown'
    vals['health_warning_count'] = _count(summary.get('warnings'))
    for key in ('api', 'coach'):
        block = summary.get(key)
        block = block if isinstance(block, dict) else {}
        vals[f'health_{key}_avg_ms'] = _ms(block.get('avg'))
        vals[f'health_{key}_p95_ms'] = _ms(block.get('p95'))
    api = summary.get('api') if isinstance(summary.get('api'), dict) else {}
    vals['health_api_errors'] = _count(api.get('errors'))
    timeline, dropped = clean_events(summary.get('events'))
    vals['health_timeline'] = timeline
    vals['health_events_dropped'] = dropped + _count(summary.get('events_dropped'))
    return vals
