# ariane-dialer-odoo

The Odoo 18 half of the **Ask Ariane dialler**: an in-Odoo softphone with a live sales coach.

This repo holds one generic addon, `ariane_dialer/`, at the root, so it can be added as a git
submodule and put straight on an Odoo addons path (Odoo.sh adds submodule folders for you).
No tenant names, prices, phone numbers or keys live here. Tenant-specific rules live in a
separate tenant addon that depends on this one, kept in that tenant's own repo.

## What it does

- Click-to-call softphone in the systray, and a "Dialler" button next to every phone field.
- Screen pop for the customer on the line: owner, pricelist, amount due, last orders, open quotes, to-dos.
- Catalogue search with this customer's price, stock, accessories and alternatives; add to a draft quote.
- Power dialler: "Today's calls", call next, skip.
- Live coach panel: product list, clearance / new-seller / "selling in your state" marks, filler lines,
  "Customer mentioned" cards.
- Wrap-up dialog that logs the call, a note and a call-back to-do. A note is required to finish.
- Call health timeline and retention crons for transcripts and health data.

Full feature notes: [`ariane_dialer/README.md`](ariane_dialer/README.md).

## Dependencies

- Odoo 18 (Community or Enterprise).
- Odoo modules: `mail`, `sale_management`, `stock`, `crm`.
- No extra Python packages. The Twilio Voice JS SDK is vendored in
  `ariane_dialer/static/lib/twilio/` (Apache-2.0, see its `LICENSE.md`).

## The server half

Telephony (Twilio Voice) and the live coach are served by the **Ask Ariane dialler service**
(the `backend/services/dialer` API in the Ask Ariane platform). This addon talks to it with a
signed pass and receives transcripts and call health back on its ingest routes.

Until the service is configured, the panel runs in **preview mode**: everything works except
placing the call itself.

## Install

1. Add this repo to your Odoo addons path. As a submodule:

   ```bash
   git submodule add git@github.com:Ask-Ariane-F-Z-E/ariane-dialer-odoo.git ariane-dialer-odoo
   ```

   then put `ariane-dialer-odoo` on the addons path (Odoo.sh does this for submodules).
   On Odoo.sh a private submodule needs a read-only deploy key: Odoo.sh project ->
   Settings -> Submodules -> copy the public key -> add it as a deploy key on this repo.
2. Update the apps list and install **Ariane Dialer**.
3. Settings -> Sales -> Dialler: set the dialler service URL, tenant id and signing key
   (given by Ask Ariane). Leave them empty for preview mode.
4. Give reps the Dialler -> User group (Settings -> Users).

A database copied from production (staging, a restore) has the dialler keys removed by
`ariane_dialer/data/neutralize.sql`.

## Tests

- Odoo tests: `odoo-bin -d <db> -i ariane_dialer --test-enable --stop-after-init`
  (or `--test-tags /ariane_dialer`).
- Panel JS tests, no Odoo needed: `node --test ariane_dialer/static/tests/` (Node 20+).

CI runs `compileall`, a ruff syntax check and the JS tests on every push and pull request.

## History

Split out of a tenant Odoo repo on 23 Oct 2026 with its git history kept. Older notes in the
code may point at design docs that stay in that repo.

## Licence

LGPL-3. Full text in `LICENSE`; it builds on the GPL-3, whose text is in `COPYING`.
