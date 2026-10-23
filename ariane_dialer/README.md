# ariane_dialer (Odoo 18)

Generic in-Odoo softphone and sales panel. No tenant names, prices or numbers live here.
Feature notes by version are below; the repo README covers install and the server half.

## What it does today (v0.1)
- Systray phone button and a right-hand panel (Dialler users only).
- A "Dialler" button next to every phone field. Click opens the panel for that customer.
- Screen pop: customer, owner, pricelist, amount due, last orders, open quotes, to-dos.
- Product search with **this customer's pricelist price**, quantity breaks, free stock, last purchase,
  and the website's own links: accessories = "goes with", alternatives = "instead of".
- Prices listed in Settings → Sales → Dialler → "Sentinel prices" never show; the card says "Check price".
- Compliance line is red "Check compliance before pitching" until a tenant module overrides
  `ariane.dialer.call._dialer_compliance` with a positive rule.
- Add to quote: product + quantity only. Odoo prices the line. Draft quotations only.
- Wrap-up: outcome, note, call-back date → call record, a note on the customer or lead, a call-back to-do.
  "Do not call" adds the number to Odoo's phone blacklist when the rep is allowed to.

## Calling from a list (power dialler, v0.9)
- The idle panel shows **Today's calls**: the rep's own calls due today or overdue, one row per
  customer, earliest first (`/ariane_dialer/queue` → hook `ariane.dialer.rules._dialer_queue`).
  Generic: the rep's Call activities on contacts and leads. A tenant module overrides the hook (for example, with its own work requests).
- **Call next** dials the top row. **Skip** sets a row aside for this page only. At wrap-up,
  **Save and call <name>** saves and dials the next row. Nothing dials without a click.

## Coach product list (v0.8)
- 60 places: the customer's own buys (15), the company's featured products (20, hook
  `_dialer_featured_products`, e.g. other brands), then best sellers, taken one category at a time.
- Each line carries category, brand and how it ships (hook `_dialer_product_facts`).

## Coach marks and fillers (v0.10)
- The coach list now fills, in order: own buys (15), **clearance** (6), **hot in the customer's
  delivery state** (up to the hot count), **new sellers** (8), featured (14), then best sellers (60 in all).
- Each signed product line carries `badges` (`clearance`, `new_seller`, `hot`) and, when hot, `hot_rank`.
  The signed customer carries `hot_state`. Marks come from set membership, not from the pool.
- Clearance: product flag `dialer_clearance` (+ optional `dialer_clearance_until`), sales managers only.
- Hot: confirmed orders shipped to the state in the window; needs `hot_min_orders` separate orders
  **and** at least 2 different buyers; ranked by buyers, then units. Settings `hot_days` (30),
  `hot_count` (5, 0 = off), `hot_min_orders` (3).
- New sellers: hook `_dialer_new_seller_products`. Generic: none. A marketplace tenant overrides it.
- Clearance, new-seller and hot hooks each run in their own savepoint: a failure costs the mark only.
- Hot and new-seller lists are cached per worker (15 min / 1 h); Settings → "Refresh coach lists" resets them.
- Filler lines (`ariane_dialer.filler_phrases`): shown as "While I check…" while the coach answers a
  customer question or a product search is slow. Lines with a digit or currency sign are dropped.
- Panel: badges on recommendations, a "Selling in <state>" strip on coached calls.

## Ending and finishing a call (v0.11)
- **Hang up** shows from the moment the rep presses Call, while the call is still connecting.
  Pressed then, nothing is dialled; a call the phone places after it is cut at once.
  The call goes straight to wrap-up. (A server-side "end the call at Twilio" backup is not here:
  it needs the dialler service.)
- **A note is needed to finish.** Save stays off until the note has at least 3 characters
  ("Write a note to finish"); `/ariane_dialer/call/wrapup` refuses the same.
- **A reload does not skip the wrap-up.** On start, and when the panel opens, it asks
  `/ariane_dialer/call/pending` for the rep's OWN newest unfinished call (in wrap-up; or still
  dialling/live after 120 minutes) and puts it back in wrap-up. No new call until it is saved.
- **Summary on the contact.** When the dialler service pushes a transcript with a summary, one
  short note (the summary and a link to the call record, never the transcript) goes on the
  contact (or lead). Once per call; deleted again if the transcript is withheld or purged.
- No file, recording or binary is stored.

## Call health and data retention (v0.12)
- **Call health** in the panel, during and after a call: a quality dot (green / amber / red) from
  Twilio's per-second samples and its warnings; "Twilio edge round trip" (browser to Twilio's edge,
  not to the customer's phone), jitter, packet loss and MOS; connect timings (click → authorised →
  connected → ringing → answered); who hung up (rep / customer / cancelled / error); an "API
  connection" time (`/ariane_dialer/ping` every 10 s while the call is on); coach answer time and
  coach stream state; the last 40 events. A rep can hide it (remembered in this browser); a manager
  always can open it.
- At the end of the wrap-up the panel sends ONE summary to `/ariane_dialer/call/health`: only the
  call's own rep, once (a second send is "already_saved"), body ≤ 64 KB. Stored on the call as
  numbers (min / avg / p95 of rtt, jitter, loss, MOS; timings; hang-up; API and coach times) and a
  JSON timeline capped at 300 events and 20 KB. Never the raw samples, never a file. The server
  checks every value again (wrong type or out of range = 0, "not measured").
- Managers see it on the call form, **Call health** tab.
- **Retention** (Settings → Sales → Dialler → Data retention): a daily cron in this Odoo clears
  transcripts (text and lines) of calls that started more than 90 days ago, and health timelines
  after 30 days. 0 = keep forever; an unreadable or negative value also keeps everything. The call,
  its outcome, note, summary, next action and the health numbers stay; the call gets
  `transcript_purged_at` and the ingest then refuses a late transcript (409 `expired`). Company by
  company, 500 calls per kind per run, and the cron runs again while more are due.

## Preview mode
Until Settings → Sales → Dialler has the service URL, tenant id and signing key, the panel works
without telephony: the rep dials on their usual phone and uses the panel for the customer, products and log.

## Live calling (needs the Ask Ariane dialler service)
The browser asks Odoo for a 5-minute signed pass (`/ariane_dialer/session`, HS256, uid from the
server session), exchanges it at `{api}/v1/dialer/session` for a Twilio token, and dials only with an
authorisation from `{api}/v1/dialer/authorize`. Live coach cards arrive on `{api}/ws/coach/{CallSid}`.
Twilio Voice JS SDK 2.18.5 is vendored in `static/lib/twilio/` (Apache-2.0).

## Tests
`odoo -i ariane_dialer --test-tags /ariane_dialer` — 102 tests (two skip without website_sale; the JS test skips without Node).
JS rules alone (from `ariane_dialer/`): `node --test static/tests/` (58 tests).
