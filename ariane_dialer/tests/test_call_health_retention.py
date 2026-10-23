"""v0.12: call health summary (route, validation, caps, manager tab) and the
retention cron (transcripts and health timelines, company by company).

Every denial test has a positive control in the same test.
"""
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged, new_test_user

from .common import GenericRulesMixin, new_company
from .test_rules_v02 import INGEST, KEY, _sign
# The spec's caps, written out (not imported: a test sized from the code's own
# constant would follow a wrong constant).
HEALTH_MAX_EVENTS = 300
HEALTH_MAX_BYTES = 20000

HEALTH = '/ariane_dialer/call/health'


def _summary(**over):
    s = {
        'samples': 42,
        'rtt': {'min': 20, 'avg': 35.5, 'p95': 80},
        'jitter': {'min': 1, 'avg': 3.2, 'p95': 9},
        'loss': {'min': 0, 'avg': 0.4, 'p95': 1.5},
        'mos': {'min': 3.9, 'avg': 4.3, 'p95': 4.4},
        'timings': {'authorize_ms': 120, 'connect_ms': 300, 'ringing_ms': 900, 'answer_ms': 4000},
        'hangup_by': 'customer', 'warnings': 2,
        'api': {'avg': 85, 'p95': 140, 'errors': 1},
        'coach': {'avg': 1800, 'p95': 2600},
        'events': [{'t': 0, 'ev': 'click'}, {'t': 120, 'ev': 'authorized'},
                   {'t': 5000, 'ev': 'warning', 'code': 'high-rtt'}],
        'events_dropped': 0,
        # raw per-second samples: never stored
        'raw': {'rtt': [20, 30, 40] * 10},
    }
    s.update(over)
    return s


