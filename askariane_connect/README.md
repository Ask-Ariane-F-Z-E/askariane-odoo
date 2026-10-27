# Ask Ariane OS (`askariane_connect`)

Connects an Odoo 18 database (Odoo.sh or self-hosted) to Ask Ariane for a free
business diagnostic. Design: ADR-0046 rev 2 (one-time code exchange with PKCE,
an Ed25519 key made inside Odoo, final confirmation inside Odoo).

- **Connect**: Ask Ariane > Connection (Settings administrators only). Tick 1-10
  companies, sign in on askariane.ai, confirm in Odoo.
- **Snapshot**: a daily cron sends `snapshot.v1` counts for the approved companies
  only. Every count filters on `company_id = <that company>`; records with no
  company (shared) are not counted. Sales and invoice counts are 0 when those
  apps are not installed. The store page (`static/description/index.html`) lists
  every field sent.
- **Where the key lives**: the private connection key is stored in Odoo system
  parameters (`askariane_connect.private_key`), so it is visible to Odoo
  administrators (Settings access). Disconnecting wipes it.
- **Address**: `web.base.url` must be `https://<host>` on the standard port;
  an international host name is sent in punycode, another port is refused.
- **Origin pin**: nothing is signed or sent when `web.base.url` no longer matches
  the address pinned at connect.
- **Key rotation**: the cron rotates the key when fewer than 30 days remain.
- **Disconnect**: a signed disconnect, then the local key is wiped whatever the
  answer.
- **Neutralize**: `data/neutralize.sql` wipes every `askariane_connect.*`
  parameter and every connect attempt on a staging or dev copy.

Endpoints default to `https://askariane.ai/connect/odoo` and
`https://api.askariane.ai`. They can be changed only in the Odoo server config
file (`askariane_connect_page`, `askariane_connect_api_base`), for testing.

Tests: `odoo -i askariane_connect --test-enable --test-tags /askariane_connect`.
They never reach the network.

License: LGPL-3.
