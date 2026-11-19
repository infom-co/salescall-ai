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
        self.assertIn("こちらの不具合で休ませている行", home)
        got = self.claim(s)                                                      # 門が、休んでいる行を止める（理由 task_resting）
        self.assertEqual(getattr(got, "reason", None), "task_resting")
        biz = http.get(f'/c/{s["client"]}/b/{s["business"]}').text
        part = biz[biz.index("今日、システムが電話を見送り始めたお客様"):]
        self.assertIn(words.gate_reason("task_resting"), part)                   # 理由の言葉は core の gate.REASONS
        other = World().setup()
        self.assertNotIn(f'/rows/{s["task"]}', _http(other["tenant"]).get("/t/watch").text)   # ほかの契約者には見えない

    def test_every_watch_says_where_it_is_read_and_what_it_means(self):
        for key, w in watch.WATCHES.items():
            with self.subTest(key=key):
                self.assertIn(w.where, ("tenant", "client"))
                self.assertTrue(w.title and w.meaning)


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.WatchOnScreen.{n}" for n in WatchOnScreen.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