@tagged('post_install', '-at_install')
class TestCallHealthRetention(GenericRulesMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rep = new_test_user(cls.env, login='hrrep1', password='hrrep1pass',  # pragma: allowlist secret
                                groups='ariane_dialer.group_dialer_user')
        cls.rep2 = new_test_user(cls.env, login='hrrep2', password='hrrep2pass',  # pragma: allowlist secret
                                 groups='ariane_dialer.group_dialer_user')
        cls.manager = new_test_user(cls.env, login='hrmgr', password='hrmgrpass1',  # pragma: allowlist secret
                                    groups='ariane_dialer.group_dialer_manager')
        cls.outsider = new_test_user(cls.env, login='hrout', password='hroutpass1',  # pragma: allowlist secret
                                     groups='base.group_user')
        cls.partner = cls.env['res.partner'].create({'name': 'Health Stores', 'is_company': True})
        cls.icp = cls.env['ir.config_parameter'].sudo()
        cls.icp.set_param('ariane_dialer.tenant_id', 'tenant-test')
        cls.Call = cls.env['ariane.dialer.call']

    def rpc(self, route, **params):
        return self.make_jsonrpc_request(route, params)

    def rpc_error(self, route, **params):
        res = self.url_open(route, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'}).json()
        self.assertIn('error', res, f'{route} was not refused')
        return res['error']['data']['message']

    def _call(self, user=None, days_ago=0, company=None, **vals):
        vals = {'phone': '+14155550199', 'user_id': (user or self.rep).id,
                'partner_id': self.partner.id, 'state': 'done',
                'started_at': fields.Datetime.now() - timedelta(days=days_ago), **vals}
        if company:
            vals['company_id'] = company.id
        return self.Call.create(vals)

    # ================================================================ health route
    def test_hr1_own_rep_saves_numbers_and_never_raw_samples(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        self.assertEqual(self.rpc(HEALTH, call_id=call.id, summary=_summary())['code'], 'saved')
        call.invalidate_recordset()
        self.assertTrue(call.health_saved_at)
        self.assertEqual(call.health_samples, 42)
        self.assertEqual((call.health_rtt_min, call.health_rtt_avg, call.health_rtt_p95), (20, 35.5, 80))
        self.assertEqual((call.health_mos_min, call.health_mos_avg), (3.9, 4.3))
        self.assertEqual(call.health_loss_p95, 1.5)
        self.assertEqual((call.health_authorize_ms, call.health_connect_ms, call.health_ringing_ms,
                          call.health_answer_ms), (120, 300, 900, 4000))
        self.assertEqual(call.health_hangup_by, 'customer')
        self.assertEqual((call.health_warning_count, call.health_api_avg_ms, call.health_api_p95_ms,
                          call.health_api_errors), (2, 85, 140, 1))
        self.assertEqual((call.health_coach_avg_ms, call.health_coach_p95_ms), (1800, 2600))
        timeline = json.loads(call.health_timeline)
        self.assertEqual([e['ev'] for e in timeline], ['click', 'authorized', 'warning'])
        self.assertEqual(timeline[2]['code'], 'high-rtt')
        # no raw sample is stored anywhere on the call
        self.assertNotIn('raw', call.health_timeline)
        self.assertNotIn('[20, 30, 40', json.dumps(call.read()[0], default=str))

    def test_hr2_second_send_is_already_saved_and_changes_nothing(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        self.assertEqual(self.rpc(HEALTH, call_id=call.id, summary=_summary())['code'], 'saved')
        call.invalidate_recordset()
        first = call.health_saved_at
        self.assertEqual(self.rpc(HEALTH, call_id=call.id,
                                  summary=_summary(samples=7, hangup_by='rep'))['code'], 'already_saved')
        call.invalidate_recordset()
        self.assertEqual((call.health_samples, call.health_hangup_by, call.health_saved_at),
                         (42, 'customer', first))

    def test_hr3_only_the_calls_own_rep_may_write(self):
        call = self._call()
        # another rep cannot even read the call (record rule); a manager can read it
        # but is not its rep
        self.authenticate('hrrep2', 'hrrep2pass')
        self.assertRegex(self.rpc_error(HEALTH, call_id=call.id, summary=_summary()),
                         "Not your call|doesn't have 'read' access")
        self.authenticate('hrmgr', 'hrmgrpass1')
        self.assertIn('Not your call', self.rpc_error(HEALTH, call_id=call.id, summary=_summary()))
        call.invalidate_recordset()
        self.assertFalse(call.health_saved_at)
        # positive control: the call's own rep
        self.authenticate('hrrep1', 'hrrep1pass')
        self.assertEqual(self.rpc(HEALTH, call_id=call.id, summary=_summary())['code'], 'saved')

    def test_hr4_values_are_validated(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        bad = _summary(
            rtt={'min': -5, 'avg': 'fast', 'p95': 999999},        # negative, text, too big
            jitter={'min': True, 'avg': None, 'p95': 12},
            loss={'min': 0, 'avg': 101, 'p95': 2},                 # >100 %
            mos={'min': 0.5, 'avg': 4.1, 'p95': 6},                # outside 1..5
            timings={'authorize_ms': -10, 'connect_ms': 0.4, 'ringing_ms': 'x',
                     'answer_ms': 10 ** 12},
            hangup_by='the dog', warnings=-3,
        )
        self.rpc(HEALTH, call_id=call.id, summary=bad)
        call.invalidate_recordset()
        self.assertEqual((call.health_rtt_min, call.health_rtt_avg, call.health_rtt_p95), (0, 0, 0))
        self.assertEqual((call.health_jitter_min, call.health_jitter_p95), (0, 12))   # bool is not 1
        self.assertEqual((call.health_loss_avg, call.health_loss_p95), (0, 2))
        self.assertEqual((call.health_mos_min, call.health_mos_avg, call.health_mos_p95), (0, 4.1, 0))
        self.assertEqual((call.health_authorize_ms, call.health_connect_ms, call.health_ringing_ms,
                          call.health_answer_ms), (0, 1, 0, 0))
        self.assertEqual(call.health_hangup_by, 'unknown')
        self.assertEqual(call.health_warning_count, 0)

    def test_hr4b_nan_and_infinity_are_dropped(self):
        # JSON has no NaN; Python's json module reads the bare words anyway.
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {
            'call_id': call.id, 'summary': _summary(rtt={'min': float('nan'), 'avg': float('inf'), 'p95': 50})}})
        self.assertIn('NaN', body)
        res = self.url_open(HEALTH, data=body, headers={'Content-Type': 'application/json'}).json()
        self.assertEqual(res.get('result', {}).get('code'), 'saved', res)
        call.invalidate_recordset()
        self.assertEqual((call.health_rtt_min, call.health_rtt_avg, call.health_rtt_p95), (0, 0, 50))

    def test_hr5_timeline_is_capped(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        # 330 events (under the 64 KB body cap): more than 300, and 300 of them
        # are still more than 20 KB
        events = [{'t': i, 'ev': f'e{i}', 'detail': 'd' * 130} for i in range(330)]
        events.insert(3, 'not an event')
        self.rpc(HEALTH, call_id=call.id, summary=_summary(events=events, events_dropped=5))
        call.invalidate_recordset()
        timeline = json.loads(call.health_timeline)
        self.assertLessEqual(len(call.health_timeline.encode()), HEALTH_MAX_BYTES)
        self.assertLessEqual(len(timeline), HEALTH_MAX_EVENTS)
        self.assertEqual(timeline[0]['ev'], 'e0')                 # cut from the END
        self.assertTrue(all(len(e['detail']) == 120 for e in timeline))
        self.assertEqual(call.health_events_dropped, 331 - len(timeline) + 5)
        # positive control: a small timeline is kept whole
        call2 = self._call()
        self.rpc(HEALTH, call_id=call2.id, summary=_summary())
        call2.invalidate_recordset()
        self.assertEqual(len(json.loads(call2.health_timeline)), 3)
        self.assertEqual(call2.health_events_dropped, 0)

    def test_hr5b_three_hundred_small_events_kept_and_one_more_dropped(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        self.rpc(HEALTH, call_id=call.id, summary=_summary(events=[{'t': i, 'ev': 'x'} for i in range(301)]))
        call.invalidate_recordset()
        self.assertEqual(len(json.loads(call.health_timeline)), 300)
        self.assertEqual(call.health_events_dropped, 1)

    def test_hr6_an_oversized_body_is_refused_before_writing(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        big = _summary(raw={'rtt': list(range(20000))})
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {'call_id': call.id, 'summary': big}})
        self.assertGreater(len(body), 64 * 1024)
        res = self.url_open(HEALTH, data=body, headers={'Content-Type': 'application/json'})
        refused = res.status_code == 413 or 'error' in (res.json() if res.status_code == 200 else {'error': 1})
        self.assertTrue(refused, res.text[:300])
        call.invalidate_recordset()
        self.assertFalse(call.health_saved_at)
        # positive control: the same summary without the raw block is stored
        self.assertEqual(self.rpc(HEALTH, call_id=call.id, summary=_summary())['code'], 'saved')

    def test_hr7_a_summary_that_is_not_an_object_is_refused(self):
        call = self._call()
        self.authenticate('hrrep1', 'hrrep1pass')
        self.assertIn('not readable', self.rpc_error(HEALTH, call_id=call.id, summary=['a']))
        self.assertIn('not readable', self.rpc_error(HEALTH, call_id=call.id))
        call.invalidate_recordset()
        self.assertFalse(call.health_saved_at)
        self.assertEqual(self.rpc(HEALTH, call_id=call.id, summary=_summary())['code'], 'saved')

    def test_hr8_ping_is_for_dialler_users_only(self):
        self.authenticate('hrout', 'hroutpass1')
        self.rpc_error('/ariane_dialer/ping')
        self.authenticate('hrrep1', 'hrrep1pass')
        self.assertEqual(self.rpc('/ariane_dialer/ping'), {'ok': True})

    def test_hr9_call_health_tab_is_for_managers(self):
        def arch(user):
            return self.Call.with_user(user).get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('name="call_health"', arch(self.manager))
        self.assertIn('health_rtt_avg', arch(self.manager))
        self.assertNotIn('name="call_health"', arch(self.rep))
        self.assertNotIn('health_rtt_avg', arch(self.rep))

    # ================================================================ retention
    def _push(self, call, rev=1, state='ready'):
        payload = {'tenant_id': 'tenant-test', 'call_id': call.id, 'provider_call_id': f'CA-{call.id}',
                   'uid': call.user_id.id, 'company_id': call.company_id.id, 'revision': rev,
                   'transcript_state': state, 'text': 'Rep: hi\nCustomer: hello', 'summary': 'Said hello',
                   'segments': [{'start': 0, 'end': 1.5, 'speaker': 'rep', 'text': 'hi'},
                                {'start': 1.6, 'end': 3, 'speaker': 'customer', 'text': 'hello'}]}
        body = json.dumps(payload).encode()
        res = self.url_open(INGEST, data=body, headers={
            'Content-Type': 'application/json', 'X-Ariane-Signature': _sign(body)})
        call.invalidate_recordset()
        return res.status_code, res.json().get('code')

    def _with_transcript(self, days_ago, **kw):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call(note='Talked about stock', **kw)
        self.assertEqual(self._push(call), (200, 'ok'))
        call.started_at = fields.Datetime.now() - timedelta(days=days_ago)
        call.flush_recordset()
        self.assertTrue(call.transcript and call.segment_ids)
        return call

    def _with_timeline(self, days_ago, **kw):
        call = self._call(days_ago=days_ago, **kw)
        call.write({'health_timeline': '[{"t":0,"ev":"click"}]', 'health_saved_at': fields.Datetime.now(),
                    'health_rtt_avg': 40, 'health_mos_avg': 4.2, 'health_hangup_by': 'rep'})
        return call

    def _run(self, batch=500):
        self.env.invalidate_all()
        out = self.Call._cron_dialer_retention(batch)
        self.env.invalidate_all()
        return out

    def test_rt1_old_transcripts_go_newer_ones_stay(self):
        self.icp.set_param('ariane_dialer.transcript_retention_days', '90')
        self.icp.set_param('ariane_dialer.health_retention_days', '0')
        old = self._with_transcript(100)
        recent = self._with_transcript(10)
        edge = self._with_transcript(89)
        note_msg = old.summary_message_id
        seg_ids = old.segment_ids.ids
        self._run()
        self.assertFalse(old.transcript)
        self.assertFalse(old.segment_ids)
        self.assertFalse(self.env['ariane.dialer.call.segment'].browse(seg_ids).exists())
        self.assertTrue(old.transcript_purged_at)
        # what stays on the old call
        self.assertEqual((old.summary, old.note, old.state), ('Said hello', 'Talked about stock', 'done'))
        if note_msg:
            self.assertTrue(note_msg.exists())
        # positive controls: newer calls are untouched
        for call in (recent, edge):
            self.assertTrue(call.transcript)
            self.assertEqual(len(call.segment_ids), 2)
            self.assertFalse(call.transcript_purged_at)

    def test_rt2_old_timelines_go_numbers_stay(self):
        self.icp.set_param('ariane_dialer.transcript_retention_days', '0')
        self.icp.set_param('ariane_dialer.health_retention_days', '30')
        old = self._with_timeline(40)
        recent = self._with_timeline(20)
        self._run()
        self.assertFalse(old.health_timeline)
        self.assertTrue(old.health_timeline_purged_at)
        self.assertEqual((old.health_rtt_avg, old.health_mos_avg, old.health_hangup_by), (40, 4.2, 'rep'))
        self.assertTrue(old.health_saved_at)
        self.assertTrue(recent.health_timeline)
        self.assertFalse(recent.health_timeline_purged_at)

    def test_rt3_zero_or_bad_settings_keep_everything_and_unset_uses_defaults(self):
        old_t = self._with_transcript(400)
        old_h = self._with_timeline(400)
        for value in ('0', 'abc', '-5'):
            self.icp.set_param('ariane_dialer.transcript_retention_days', value)
            self.icp.set_param('ariane_dialer.health_retention_days', value)
            self.assertEqual(self._run(), (0, 0), value)
            self.assertTrue(old_t.transcript, value)
            self.assertTrue(old_h.health_timeline, value)
        # unset: the defaults (90 / 30) apply
        mid_t = self._with_transcript(60)    # inside 90
        mid_h = self._with_timeline(25)      # inside 30
        self.icp.search([('key', 'in', ('ariane_dialer.transcript_retention_days',
                                        'ariane_dialer.health_retention_days'))]).unlink()
        self._run()
        self.assertFalse(old_t.transcript)
        self.assertFalse(old_h.health_timeline)
        self.assertTrue(mid_t.transcript)
        self.assertTrue(mid_h.health_timeline)

    def test_rt4_each_company_on_its_own(self):
        self.icp.set_param('ariane_dialer.transcript_retention_days', '90')
        self.icp.set_param('ariane_dialer.health_retention_days', '30')
        other = new_company(self.env, 'Retention Co 2')
        here = self._with_timeline(40)
        there = self._with_timeline(40, company=other)
        done, remaining = self.Call._dialer_retention_company(self.env.company)
        self.env.invalidate_all()
        self.assertEqual((done, remaining), (1, 0))
        self.assertFalse(here.health_timeline)
        self.assertTrue(there.health_timeline)        # the other company is not read
        # positive control: the cron goes through every company
        self._run()
        self.assertFalse(there.health_timeline)

    def test_rt5_batches_report_what_is_left(self):
        self.icp.set_param('ariane_dialer.transcript_retention_days', '0')
        self.icp.set_param('ariane_dialer.health_retention_days', '30')
        calls = self._with_timeline(40) | self._with_timeline(41) | self._with_timeline(42)
        self.assertEqual(self._run(batch=2), (2, 1))
        self.assertEqual(len(calls.filtered('health_timeline')), 1)
        self.assertEqual(self._run(batch=2), (1, 0))
        self.assertFalse(calls.filtered('health_timeline'))
        self.assertEqual(self._run(batch=2), (0, 0))

    def test_rt6_nothing_outside_the_dialler_is_touched(self):
        self.icp.set_param('ariane_dialer.transcript_retention_days', '90')
        self.icp.set_param('ariane_dialer.health_retention_days', '30')
        partner = self.env['res.partner'].create({'name': 'Old contact', 'comment': 'keep me'})
        msg = partner.message_post(body='An old note')
        self.env.cr.execute("UPDATE res_partner SET create_date = now() - interval '400 days' WHERE id = %s",
                            [partner.id])
        self.env.cr.execute("UPDATE mail_message SET date = now() - interval '400 days' WHERE id = %s", [msg.id])
        old = self._with_transcript(100)
        self._run()
        self.assertFalse(old.transcript)              # positive control: the run did run
        self.assertTrue(partner.exists())
        self.assertIn('keep me', str(partner.comment))
        self.assertTrue(msg.exists())
        self.assertIn('An old note', str(msg.body))

    def test_rt7_ingest_refuses_a_transcript_after_retention(self):
        self.icp.set_param('ariane_dialer.transcript_retention_days', '90')
        self.icp.set_param('ariane_dialer.health_retention_days', '0')
        old = self._with_transcript(100)
        self._run()
        self.assertEqual(self._push(old, rev=2), (409, 'expired'))
        self.assertFalse(old.transcript)
        self.assertFalse(old.segment_ids)
        # positive control: a purge push still applies
        self.assertEqual(self._push(old, rev=3, state='purged'), (200, 'ok'))
        self.assertEqual(old.transcript_state, 'purged')
        self.assertFalse(old.summary)

    def test_rt8_the_daily_cron_runs_the_retention(self):
        cron = self.env.ref('ariane_dialer.cron_dialer_retention')
        self.assertTrue(cron.active)
        self.assertEqual((cron.interval_number, cron.interval_type), (1, 'days'))
        self.assertEqual(cron.model_id.model, 'ariane.dialer.call')
        self.icp.set_param('ariane_dialer.transcript_retention_days', '0')
        self.icp.set_param('ariane_dialer.health_retention_days', '30')
        old = self._with_timeline(40)
        cron.method_direct_trigger()
        self.env.invalidate_all()
        self.assertFalse(old.health_timeline)

    def test_rt9_settings_round_trip(self):
        settings = self.env['res.config.settings'].create({})
        self.assertEqual((settings.ariane_dialer_transcript_retention_days,
                          settings.ariane_dialer_health_retention_days), (90, 30))
        settings.write({'ariane_dialer_transcript_retention_days': 0,
                        'ariane_dialer_health_retention_days': 7})
        settings.execute()
        self.assertEqual(self.icp.get_param('ariane_dialer.transcript_retention_days'), '0')
        self.assertEqual(self.icp.get_param('ariane_dialer.health_retention_days'), '7')
        from ..models.dialer_retention import retention_days
        self.assertEqual((retention_days(self.env, 'transcript'), retention_days(self.env, 'health')), (0, 7))
