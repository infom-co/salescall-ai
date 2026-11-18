"""承認待ちの受け箱の試験（持ち主 CL-3。web/inbox.py）。本物の門・通話の締めを通して承認待ちを作る（CL-2 の試験の土台を使う）。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from salescall.web import inbox
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import app_access
from tests.test_apology import ApologyBase


class ApologyInInbox(ApologyBase):
    def page(self, s):
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        return TestClient(create_app(app_access(), lambda r: person, b"t")).get("/t/inbox").text

    def test_an_apology_waits_in_the_inbox_and_cannot_be_approved_without_hearing_the_recording(self):
        s = self.fresh(stage_cap=1)
        aid = self.incident(s)                               # 言ってはいけないことを言った通話（システムがお詫びの案を作る）
        order_id = self.draft(s, aid)["order_id"]
        self.recorded(s, order_id)                           # 案の録音の組
        page = self.page(s)
        self.assertIn("お詫びの電話の承認", page)
        self.assertIn("承認するまで、このお客様へは、お約束の電話もかけません。", page)
        self.assertFalse(inbox.CAN_PLAY_RECORDINGS)
        self.assertNotIn("/inbox/apology/approve", page)    # 録音を聞く仕組みができるまで、聞かずに承認できる形を作らない
        self.assertIn("録音を聞く仕組みを準備しています", page)

    def test_an_approved_apology_leaves_the_inbox(self):
        s = self.fresh(stage_cap=1)
        aid = self.incident(s)
        self.order(s, aid)                                   # 契約者が承認（CL-2 の流れ）
        self.assertNotIn("お詫びの電話の承認", self.page(s))


if __name__ == "__main__":
    unittest.main()
