"""見張りの画面の試験（持ち主 CL-3。計画書 12-1。web/watch.py）。状態は CL-2 の試験の土台で、本物の流れ（回線ごとの処理・門・出口・締め）
から作る（架空のデータだけ）。画面は答える場所の事実を並べるだけ：こちらの不具合で2回続けて終えた行が「休ませている行」に、終えた電話が
「こちらの不具合で終えた電話」に出る。ほかの契約者には見えない。ホームの「気をつけること」の数は、見張りの1か所が数える。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from salescall.output import words
from salescall.web import watch
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import World, app_access
from tests.test_troubles import OurTroubles

SECRET = b"test-only-secret"


def _http(tenant):
    return TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, tenant, "架空の契約者"), SECRET))


class WatchOnScreen(OurTroubles):
    def test_a_row_rested_by_our_trouble_and_its_calls_show_on_the_watch(self):
        s = self.ready()
        self.trouble(s, task=s["task"])
        self.trouble(s, task=s["task"])
        self.assertTrue(self.resting(s, s["task"]))
        http = _http(s["tenant"])
        page = http.get("/t/watch").text
        def block(key):   # 中身のある項目は表、空の項目は下のまとめに出る（どちらも id は項目の鍵）
            at = page.index(f'id="{key}"')
            nxt = page.find('id="', at + 5)
            return page[at:nxt if nxt > 0 else len(page)]
        rest = block("resting_rows")
        self.assertIn("休ませています", rest)                                     # 行の待ちの言葉（core の gate.REASONS）
        self.assertIn(f'/rows/{s["task"]}', rest)                                # その行へ飛べる
        calls = block("fault_calls")
        self.assertEqual(calls.count(words.call_end("ai_malfunction")), 2)       # 終えた2本（言葉は output/words の1か所）
        home = http.get("/t").text
        self.assertIn("気をつけること", home)
        self.assertIn("こちらの不具合で休ませているお客様", home)
        got = self.claim(s)                                                      # 門が、休んでいる行を止める（理由 task_resting）
        self.assertEqual(getattr(got, "reason", None), "task_resting")
        biz = http.get(f'/c/{s["client"]}/b/{s["business"]}').text
        part = biz[biz.index("いま電話を見送っているお客様"):]
        self.assertIn(words.gate_reason("task_resting"), part)                   # 理由の言葉は core の gate.REASONS
        # 数えるのは core の gate.denied_now の1か所（業務で絞る）。ほかの業務のタブには、この業務の見送りを数えない（CL-2 の 4-468 の 63 の (1)）
        from tests.support import World as _W
        other_biz, _ = _W().business_with_version(s["tenant"], s["client"])
        elsewhere = http.get(f'/c/{s["client"]}/b/{other_biz}').text
        self.assertNotIn(words.gate_reason("task_resting"), elsewhere)
        overview = http.get(f'/c/{s["client"]}').text                          # 依頼者の概要（全業務）には出る
        self.assertIn(words.gate_reason("task_resting"), overview[overview.index("いま電話を見送っているお客様"):])
        other = World().setup()
        self.assertNotIn(f'/rows/{s["task"]}', _http(other["tenant"]).get("/t/watch").text)   # ほかの契約者には見えない

    def test_a_rested_row_can_be_put_back_by_hand_from_the_row_page(self):   # 計画書 10-3「管理者・契約者が手で戻すこともできる」
        import re
        s = self.ready()
        self.trouble(s, task=s["task"])
        self.trouble(s, task=s["task"])
        self.assertTrue(self.resting(s, s["task"]))
        http = _http(s["tenant"])
        row = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}'
        page = http.get(row).text
        self.assertIn("手で戻す", page)
        token = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
        none = http.post(f"{row}/restore", data={"form_token": token, "reason": ""}, follow_redirects=False)
        self.assertIn("ok=0", none.headers["location"])                         # 理由が無ければ戻さない
        self.assertTrue(self.resting(s, s["task"]))
        r = http.post(f"{row}/restore", data={"form_token": token, "reason": "試験：直ったことを確かめた"}, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith(row + "?"))
        self.assertIn("ok=1", r.headers["location"])
        self.assertFalse(self.resting(s, s["task"]))                            # core の troubles.restore が戻した
        self.assertNotIn("手で戻す", http.get(row).text)
        again = http.post(f"{row}/restore", data={"form_token": token, "reason": "試験：もう1回"}, follow_redirects=False)
        self.assertIn("ok=0", again.headers["location"])                        # もう休んでいない＝断る・何も変えない

    def test_a_row_waiting_for_name_recordings_shows_with_the_missing_parts(self):   # 計画書 12-1（決めるのは門と同じ core の1か所）
        import unittest.mock
        from salescall.core import gate
        s = self.ready()
        with unittest.mock.patch.object(gate, "name_recording_missing",
                                        lambda tx, task: ["お名前「架空」"] if task == s["task"] else None):
            page = _http(s["tenant"]).get("/t/watch").text
        part = page[page.index('id="name_recordings"'):]
        self.assertIn("足りない録音：お名前「架空」", part[:3000])
        self.assertIn(f'/rows/{s["task"]}', part[:3000])

    def test_every_watch_says_where_it_is_read_and_what_it_means(self):
        for key, w in watch.WATCHES.items():
            with self.subTest(key=key):
                self.assertIn(w.where, ("tenant", "client", "system"))
                self.assertTrue(w.title and w.meaning)


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.WatchOnScreen.{n}" for n in WatchOnScreen.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
