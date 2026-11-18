"""電話のやり取りの画面の試験（持ち主 CL-3）。承認する前に、その場のやり取りを確かめられること（✅ 2026-10-30 管理者「人が録音や
その場のやり取りを確認できる状態にする必要があります」）。状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from salescall.core import attempts, conversation_io
from salescall.core.conversation_io import Utterance
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import SYSTEM, World, app_access
from tests.test_gate import FakeDriver, dial_exit
from tests.test_irreversible import TALK, Helpers

SECRET = b"test-only-secret"


class CallWords(Helpers):
    def test_the_whole_exchange_is_shown_and_only_inside_its_own_row(self):
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in TALK[1:3]:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: conversation_io.write_utterance(tx, c.attempt_id, Utterance("ai", 0, "お世話になっております。架空の会社です。", 0.5)))
        self.tx(s, lambda tx: conversation_io.write_utterance(tx, c.attempt_id, Utterance("callee", 1, "営業の電話はお断りです", 3.2)))
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, s["tenant"], "架空の契約者"), SECRET))
        row = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}'
        self.assertIn(f"{row}/calls/{c.attempt_id}", http.get(row).text)           # 行の画面の電話の記録から開ける
        page = http.get(f"{row}/calls/{c.attempt_id}").text
        for text in ("お世話になっております。架空の会社です。", "営業の電話はお断りです", "お客様", "こちら（AI）"):
            self.assertIn(text, page)
        self.assertLess(page.index("架空の会社です"), page.index("お断りです"))      # 起きた順
        other = World().setup()
        outsider = TestClient(create_app(app_access(), lambda r: Person("別の契約者", False, other["tenant"], "別"), SECRET))
        self.assertEqual(outsider.get(f"{row}/calls/{c.attempt_id}").status_code, 404)   # 別の契約者は開けない
        wrong = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{other["task"]}/calls/{c.attempt_id}'
        self.assertEqual(http.get(wrong).status_code, 404)                         # 別の行の住所では開けない


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.CallWords.{n}" for n in CallWords.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
