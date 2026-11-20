"""ログインの2段階の確かめ＝パスキー（core/passkeys・core/people。表の作り 0271）の試験。

✅ 2026-11-20 ユーザー「ア パスキーだけ」。本物の端末の代わりに、試験の中で端末と同じ形（WebAuthn の登録＝none の証明・確かめ＝ECDSA の署名）を作る。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import unittest
import uuid

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from salescall.core import passkeys, people
from salescall.core.access import Access
from tests.support import ADMIN, TEST_PASSWORD, World

SITE = passkeys.Site(rp_id="app.example.test", origin="https://app.example.test")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """試験の端末（パスキー1つ）：秘密の鍵は端末の中だけ（試験のこの入れ物）。"""

    def __init__(self, site: passkeys.Site = SITE):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred = os.urandom(32)
        self.count = 0
        self.site = site

    def _auth_data(self, flags: int, extra: bytes = b"") -> bytes:
        return hashlib.sha256(self.site.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big") + extra

    def create(self, options_json: str, *, origin: str | None = None) -> str:
        """登録：端末が鍵を作り、公開の鍵を渡す（none の証明）。"""
        challenge = json.loads(options_json)["challenge"]
        client = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": origin or self.site.origin, "crossOrigin": False}).encode()
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        attested = bytes(16) + len(self.cred).to_bytes(2, "big") + self.cred + cose
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(0x45, attested)})
        return json.dumps({"id": b64(self.cred), "rawId": b64(self.cred), "type": "public-key", "clientExtensionResults": {},
                           "response": {"clientDataJSON": b64(client), "attestationObject": b64(att), "transports": ["internal"]}})

    def get(self, options_json: str, *, origin: str | None = None, count_step: int = 1) -> str:
        """確かめ：端末が問いに署名する（本人の確かめ済み＝UV）。"""
        challenge = json.loads(options_json)["challenge"]
        self.count += count_step
        client = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin or self.site.origin, "crossOrigin": False}).encode()
        auth = self._auth_data(0x05)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return json.dumps({"id": b64(self.cred), "rawId": b64(self.cred), "type": "public-key", "clientExtensionResults": {},
                           "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth), "signature": b64(sig)}})


class Passkeys(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.plain = Access(os.environ["SALESCALL_TEST_APP_DSN"])   # 本番と同じ入口（試験のログインを付けない）
        self.login = f"kakuu-{uuid.uuid4()}"
        self.w.a.in_system(ADMIN, lambda tx: people.create(tx, tenant_id=self.s["tenant"], kind="operator", name="架空の契約者",
                                                            login_id=self.login, password=TEST_PASSWORD))

    def person(self):
        got = people.login(self.plain, self.login, TEST_PASSWORD)
        self.assertIsNotNone(got)
        return got

    def register(self, person, device, label="仕事の PC"):
        return passkeys.register(self.plain, person, SITE, device.create(passkeys.register_options(self.plain, person, SITE)), label)

    def test_a_person_with_a_passkey_needs_it_after_the_password(self):
        """パスキーを登録していない方は、パスワードだけで使える（2段目は任意）。登録した方は、パスワードのあと、パスキーで確かめるまで印を使えない。
        最初のパスキーのときだけ予備の合言葉（10個・このときだけ見せる）。"""
        first = self.person()
        self.assertFalse(first.needs_passkey)
        self.assertIsNotNone(people.by_session(self.plain, first.token))
        phone = Device()
        _, codes = self.register(first.person, phone)
        self.assertEqual(len(codes), passkeys.RECOVERY_CODES)
        self.assertIsNone(self.register(first.person, Device(), "予備のスマホ")[1])              # 2つ目からは作らない
        s = self.person()
        self.assertTrue(s.needs_passkey)
        self.assertIsNone(people.by_session(self.plain, s.token))                               # 2段目の前は使えない
        self.assertTrue(passkeys.verify_login(self.plain, s.token, SITE, phone.get(passkeys.login_options(self.plain, s.token, SITE))))
        self.assertEqual(people.by_session(self.plain, s.token).person_id, s.person.person_id)
        with self.assertRaises(passkeys.PasskeyRefused):                                        # 通った印は、もう2段目を待たない
            passkeys.login_options(self.plain, s.token, SITE)

    def test_a_fake_site_a_copied_key_or_a_reused_question_does_not_pass(self):
        """偽のサイト（住所の違う origin）・使い回した問い・前より小さい署名の回数（写しの疑い）・登録していない端末は通らない。記録に残る。"""
        phone = Device()
        self.register(self.person().person, phone)
        s = self.person()
        opts = passkeys.login_options(self.plain, s.token, SITE)
        self.assertFalse(passkeys.verify_login(self.plain, s.token, SITE, phone.get(opts, origin="https://app.example.test.evil.example")))
        self.assertFalse(passkeys.verify_login(self.plain, s.token, SITE, phone.get(opts)))     # 問いは1回だけ（さっき使った）
        self.assertFalse(passkeys.verify_login(self.plain, s.token, SITE, Device().get(passkeys.login_options(self.plain, s.token, SITE))))
        self.assertIsNone(people.by_session(self.plain, s.token))
        self.assertTrue(passkeys.verify_login(self.plain, s.token, SITE, phone.get(passkeys.login_options(self.plain, s.token, SITE))))   # 本物は通る（回数1）
        copy = self.person()                                                                     # 写しの端末：回数が前より小さい（0）
        phone.count = 0
        self.assertFalse(passkeys.verify_login(self.plain, copy.token, SITE, phone.get(passkeys.login_options(self.plain, copy.token, SITE), count_step=0)))
        self.assertIsNone(people.by_session(self.plain, copy.token))
        whys = self.w.a.in_system(people.LOGIN_CHECKER, lambda tx: [r["why"] for r in tx.all(
            "SELECT why FROM salescall.person_login_events WHERE login_id = %s ORDER BY seq", (self.login,))])
        self.assertEqual((whys.count("passkey_failed"), whys.count("passkey_ok")), (4, 1))

    def test_a_recovery_code_works_once_and_repeated_misses_are_locked(self):
        """予備の合言葉：一度だけ使える（区切り・大文字小文字は見ない）。作り直すと前の組は使えない。2段目を続けて誤ると、決めた長さのあいだ試させない
        （パスワードが通っても数え直さない＝people.locked の段 second）。"""
        p = self.person().person
        _, codes = self.register(p, Device())
        s = self.person()
        self.assertTrue(passkeys.use_recovery_code(self.plain, s.token, codes[0].lower().replace("-", " ")))
        self.assertIsNotNone(people.by_session(self.plain, s.token))
        again = self.person()
        self.assertFalse(passkeys.use_recovery_code(self.plain, again.token, codes[0]))          # 一度だけ
        fresh = passkeys.new_recovery_codes(self.plain, p)
        self.assertFalse(passkeys.use_recovery_code(self.plain, again.token, codes[1]))          # 前の組は使えない
        most = self.w.a.in_system(people.LOGIN_CHECKER, lambda tx: int(people._setting(tx, "login.max_failures")))
        for _ in range(most):
            passkeys.use_recovery_code(self.plain, self.person().token, "AAAAA-AAAAA")
        self.assertFalse(passkeys.use_recovery_code(self.plain, self.person().token, fresh[0]))  # 正しくても、断っているあいだは試さない
        self.assertIsNotNone(self.person())                                                      # パスワードの段は別に数える

    def test_removing_the_last_passkey_drops_the_second_step(self):
        """最後のパスキーを外すと、2段目は要らなくなる（任意のため）。外せるのは自分のパスキーだけ。"""
        first = self.person()
        pid, _ = self.register(first.person, Device())
        with self.assertRaises(passkeys.PasskeyRefused):
            passkeys.remove(self.plain, first.person.__class__(uuid.uuid4(), first.person.tenant_id, "operator", "ほかの方", "x"), pid)
        passkeys.remove(self.plain, first.person, pid)
        self.assertFalse(self.person().needs_passkey)
