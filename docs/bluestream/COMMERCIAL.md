# Commercial model: Free Relay vs Premium Studio

## What is enforced (and where)

* **Backend, not UI:** every Studio mutation route checks the entitlement, and the
  **runtime** (`studio.cli argv`) refuses to start a Studio stream without a valid
  one (exit 5). Hiding buttons is not relied on.
* **Entitlement** = vendor-signed Ed25519 token `BS1.<payload>.<sig>` with `exp`,
  optional `nbf`, `state` (active/trial), optional install binding, and a bounded
  grace period (max 7 days) so a renewal hiccup does not kill a running stream.
  Verified **offline** (no licence server traffic). Tampering/wrong key/expired/not-yet-valid/
  wrong-plan/revoked are all rejected (see `webapp/test_studio_license.py`).
* **Revocation** = a vendor-signed list (`BS1R...`) saved as
  `/etc/bluestream/studio-revocations.token`; if never refreshed, expiry is the backstop.
* Free Relay and Playlist never import or call any of this.

## Vendor workflow

```
python3 tools/studio_license.py keygen --out vendor.key      # keep OFFLINE, never on a VPS
# install the printed public key on customers:
echo <pubhex> > /usr/local/lib/bluestream/studio/vendor_pubkey.hex
python3 tools/studio_license.py issue --key vendor.key --lic cust-0001 --days 30 --trial
python3 tools/studio_license.py revoke --key vendor.key --lic cust-0001
```
Without `vendor_pubkey.hex` Studio reports "licence verification key is not configured" and stays locked.

## Payments: NOT LIVE

`webapp/studio/payments.py` provides the provider interface, a timestamp-bound HMAC
webhook verifier (`t=...,v1=...`), replay window and idempotent event handling, all
tested. **No provider is integrated, no price is set, and no card data is touched by
this software.** Marking payments live requires, outside this repo: a chosen provider,
merchant account, test + live credentials, registered webhook endpoint (an internet-facing
service, not the customer VPS), refund and tax/VAT/legal handling, and end-to-end sandbox
tests. Until then licences are issued manually with the tool above.

## Subscription states shown to users

active, trial, grace (expired but inside grace), expired, revoked, not-yet-valid, invalid, none, unconfigured.

## Centralised cost

Customers run the media engine; the vendor needs only (later) a tiny licence/webhook
service and a support inbox. Licence checking needs **zero** runtime traffic today.
