"""承認待ちの、行ごとの種類の試験（持ち主 CL-3。web/inbox.py）。状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。

前の担当の方との約束（old_promise）：選びを待つ行が受け箱に出る・「守る」を出すかは core の1か所（old_promise_keepable）に従う・
画面のボタンで選ぶと受け箱から消え、行の予定が core の決めたとおりになる。
1か月以上先のかけ直し（month_ahead）：伺っている行が受け箱に出る（お電話する日時つき）・画面のボタンで答えると消え、もう答えられない。
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.core import attempts, call_report, calling_hours, health, tasks
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import CLEAN, app_access, callback
from tests.test_gate import FakeDriver, dial_exit
from tests.test_settle import DialFixAfterCalls, rep
from tests.test_unwelcome import MonthAhead

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


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.{c.__name__}.{n}" for c in (OldPromise, MonthAheadAnswer) for n in c.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
