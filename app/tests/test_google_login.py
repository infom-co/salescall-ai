"""Google のアカウントでのログイン（core/google_login・表の作り 0272）の試験。本物の Google の代わりに、試験の鍵で同じ形の ID の印（RS256）を作る。"""

from __future__ import annotations

import base64
import json
import os
import time
import unittest
import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from salescall.core import google_login, passkeys, people
from salescall.core.access import Access
from tests.support import ADMIN, TEST_PASSWORD, World
from tests.test_passkeys import SITE, Device

CLIENT = "kakuu-client.apps.googleusercontent.com"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeGoogle:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        n = self.key.public_key().public_numbers()
        self.keys = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                               "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")), "e": b64(n.e.to_bytes(3, "big"))}]}

    def token(self, sub: str, **over) -> str:
        now = int(time.time())
        body = {"iss": "https://accounts.google.com", "aud": CLIENT, "sub": sub, "email": f"{sub}@example.test", "email_verified": True,
                "iat": now, "exp": now + 3600, **over}
        head = b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        payload = b64(json.dumps(body).encode())
        sig = self.key.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{payload}.{b64(sig)}"


class GoogleLogin(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.plain = Access(os.environ["SALESCALL_TEST_APP_DSN"])
        self.g = FakeGoogle()

    def make(self):
        login = f"kakuu-{uuid.uuid4()}"
        self.w.a.in_system(ADMIN, lambda tx: people.create(tx, tenant_id=self.s["tenant"], kind="operator", name="架空の契約者",
                                                            login_id=login, password=TEST_PASSWORD))
        return people.login(self.plain, login, TEST_PASSWORD).person

    def go(self, token):
        return google_login.login(self.plain, token, client_id=CLIENT, keys=self.g.keys)

    def test_a_linked_google_account_logs_in_and_only_one_person_holds_it(self):
        """結び付けた Google のアカウントでログインできる（結び付いていなければ None）。1つの Google のアカウントは1人の方にだけ。外せば使えない。"""
        me, other = self.make(), self.make()
        sub = f"g{uuid.uuid4().hex}"
        self.assertIsNone(self.go(self.g.token(sub)))                                   # まだ結び付いていない
        self.assertEqual(google_login.link(self.plain, me, self.g.token(sub), client_id=CLIENT, keys=self.g.keys), f"{sub}@example.test")
        got = self.go(self.g.token(sub))
        self.assertEqual((got.person.person_id, got.needs_passkey), (me.person_id, False))
        self.assertEqual(people.by_session(self.plain, got.token).person_id, me.person_id)
        with self.assertRaises(google_login.GoogleRefused):                            # ほかの方には結び付けられない
            google_login.link(self.plain, other, self.g.token(sub), client_id=CLIENT, keys=self.g.keys)
        google_login.unlink(self.plain, me)
        self.assertIsNone(self.go(self.g.token(sub)))
        google_login.link(self.plain, other, self.g.token(sub), client_id=CLIENT, keys=self.g.keys)   # 外したあとは、ほかの方に結び付けられる
        self.assertEqual(self.go(self.g.token(sub)).person.person_id, other.person_id)

    def test_a_bad_google_token_is_refused(self):
        """署名の違う印・ほかの宛先・期限切れ・メール未確認・ほかの発行元は通らない。"""
        me = self.make()
        sub = f"g{uuid.uuid4().hex}"
        google_login.link(self.plain, me, self.g.token(sub), client_id=CLIENT, keys=self.g.keys)
        stranger = FakeGoogle()
        for name, token in (("ほかの鍵の署名", stranger.token(sub)), ("ほかの宛先", self.g.token(sub, aud="ほかの.apps.googleusercontent.com")),
                            ("期限切れ", self.g.token(sub, exp=int(time.time()) - 3600)), ("メール未確認", self.g.token(sub, email_verified=False)),
                            ("ほかの発行元", self.g.token(sub, iss="https://evil.example")), ("形が違う", "abc")):
            with self.subTest(name), self.assertRaises(google_login.GoogleRefused):
                self.go(token)

    def test_google_login_still_needs_the_passkey(self):
        """Google でログインしても、パスキーを登録した方は2段目が要る（ログインの印を作る1か所＝people.start_session）。"""
        me = self.make()
        sub = f"g{uuid.uuid4().hex}"
        google_login.link(self.plain, me, self.g.token(sub), client_id=CLIENT, keys=self.g.keys)
        phone = Device()
        passkeys.register(self.plain, me, SITE, phone.create(passkeys.register_options(self.plain, me, SITE)), "仕事の PC")
        got = self.go(self.g.token(sub))
        self.assertTrue(got.needs_passkey)
        self.assertIsNone(people.by_session(self.plain, got.token))
        self.assertTrue(passkeys.verify_login(self.plain, got.token, SITE, phone.get(passkeys.login_options(self.plain, got.token, SITE))))
        self.assertIsNotNone(people.by_session(self.plain, got.token))

    def test_the_keys_are_fetched_once_until_their_age(self):
        """Google の公開の鍵は、示された期限まで使い回す（取りに行くのは期限が過ぎてから）。"""
        calls = []

        class Answer:
            headers = {"Cache-Control": "public, max-age=100"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"keys": []}).encode()

        def opener(url, timeout):
            calls.append(url)
            return Answer()
        google_login._cache.update(keys=None, until=0.0)
        google_login.fetch_keys(opener=opener, now=1000.0)
        google_login.fetch_keys(opener=opener, now=1099.0)
        self.assertEqual(len(calls), 1)
        google_login.fetch_keys(opener=opener, now=1101.0)
        self.assertEqual(calls, [google_login.CERTS_URL] * 2)
