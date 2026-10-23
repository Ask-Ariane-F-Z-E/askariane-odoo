"""The coach card's plain rules (static/src/coach_view.js: pass-id guard,
fillers, the "Selling in {state}" strip, badges) and how the dialler service
uses them (static/src/dialer_service.js: the slow-search filler, the coach feed
handlers, the strip on a failed start, the "Customer mentioned" cards;
panel.xml's bindings) run under Node's
built-in test runner, from static/tests/*.test.mjs.

Node ships in the official Odoo image (rtlcss needs it). Where there is no
Node the test is SKIPPED and says so: a skip is not a pass.
"""
import os
import re
import shutil
import subprocess

from odoo.tests import BaseCase, tagged

JS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'tests')
JS_TESTS = [os.path.join(JS_DIR, name) for name in ('coach_view.test.mjs', 'dialer_service.test.mjs',
                                                  'hangup_wrapup.test.mjs',
                                                  'call_health.test.mjs', 'mentions.test.mjs')]


@tagged('post_install', '-at_install')
class TestCoachViewJs(BaseCase):

    def test_coach_view_rules_pass_under_node(self):
        node = shutil.which('node') or shutil.which('nodejs')
        if not node:
            self.skipTest('node is not installed: the coach_view.js rules were NOT checked')
        run = subprocess.run([node, '--test', '--test-reporter=tap', *JS_TESTS],
                             capture_output=True, text=True, timeout=120, check=False)
        out = run.stdout + run.stderr
        passed = re.search(r'^# pass (\d+)$', out, re.M)
        failed = re.search(r'^# fail (\d+)$', out, re.M)
        skipped = re.search(r'^# skipped (\d+)$', out, re.M)
        # a run that loads nothing reports "# pass 0": that is not a pass
        self.assertTrue(passed and int(passed.group(1)) > 0, out[-3000:])
        self.assertEqual((run.returncode, int(failed.group(1)) if failed else -1), (0, 0), out[-3000:])
        # every file ran: each names its tests in the report
        for marker in ('U5: a customer question', 'coach_pending shows a filler', 'H1: Hang up while',
                       'CH1: stats are min', 'M2: a mention asks'):
            self.assertIn(marker, out)
        self.assertEqual(int(skipped.group(1)) if skipped else -1, 0, out[-3000:])
