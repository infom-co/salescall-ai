"""承認待ちの、行ごとの種類の試験（持ち主 CL-3。web/inbox.py）。状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。

前の担当の方との約束（old_promise）：選びを待つ行が受け箱に出る・「守る」を出すかは core の1か所（old_promise_keepable）に従う・
画面のボタンで選ぶと受け箱から消え、行の予定が core の決めたとおりになる。
1か月以上先のかけ直し（month_ahead）：伺っている行が受け箱に出る（お電話する日時つき）・画面のボタンで答えると消え、もう答えられない。
直しでかける電話（correction）：確かめ待ちの行が受け箱と1行の画面に出る・1行の画面から確かめると、その画面へ戻り、確かめ待ちが無くなる（CL-2 4-422）。
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.core import attempts, call_report, calling_hours, health, tasks
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import CLEAN, OPERATOR, app_access, callback
from tests.test_gate import FakeDriver, dial_exit
from tests.test_settle import Corrections, DialFixAfterCalls, rep
from tests.test_unwelcome import MonthAhead
from tests.test_approval import Helpers as ApprovalHelpers, TAKEN

SECRET = b"test-only-secret"


class OldPromise(DialFixAfterCalls):
    def _waiting_row(self):
        """名前を「違う」で直した通話で、前の担当の方が新しい約束をした行（CL-2 の試験と同じ作り方）。"""
        from tests.support import SYSTEM
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in ("answered", "in_conversation"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep(callback=callback("later", time="あとで"))))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        self.settle(s, c.attempt_id)
        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), c.attempt_id)
        return s

    def _http(self, s):
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        return TestClient(create_app(app_access(), lambda r: person, SECRET))

    def test_a_row_waiting_for_the_choice_is_in_the_inbox_and_the_choice_takes_it_out(self):
        s = self._waiting_row()
        http = self._http(s)
        page = http.get("/t/inbox").text
        self.assertIn("前の担当の方との約束", page)
        self.assertIn(f'/c/{s["client"]}/inbox/old_promise/keep', page)        # 守れる約束なので「守る」も出る（core の1か所の答え）
        self.assertIn(f'/c/{s["client"]}/inbox/old_promise/new_person', page)
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        r = http.post(f'/c/{s["client"]}/inbox/old_promise/new_person', data={"form_token": token, "task_id": str(s["task"])},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))         # 予定は core の決めたとおり
        self.assertNotIn("前の担当の方との約束", http.get("/t/inbox").text)
        again = http.post(f'/c/{s["client"]}/inbox/old_promise/keep', data={"form_token": token, "task_id": str(s["task"])},
                          follow_redirects=False)                              # もう選んだ行（古い画面から押した）
        self.assertIn("ok=0", again.headers["location"])                       # 断りの文が出る（500 にならない・何も書かない）




def _http(tenant):
    person = Person("試験の契約者", False, tenant, "架空の契約者")
    return TestClient(create_app(app_access(), lambda r: person, SECRET))


class MonthAheadAnswer(MonthAhead):
    def test_a_row_asked_about_is_in_the_inbox_and_the_answer_takes_it_out(self):
        s = self.ready()
        self.choose(s, "with_approval")
        words, first, last, repeat = self.monthly_rounds()[0]
        w, d, p = self.passed(s, first(s), last(s), repeat)
        self.assertEqual(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])), p["call_at"])
        http = _http(s["tenant"])
        page = http.get("/t/inbox").text
        self.assertIn("1か月以上先のかけ直し", page)
        self.assertIn(calling_hours.label(p["call_at"]), page)                 # お電話する日時（書き方は core の1か所）
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        r = http.post(f'/c/{s["client"]}/inbox/month_ahead/approve', data={"form_token": token, "task_id": str(s["task"])},
                      follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])))
        self.assertNotIn("1か月以上先のかけ直し", http.get("/t/inbox").text)
        again = http.post(f'/c/{s["client"]}/inbox/month_ahead/refuse', data={"form_token": token, "task_id": str(s["task"])},
                          follow_redirects=False)
        self.assertIn("ok=0", again.headers["location"])                       # もう答えた（古い画面）＝断りの文・何も書かない


class CorrectionCall(Corrections):
    def test_a_correction_call_waits_in_the_inbox_and_on_the_row_until_confirmed(self):
        from salescall.core import next_action
        from tests.test_judge import goal_refused
        s = self.ready()
        got = self.settle(s, self.call(s, report=goal_refused(rep(), goals=("Q1",)), items=(("Q1", "accepted", "いいえ"),)))
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.correct(s, got["version_no"], "open")                              # 「断られた」を「未決」に直した＝かける（CL-2 の試験と同じ）
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：予定をいまに"))
        http = _http(s["tenant"])
        self.assertIn("直しでかける電話の確かめ", http.get("/t/inbox").text)
        row = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}'
        page = http.get(row).text
        self.assertIn("直した内容でお電話してよいかの確かめを待っています", page)
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        r = http.post(f'/c/{s["client"]}/inbox/correction/confirm', data={"form_token": token, "task_id": str(s["task"]), "back": row},
                      follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith(row + "?"))            # 1行の画面へ戻る
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT salescall.task_correction_unconfirmed(%s) AS v", (s["task"],))["v"]))
        self.assertNotIn("直しでかける電話の確かめ", http.get("/t/inbox").text)
        outside = http.post(f'/c/{s["client"]}/inbox/correction/confirm', data={"form_token": token, "task_id": str(s["task"]),
                                                                              "back": "https://example.invalid/"}, follow_redirects=False)
        self.assertTrue(outside.headers["location"].startswith("/t/inbox"))      # 依頼者の中の住所でなければ戻さない（ok=0：もう確かめた）
        self.assertIn("ok=0", outside.headers["location"])


class VerificationMode(ApprovalHelpers):
    """検証モードの2つの承認（発信前の行の承認・結果の確かめ）と、結果を直す画面（CL-2 の承認の試験と同じ作り方）。"""

    def _post(self, http, path, data):
        token = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t").text).group(1)
        return http.post(path, data={"form_token": token, **data}, follow_redirects=False)

    def test_rows_wait_for_approval_in_one_item_per_run_and_are_approved_together(self):
        from salescall.core import approval
        s = self.fresh(stage_cap=100)
        self.put_client(s, "approval.human", True)
        http = _http(s["tenant"])
        page = http.get("/t/inbox").text
        self.assertIn("発信前の行の承認（検証モード）", page)
        self.assertIn(f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}"', page)     # 決める前に見る（お客様リスト）
        r = self._post(http, f'/c/{s["client"]}/inbox/task_approval/approve_all', {"run_id": str(s["run"])})
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: approval.uncleared(tx, s["task"])))
        self.assertNotIn("発信前の行の承認（検証モード）", http.get("/t/inbox").text)
        again = self._post(http, f'/c/{s["client"]}/inbox/task_approval/approve_all', {"run_id": str(s["run"])})
        self.assertIn("ok=0", again.headers["location"])                         # もう待っている行は無い（何も書かない）

    def test_a_result_waits_for_the_check_and_can_be_approved_or_corrected(self):
        from salescall.core import approval, quality_stage
        from salescall.core.attempts import VerificationSummary
        for how in ("approve", "correct"):
            with self.subTest(how=how):
                s = self.fresh(stage_cap=100)
                self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, None, evidence="試験"), OPERATOR)
                self.put_client(s, "approval.human", True)
                self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
                self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
                http = _http(s["tenant"])
                self.assertIn("結果の確かめ（検証モード）", http.get("/t/inbox").text)
                if how == "approve":
                    r = self._post(http, f'/c/{s["client"]}/inbox/result_check/approve', {"task_id": str(s["task"]), "seq": "1"})
                else:
                    row = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}'
                    self.assertIn("結果を直す", http.get(row).text)
                    self.assertIn("ok=0", self._post(http, row + "/result", {"version": "1", "state": "incomplete", "reason": ""}).headers["location"])
                    r = self._post(http, row + "/result", {"version": "1", "state": "incomplete", "reason": "試験：録音を聞き直した"})
                self.assertIn("ok=1", r.headers["location"])
                self.assertEqual(len(self.releases(s)), 1)                          # 確定した（1つだけ）
                self.assertNotIn("結果の確かめ（検証モード）", http.get("/t/inbox").text)


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.{c.__name__}.{n}" for c in (OldPromise, MonthAheadAnswer, CorrectionCall, VerificationMode) for n in c.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
