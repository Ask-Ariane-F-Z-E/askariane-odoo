-- A database copied from production (staging, a restore) must not hold the
-- dialler's keys: with them it could mint passes the live dialler service
-- trusts and accept pushes meant for production.
DELETE FROM ir_config_parameter
 WHERE key IN ('ariane_dialer.signing_key', 'ariane_dialer.ingest_key',
               'ariane_dialer.api_url', 'ariane_dialer.tenant_id');
