"""The "Connect to Ask Ariane" link in Settings: shown only while the dialler
service is not set up, and it carries no data about this database or user."""
import ast

from lxml import etree

from odoo.tests import TransactionCase, tagged

CONNECT_URL = 'https://askariane.ai/book?src=odoo-store'


def _truth(node, values):
    """The web client's reading of a plain boolean field expression (and, or,
    not, field names). Anything else fails the test rather than guess."""
    if isinstance(node, ast.Expression):
        return _truth(node.body, values)
    if isinstance(node, ast.BoolOp):
        parts = [_truth(v, values) for v in node.values]
        return all(parts) if isinstance(node.op, ast.And) else any(parts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _truth(node.operand, values)
    if isinstance(node, ast.Name):
        return bool(values[node.id])
    raise AssertionError(f'unexpected expression part: {ast.dump(node)}')


@tagged('post_install', '-at_install')
class TestSettingsConnectLink(TransactionCase):

    def _connect_block(self):
        arch = self.env['res.config.settings'].get_views([(False, 'form')])['views']['form']['arch']
        blocks = etree.fromstring(arch).xpath("//div[@name='ariane_dialer_connect']")
        self.assertEqual(len(blocks), 1, 'the connect block is in the rendered settings form')
        return blocks[0]

    def _shown(self, api_url, tenant_id, key='k-test'):
        """Set the parameters, open Settings as Odoo does, and read the block's
        invisible expression on the values the form gets."""
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('ariane_dialer.api_url', api_url)
        icp.set_param('ariane_dialer.tenant_id', tenant_id)
        icp.set_param('ariane_dialer.signing_key', key)
        settings = self.env['res.config.settings'].create({})
        values = {name: settings[name] for name in ('ariane_dialer_api_url', 'ariane_dialer_tenant_id',
                                                   'ariane_dialer_signing_key')}
        expression = ast.parse(self._connect_block().get('invisible'), mode='eval')
        return not _truth(expression, values)

    def test_link_target_carries_no_data(self):
        links = self._connect_block().xpath('.//a')
        self.assertEqual([a.get('href') for a in links], [CONNECT_URL])
        self.assertEqual(links[0].get('target'), '_blank')
        self.assertIn('Connect to Ask Ariane', ''.join(links[0].itertext()))

    def test_shown_only_when_unconfigured(self):
        self.assertTrue(self._shown(False, False, False), 'nothing set: shown')
        self.assertTrue(self._shown('https://dialer.example.com', False), 'no tenant id: shown')
        self.assertTrue(self._shown(False, 'tenant-1'), 'no service URL: shown')
        self.assertTrue(self._shown('https://dialer.example.com', 'tenant-1', False),
                        'URL and tenant but no signing key: shown')
        self.assertFalse(self._shown('https://dialer.example.com', 'tenant-1'), 'configured: hidden')
