"""停止の画面の試験（持ち主 CL-3）：AIの不具合の停止の、緊急の道での再開（Intent.md 10-3「ボタンと権限は CL-3・CL-2」）と、
再開できないときの警告（✅ 2026-10-31 管理者「ダッシュボードになぜ再開できないのかの警告を出すようにして下さい」）。
状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.core import errors
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import World, app_access
from tests.test_ai_troubles import JEV, SONNET, Pause, trouble

SECRET = b"test-only-secret"


class EmergencyRouteOnScreen(Pause):
    def _http(self, tenant):
        person = Person("試験の契約者", False, tenant, "架空の契約者")
        return TestClient(create_app(app_access(), lambda r: person, SECRET))

    def test_the_contractor_reopens_on_the_emergency_route_from_the_stop_page(self):
        self.note(self.attempt(), trouble(SONNET, error="529 overloaded"))
        sid = self.ai_stops()[0]
        http = self._http(self.s["tenant"])
        page = http.get(f"/t/stops/{sid}").text
        self.assertIn("緊急の道で再開する", page)
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        empty = http.post(f"/t/stops/{sid}/emergency", data={"form_token": token, "reason": ""}, follow_redirects=False)
        self.assertIn("ok=0", empty.headers["location"])                       # 理由は必ず
        self.assertEqual(self.blocking() != [], True)
        r = http.post(f"/t/stops/{sid}/emergency", data={"form_token": token, "reason": "試験：今日中に約束の電話を済ませたい"},
                      follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self.blocking(), [])                                  # その停止では止めない（core の1か所）
        self.assertIn("緊急の道で再開しています", http.get(f"/t/stops/{sid}").text)
        again = http.post(f"/t/stops/{sid}/emergency", data={"form_token": token, "reason": "もう一度"}, follow_redirects=False)
        self.assertIn("ok=0", again.headers["location"])                       # 2回は出さない（core の理由の文）
        from salescall.core import stops
        from tests.support import OPERATOR, app_access
        app_access().in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.resume(tx, sid, reason="試験：止めた停止を解く"))
        page = http.get(f"/t/stops/{sid}").text                                 # 停止が解ければ、緊急の道も終わる（いま効いているかは core の active だけを見る）
        self.assertIn("緊急の道で再開していました", page)
        self.assertNotIn("緊急の道で再開しています", page)

    def test_a_stop_that_cannot_be_reopened_says_why(self):
        w = World().setup()
        self.note(self.attempt(w), trouble(JEV), s=w)
        sid = self.ai_stops(w)[0]
        page = self._http(w["tenant"]).get(f"/t/stops/{sid}").text
        self.assertIn("緊急の道では再開できません", page)
        self.assertIn(errors.EMERGENCY_CANNOT["judge_stopped"], page)
        self.assertNotIn('action="/t/stops/%d/emergency"' % sid, page)


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.EmergencyRouteOnScreen.{n}" for n in EmergencyRouteOnScreen.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
