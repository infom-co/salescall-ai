"""PC のログインの試験（DESIGN.md 5-5・11章。agent/pc_login.py）。

- PC はパスワードに触れない：ブラウザでログインしてもらい（ID とパスワード・Google・パスキー）、サーバーから PC の通行証を受け取る。
- 受け取った通行証は PC の中にしまい、PC の部品は、それでサーバーにつなぐ（PC はデータベースにつながない）。通行証は画面・表示に出さない。
⚠️ サーバーは試験の中の代わり（本物のサーバーは CL-3。払い出しは core.agents.issue）。通行証は試験の中で作って使うだけ。
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path

from salescall.agent import pc_login
from salescall.core import agents
from tests.support import SYSTEM, World, app_access

SERVER = "https://salescall.example.invalid"


class FakeServer:
    """サーバーの代わり：始めると確かめのコードを返し、決めた回数「待って」を返したあと、ログインした契約者の PC に通行証を払い出す。"""

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
        got = self.issue(uuid.UUID(body["replaces"]) if body.get("replaces") else None)
        return {"tenant_id": str(got.tenant_id), "pc": str(got.pc_id), "token": got.token}


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
        self.a = app_access()
        self.t = self.w.tenant()
        self.issue = lambda replaces: agents.issue(self.a, SYSTEM, self.t, label="試験のPC", replaces=replaces)
        self.dir = Path(tempfile.mkdtemp(prefix="salescall-pc-"))

    def run_login(self, server, previous=None):
        said, opened, clock = [], [], Clock()
        got = pc_login.login(SERVER, previous=previous, post=server, open_browser=opened.append, say=said.append,
                             sleep=clock.sleep, now=clock.now)
        return got, said, opened, clock

    def who(self, token):
        return self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, token))

    def test_the_pc_logs_in_through_the_browser_and_keeps_the_pass(self):
        server = FakeServer(self.issue)
        got, said, opened, clock = self.run_login(server)
        self.assertEqual(opened, [SERVER + "/pc?code=ABCD-1234"])                 # ブラウザでログインのページを開く
        self.assertIn("ABCD-1234", said[0])                                       # 確かめのコードを見せる
        self.assertEqual(clock.slept, [5, 5, 10])                                 # 「遅く」と言われたら間を延ばす
        self.assertEqual(got.tenant_id, self.t)
        self.assertNotIn(got.token, repr(got) + "".join(said))                   # 通行証は見せない
        path = self.dir / "pc-pass.bin"
        pc_login.save(got, path)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)          # 本人だけが読める
        kept = pc_login.load(path)
        self.assertEqual(kept, got)
        self.assertEqual(self.who(kept.token), agents.Who(self.t, kept.pc_id, "試験のPC"))   # サーバーが PC を決められる
        again, *_ = self.run_login(FakeServer(self.issue, waits=()), previous=kept)   # ログインし直す：前の PC の番号を渡す
        self.assertNotEqual(again.pc_id, kept.pc_id)
        self.assertIsNone(self.who(kept.token))                                  # 前の通行証は止まる
        self.assertTrue(pc_login.logout(path))
        self.assertIsNone(pc_login.load(path))

    def test_refusals_timeouts_and_plain_http_are_failures(self):
        with self.assertRaises(pc_login.LoginFailed):
            self.run_login(FakeServer(self.issue, waits=(), final="access_denied"))
        with self.assertRaises(pc_login.LoginFailed):                            # 期限まで待っても済まない
            self.run_login(FakeServer(self.issue, waits=["authorization_pending"] * 500))
        broken = lambda url, body: {"error": "server_error"} if url.endswith(pc_login.START) else {}   # noqa: E731
        with self.assertRaises(pc_login.LoginFailed):                            # 答えの形が違っても、PC の部品を終わらせない
            self.run_login(broken)
        half = FakeServer(self.issue, waits=())
        with self.assertRaises(pc_login.LoginFailed):
            self.run_login(lambda url, body: half(url, body) if url.endswith(pc_login.START) else {"pc": "x"})
        with self.assertRaises(pc_login.LoginFailed):                            # 通行証を運ぶので https だけ
            pc_login.login("http://salescall.example.invalid", post=FakeServer(self.issue))


class Automatic(unittest.TestCase):
    def test_the_agent_opens_the_login_page_by_itself_and_waits(self):
        """契約者は命令を打たない：PC の部品が、まだログインしていなければ自動でログインのページを開き、済むまで待つ。期限が切れたら
        新しいコードで待ち直すが、ブラウザを開き直すのは REOPEN_AFTER ごとまで。ネットに届かないあいだも待ち直す。"""
        path = Path(tempfile.mkdtemp(prefix="salescall-pc-")) / "pc-pass.bin"
        clock, opened, tries = Clock(), [], []
        got = pc_login.PcPass(uuid.uuid4(), uuid.uuid4(), "架空の通行証")

        def login_once(server, *, previous, open_browser, say):   # 1回分のログイン（10分待って期限切れ、を4回のあとに済む）
            tries.append(previous)
            open_browser(SERVER + "/pc")
            clock.t += 600
            if len(tries) == 1:
                raise OSError("試験：まだネットにつながっていない")
            if len(tries) < 5:
                raise pc_login.LoginFailed("時間切れです")
            return got

        kept = pc_login.until_logged_in(SERVER, login_once=login_once, open_browser=opened.append, say=lambda t: None,
                                        sleep=clock.sleep, now=clock.now, path=path)
        self.assertEqual((kept, pc_login.load(path), len(tries)), (got, got, 5))  # しまってから返す
        self.assertEqual(len(opened), 2)                                         # 初めてと、30分たったときだけ開く


if __name__ == "__main__":
    unittest.main()
