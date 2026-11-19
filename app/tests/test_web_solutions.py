"""依頼者の解決策の確かめの、承認待ちのボタンの試験（持ち主 CL-3。web/inbox.py の solution）。状態は CL-2 の試験の土台で、本物の流れから作る
（架空のデータだけ）。✅ 2026-11-06 管理者「契約者にそれをダッシュボードで確認します」・2026-11-07「契約者が承認をしないと自動で入らず、
新規の電話もかけられません、ダッシュボードに承認ボタンを分かりやすく表示」。「入れる」に名前を洗い出す処理は要らない（CL-2 4-428）。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from fastapi.testclient import TestClient

from salescall.core import business_settings as bs
from salescall.core import call_report, client_solutions
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, app_access, make_report
from tests.test_client_solutions import NO_NAMES, Solutions
from tests.test_web_inbox_rows import SECRET, form_of


class SolutionButtons(Solutions):
    def _left_out(self):
        """手で前の版に戻した流れに、あとで別の解決策が来た実施回（CL-2 の試験と同じ作り方）：戻す前の解決策が、その流れに入っていない。"""
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金"), find_names=NO_NAMES), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版にする"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：手で前の版に戻す"), OPERATOR)
        back = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (s["run"],))["v"])
        later = self.w.insert_claim(s, version=back)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, later, st)
        point = call_report.NotAgreed(5, "Q1", "解約のときは？", 1, False, None, False, "解約")
        self.tx(s, lambda tx: call_report.save(tx, later, replace(make_report(), not_agreed=(point,))))
        self.tx(s, lambda tx: client_solutions.add(tx, later, 0, "いつでも解約できます。", origin="dashboard",
                                                   name_answer=self.namer("解約"), find_names=NO_NAMES), OPERATOR)
        return s, first

    def test_a_solution_outside_the_line_is_offered_in_the_inbox_and_added_by_the_button(self):
        s, first = self._left_out()
        offered = self.tx(s, lambda tx: client_solutions.not_in_line(tx, s["run"]))
        self.assertEqual([r["solution_id"] for r in offered], [first.solution_id])
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, s["tenant"], "架空の契約者"), SECRET))
        page = http.get("/t/inbox").text
        self.assertIn("依頼者の解決策の確かめ", page)
        self.assertIn("月額1万円です。", page)
        add = f'/c/{s["client"]}/inbox/solution/add'
        sent = form_of(page, add)
        if offered[0]["unconfirmed"]:   # 手で外して、まだ確かめていない＝電話が止まっている＝「入れない」も出す
            self.assertIn(f'/c/{s["client"]}/inbox/solution/leave_out', page)
        r = http.post(add, data=sent, follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.not_in_line(tx, s["run"])), [])   # core の add_earlier が入れた
        self.assertNotIn("月額1万円です。", http.get("/t/inbox").text)
        again = http.post(add, data=sent, follow_redirects=False)
        self.assertIn("ok=0", again.headers["location"])                                          # もう入れた（古い画面）＝断る


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.SolutionButtons.{n}" for n in SolutionButtons.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
