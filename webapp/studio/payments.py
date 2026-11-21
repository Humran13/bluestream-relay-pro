"""Checkout-ready payment-provider ADAPTER (interface + verified-webhook
plumbing). NOT LIVE: no provider is configured, no price is hard-coded, and
nothing here collects card data. A provider is only "live" after a supported
provider, merchant account, credentials, webhooks, refunds, tax/legal handling
and end-to-end sandbox tests exist (see docs/bluestream/COMMERCIAL.md).

`SignedWebhookVerifier` implements the common `t=<ts>,v1=<hex hmac-sha256>`
scheme (timestamp-bound, constant-time compare, replay window) which several
providers use; each real adapter supplies its own secret and event mapping.
"""
import hashlib
import hmac
import json
import os
import time

REPLAY_WINDOW_S = 300


class PaymentError(Exception):
    pass


class PaymentProvider:
    """Interface a real provider adapter implements."""
    name = "unconfigured"
    live = False

    def create_checkout(self, plan, customer_ref):
        raise PaymentError("no payment provider is configured")

    def parse_event(self, payload):
        """-> {"id": str, "type": "paid|renewed|refunded|cancelled", "lic": str, ...}"""
        raise PaymentError("no payment provider is configured")


class NullProvider(PaymentProvider):
    pass


class SignedWebhookVerifier:
    def __init__(self, secret, tolerance=REPLAY_WINDOW_S):
        if not secret:
            raise PaymentError("webhook secret not configured")
        self.secret = secret if isinstance(secret, bytes) else secret.encode()
        self.tolerance = tolerance

    def sign(self, payload, ts=None):  # used by tests / local sandbox only
        ts = int(time.time() if ts is None else ts)
        mac = hmac.new(self.secret, b"%d." % ts + payload, hashlib.sha256).hexdigest()
        return "t=%d,v1=%s" % (ts, mac)

    def verify(self, payload, header, now=None):
        now = time.time() if now is None else now
        try:
            fields = dict(part.split("=", 1) for part in header.split(","))
            ts, sig = int(fields["t"]), fields["v1"]
        except (KeyError, ValueError, AttributeError):
            raise PaymentError("malformed signature header")
        if abs(now - ts) > self.tolerance:
            raise PaymentError("signature timestamp outside tolerance (replay?)")
        want = hmac.new(self.secret, b"%d." % ts + payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            raise PaymentError("signature mismatch")
        return True


class IdempotencyStore:
    """Remembers processed event ids so a re-delivered webhook is a no-op."""

    def __init__(self, path):
        self.path = path

    def _seen(self):
        try:
            with open(self.path) as fh:
                return set(json.load(fh))
        except (OSError, ValueError):
            return set()

    def first_time(self, event_id):
        seen = self._seen()
        if event_id in seen:
            return False
        seen.add(event_id)
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(sorted(seen)[-5000:], fh)
        os.replace(tmp, self.path)
        return True


def handle_webhook(verifier, store, payload, header, now=None):
    """Verify -> dedupe -> return the parsed event dict, or None for a duplicate."""
    verifier.verify(payload, header, now)
    try:
        event = json.loads(payload)
        event_id = str(event["id"])
    except (ValueError, KeyError, TypeError):
        raise PaymentError("malformed event")
    return event if store.first_time(event_id) else None
