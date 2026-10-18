"""PC のログインの試験（DESIGN.md 5-4・11章。supervisor/pc_login.py・config）。

- PC はパスワードに触れない：ブラウザでログインしてもらい（ID とパスワード・Google・パスキー）、サーバーから PC の鍵を受け取る。
- 受け取った鍵は PC の中にしまい、見守り役は SALESCALL_DSN が無ければ、それでつなぐ。鍵は画面・表示に出さない。
⚠️ サーバーは試験の中の代わり（本物のサーバーは CL-3）。鍵は試験の中で作って使うだけ。
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path

from salescall.core import logins
from salescall.core.access import Access, Provisioner
from salescall.supervisor import config, pc_login
from tests.support import OPERATOR, SYSTEM, World

SERVER = "https://salescall.example.invalid"


class FakeServer:
    """サーバーの代わり：始めると確かめのコードを返し、決めた回数「待って」を返したあと、ログインした契約者の鍵を払い出す。"""

    def __init__(self, issue, *, waits=("authorization_pending", "slow_down"), final=None):
        self.issue, self.waits, self.final, self.calls = issue, list(waits), final, []

    def __call__(self, url, body):
        self.calls.append((url, body))
        if url.endswith(pc_login.START):
            return {"device_code": "dc-1", "user_code": "ABCD-1234", "verification_uri": SERVER + "/pc",
                    "verification_uri_complete": SERVER + "/pc?code=ABCD-1234", "interval": 5, "expires_in": 600}
        if self.waits:
            return {"error": self.waits.pop(0)}
        if self.final:
            return {"error": self.final}
        login = self.issue(body.get("replaces"))
        return {"tenant_id": str(login.tenant_id), "pc": login.pc, "conninfo": login.conninfo}


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    def now(self):
        return self.t


class Login(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        prov = Provisioner(os.environ["SALESCALL_TEST_PROVISIONER_DSN"])
        self.issue = lambda replaces: logins.issue(self.w.a, SYSTEM, prov, self.s["tenant"], pc_label="試験のPC",
                                                   base_conninfo=os.environ["SALESCALL_TEST_APP_DSN"], replaces=replaces)
        self.dir = Path(tempfile.mkdtemp(prefix="salescall-pc-"))

    def run_login(self, server, previous=None):
        said, opened, clock = [], [], Clock()
        key = pc_login.login(SERVER, previous=previous, post=server, open_browser=opened.append, say=said.append,
                             sleep=clock.sleep, now=clock.now)
        return key, said, opened, clock

    def test_the_pc_logs_in_through_the_browser_and_keeps_the_key(self):
        server = FakeServer(self.issue)
        key, said, opened, clock = self.run_login(server)
        self.assertEqual(opened, [SERVER + "/pc?code=ABCD-1234"])                 # ブラウザでログインのページを開く
        self.assertIn("ABCD-1234", said[0])                                       # 確かめのコードを見せる
        self.assertEqual(clock.slept, [5, 5, 10])                                 # 「遅く」と言われたら間を延ばす
        self.assertEqual(key.tenant_id, self.s["tenant"])
        self.assertNotIn(key.conninfo, repr(key) + "".join(said))                # 鍵は見せない
        path = self.dir / "pc-key.bin"
        pc_login.save(key, path)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)          # 本人だけが読める
        got = pc_login.load(path)
        cfg = config.load({}, pc_key=got)                                         # SALESCALL_DSN が無ければ、しまった鍵でつなぐ
        self.assertEqual((cfg.tenant_id, cfg.dsn), (self.s["tenant"], key.conninfo))
        rows = Access(cfg.dsn).in_client(OPERATOR, cfg.tenant_id, self.s["client"], lambda tx: tx.all(
            "SELECT task_id FROM salescall.tasks"))
        self.assertEqual([r["task_id"] for r in rows], [self.s["task"]])
        again, *_ = self.run_login(FakeServer(self.issue, waits=()), previous=got)   # ログインし直す：前の PC の番号を渡す
        self.assertNotEqual(again.pc, got.pc)
        self.assertTrue(pc_login.logout(path))
        self.assertIsNone(pc_login.load(path))

    def test_refusals_timeouts_and_plain_http_are_failures(self):
        with self.assertRaises(pc_login.LoginFailed):
            self.run_login(FakeServer(self.issue, waits=(), final="access_denied"))
        with self.assertRaises(pc_login.LoginFailed):                            # 期限まで待っても済まない
            self.run_login(FakeServer(self.issue, waits=["authorization_pending"] * 500))
        with self.assertRaises(pc_login.LoginFailed):                            # 鍵を運ぶので https だけ
            pc_login.login("http://salescall.example.invalid", post=FakeServer(self.issue))


class Config(unittest.TestCase):
    def test_the_admin_pc_uses_its_settings_and_a_contractor_pc_needs_a_login(self):
        tenant = uuid.uuid4()
        cfg = config.load({"SALESCALL_DSN": "host=x", "SALESCALL_TENANT_ID": str(tenant)}, pc_key=None)
        self.assertEqual((cfg.dsn, cfg.tenant_id), ("host=x", tenant))           # 管理者の PC は、前と同じ
        with self.assertRaises(config.ConfigMissing):                            # 契約者の PC で、まだログインしていない
            config.load({}, pc_key=None)
        key = pc_login.PcKey(tenant, "0a1b2c3d", "host=y")
        with self.assertRaises(config.ConfigMissing):                            # ログインした契約者と違う契約者の番号
            config.load({"SALESCALL_TENANT_ID": str(uuid.uuid4())}, pc_key=key)


if __name__ == "__main__":
    unittest.main()
