"""Licence / entitlement / payment-adapter tests (offline, no network)."""
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from webapp.studio import cli, ed25519, entitlement, payments

SK = bytes(range(32))
PK = ed25519.public_key(SK)


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vector_1(self):
        sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        pk = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(ed25519.public_key(sk), pk)
        self.assertEqual(ed25519.sign(sk, b""), sig)
        self.assertTrue(ed25519.verify(pk, b"", sig))
        self.assertFalse(ed25519.verify(pk, b"x", sig))

    def test_malformed_inputs_never_raise(self):
        for bad in (b"", b"x" * 64, b"\xff" * 64):
            self.assertFalse(ed25519.verify(PK, b"m", bad))
        self.assertFalse(ed25519.verify(b"short", b"m", b"x" * 64))


class EntitlementTests(unittest.TestCase):
    NOW = 1_900_000_000

    def tok(self, **kw):
        doc = {"v": 1, "lic": "L1", "plan": "studio", "exp": self.NOW + 1000, "nbf": self.NOW - 10}
        doc.update(kw)
        return entitlement.make_token(SK, doc)

    def check(self, token, now=None, **kw):
        return entitlement.check(token, PK, now or self.NOW, **kw)

    def test_valid_trial_and_states(self):
        self.assertEqual(self.check(self.tok()).state, "active")
        self.assertEqual(self.check(self.tok(state="trial")).state, "trial")
        self.assertTrue(self.check(self.tok()).ok)

    def test_expiry_grace_and_hard_expiry(self):
        t = self.tok(grace=3600)
        self.assertEqual(self.check(t, self.NOW + 1500).state, "grace")
        self.assertTrue(self.check(t, self.NOW + 1500).ok)
        self.assertEqual(self.check(t, self.NOW + 1000 + 3601).state, "expired")
        self.assertFalse(self.check(self.tok(), self.NOW + 2000).ok)  # no grace set
        huge = self.tok(grace=10 ** 9)  # capped at MAX_GRACE
        self.assertFalse(self.check(huge, self.NOW + 1000 + entitlement.MAX_GRACE + 5).ok)

    def test_tamper_wrong_key_and_garbage(self):
        good = self.tok()
        head, _body, sig = good.split(".")
        forged = entitlement.make_token(SK, {"v": 1, "lic": "L1", "plan": "studio",
                                             "exp": self.NOW + 10 ** 9})
        self.assertFalse(self.check(".".join([head, forged.split(".")[1], sig])).ok)  # payload swap
        other = ed25519.public_key(bytes(range(1, 33)))
        self.assertFalse(entitlement.check(good, other, self.NOW).ok)
        for junk in ("", "BS1.x.y", "nope", "BS1..", None):
            self.assertFalse(self.check(junk).ok)
        self.assertEqual(entitlement.check(good, b"", self.NOW).state, "unconfigured")

    def test_not_yet_valid_plan_install_binding(self):
        self.assertEqual(self.check(self.tok(nbf=self.NOW + 50)).state, "not-yet-valid")
        self.assertFalse(self.check(self.tok(plan="other")).ok)
        self.assertFalse(self.check(self.tok(inst="A"), install_id="B").ok)
        self.assertTrue(self.check(self.tok(inst="A"), install_id="A").ok)

    def test_revocation_is_vendor_signed(self):
        rev = entitlement.make_token(SK, {"revoked": ["L1"]}, prefix="BS1R")
        ids = entitlement.revoked_ids(rev, PK)
        self.assertEqual(self.check(self.tok(), revoked=ids).state, "revoked")
        forged = entitlement.make_token(bytes(range(1, 33)), {"revoked": ["L1"]}, prefix="BS1R")
        self.assertEqual(entitlement.revoked_ids(forged, PK), frozenset())

    def test_activation_persists_only_valid_tokens(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "BLUESTREAM_ETC_DIR": d, "BLUESTREAM_STUDIO_PUBKEY_HEX": PK.hex()}):
            self.assertFalse(entitlement.install_token("BS1.a.b").ok)
            self.assertFalse(os.path.exists(os.path.join(d, "studio-license.token")))
            good = entitlement.install_token(entitlement.make_token(
                SK, {"v": 1, "lic": "L9", "plan": "studio", "exp": time.time() + 99}))
            self.assertTrue(good.ok)
            self.assertTrue(entitlement.current().ok)

    def test_runtime_refuses_without_licence(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_ETC_DIR": d}):
            os.environ.pop("BLUESTREAM_STUDIO_PUBKEY_HEX", None)
            with mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(cli.cmd_argv("demo", "demo"), 5)
            self.assertIn("licence", err.getvalue())


class PaymentTests(unittest.TestCase):
    def test_provider_is_not_live_by_default(self):
        prov = payments.NullProvider()
        self.assertFalse(prov.live)
        with self.assertRaises(payments.PaymentError):
            prov.create_checkout("studio", "c1")
        with self.assertRaises(payments.PaymentError):
            payments.SignedWebhookVerifier("")

    def test_webhook_signature_replay_and_idempotency(self):
        v = payments.SignedWebhookVerifier("whsec_test")
        body = b'{"id":"evt_1","type":"paid","lic":"L1"}'
        now = 1_900_000_000
        hdr = v.sign(body, now)
        with tempfile.TemporaryDirectory() as d:
            store = payments.IdempotencyStore(os.path.join(d, "seen.json"))
            self.assertEqual(payments.handle_webhook(v, store, body, hdr, now)["id"], "evt_1")
            self.assertIsNone(payments.handle_webhook(v, store, body, hdr, now))  # redelivery
        for bad_hdr, bad_body, when in ((hdr, body + b" ", now), ("t=1,v1=00", body, now),
                                        ("garbage", body, now), (hdr, body, now + 10_000)):
            with self.assertRaises(payments.PaymentError):
                v.verify(bad_body, bad_hdr, when)


if __name__ == "__main__":
    unittest.main()


class LicenceHardeningTests(unittest.TestCase):
    NOW = 1_900_000_000

    def tok(self, **kw):
        doc = {"v": 1, "lic": "L1", "plan": "studio", "exp": self.NOW + 1000}
        doc.update(kw)
        return entitlement.make_token(SK, doc)

    def test_bound_licence_is_not_bypassed_by_a_missing_local_install_id(self):
        bound = self.tok(inst="abc123")
        self.assertFalse(entitlement.check(bound, PK, self.NOW, install_id="").ok)       # was a bypass
        self.assertFalse(entitlement.check(bound, PK, self.NOW, install_id="other").ok)
        self.assertTrue(entitlement.check(bound, PK, self.NOW, install_id="abc123").ok)
        self.assertTrue(entitlement.check(self.tok(), PK, self.NOW, install_id="").ok)   # unbound still fine

    def test_install_id_is_created_once_and_stable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_ETC_DIR": d}):
            self.assertEqual(entitlement.get_install_id(), "")
            first = entitlement.get_install_id(create=True)
            self.assertRegex(first, r"^[0-9a-f]{16}$")
            self.assertEqual(entitlement.get_install_id(create=True), first)

    def test_default_licence_dir_is_the_web_writable_studio_store(self):
        """The hardened web unit cannot write /etc/bluestream; activation must land in the Studio store."""
        env = {k: v for k, v in os.environ.items() if k != "BLUESTREAM_ETC_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["BLUESTREAM_STUDIO_DIR"] = "/var/lib/bluestream/studio"
            self.assertEqual(entitlement.etc_dir(), "/var/lib/bluestream/studio")
