"""知らせの送信の試験（持ち主 CL-3。output/notices.py）。送り方の部品は試験の役（実際には送らない）。架空のデータだけ。"""

from __future__ import annotations

import itertools
import unittest

from salescall.core import notify
from salescall.output import notices
from salescall.output.notices import SendFailed, Step, next_step, text_for
from tests.support import OPERATOR, SYSTEM, World, app_access

_keys = itertools.count()


class FakeTransport:
    """送ったつもりの文を覚える。fail＝失敗の理由（あれば失敗する）。"""
    def __init__(self, fail: str | None = None) -> None:
        self.fail, self.sent = fail, []

    def send(self, text: str) -> None:
        if self.fail:
            raise SendFailed(self.fail)
        self.sent.append(text)


class Order(unittest.TestCase):
    """✅ 2026-11-18 管理者「Rakuten Link が最優先、それが使えなければ、チャットワーク」。"""

    def test_rakuten_link_comes_first(self):
        self.assertEqual(next_step([], ["sms", "chatwork"], 3), Step("sms", "send", 1))

    def test_after_a_failure_the_other_way_is_tried_then_back_again(self):
        h = [("sms", "failed")]
        self.assertEqual(next_step(h, ["sms", "chatwork"], 3), Step("chatwork", "send", 1))
        h.append(("chatwork", "failed"))
        self.assertEqual(next_step(h, ["sms", "chatwork"], 3), Step("sms", "send", 2))

    def test_once_delivered_nothing_more_is_sent(self):
        self.assertIsNone(next_step([("sms", "failed"), ("chatwork", "sent")], ["sms", "chatwork"], 3))

    def test_when_every_way_used_up_its_tries_each_is_given_up_once(self):
        h = [("sms", "failed"), ("chatwork", "failed")]
        self.assertEqual(next_step(h, ["sms", "chatwork"], 1), Step("sms", "give_up", 2))
        h.append(("sms", "gave_up"))
        self.assertEqual(next_step(h, ["sms", "chatwork"], 1), Step("chatwork", "give_up", 2))
        h.append(("chatwork", "gave_up"))
        self.assertIsNone(next_step(h, ["sms", "chatwork"], 1))

    def test_a_way_that_is_not_ready_is_not_used(self):
        self.assertEqual(next_step([], ["chatwork"], 3), Step("chatwork", "send", 1))
        self.assertIsNone(next_step([], [], 3))


class State(unittest.TestCase):
    def test_a_notice_delivered_by_the_second_way_is_delivered(self):   # ⛔ 2026-11-18 の誤り
        self.assertEqual(notices.delivery_state(["failed", "sent"]), "sent")
        self.assertEqual(notices.delivery_state(["failed"]), "trying")
        self.assertEqual(notices.delivery_state(["failed", "failed", "gave_up"]), "gave_up")
        self.assertEqual(notices.delivery_state([]), "none")


class Text(unittest.TestCase):
    def test_a_short_message_fits_one_sms(self):
        t = text_for("program_update", "PC のプログラムに新しい版があります")
        self.assertLessEqual(len(t), notices.SMS_LIMIT)
        self.assertTrue(t.startswith(notices.HEAD))

    def test_a_long_summary_is_cut_and_points_to_the_screen(self):
        t = text_for("program_update", "長い要約。" * 40)
        self.assertEqual(len(t), notices.SMS_LIMIT)
        self.assertTrue(t.endswith(notices.TAIL))


class Delivery(unittest.TestCase):
    """実際のデータベースで：1回届けば終わり・送れなければ回数まで・使い切れば記録して止める（止めない＝ほかは何も変わらない）。"""

    @classmethod
    def setUpClass(cls):
        cls.s = World().setup()

    def _event(self) -> int:
        key = f"試験の知らせ-{next(_keys)}"
        app_access().in_tenant(OPERATOR, self.s["tenant"], lambda tx: notify.emit(
            tx, "program_update", key=key, summary="PC のプログラムに新しい版があります（試験）"))
        return app_access().in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.one(
            "SELECT event_id FROM salescall.notification_events WHERE event_key = %s", (key,)))["event_id"]

    def _deliver(self, eid, transports, tries=2):
        return notices.deliver_event(app_access(), SYSTEM, self.s["tenant"], eid, transports, tries)

    def _rows(self, eid):
        return app_access().in_tenant(OPERATOR, self.s["tenant"], lambda tx: [(r["channel"], r["status"], r["attempt_no"]) for r in tx.all(
            "SELECT channel, status, attempt_no FROM salescall.notification_deliveries WHERE event_id = %s ORDER BY delivery_id", (eid,))])

    def test_an_undelivered_notice_shows_on_the_watch_and_the_home(self):   # 計画書 5-2「知らせが送れたか」・見張りの1か所
        from fastapi.testclient import TestClient
        from salescall.web.app import create_app
        from salescall.web.context import Person
        eid = self._event()
        broken = {"sms": FakeTransport("試験：つながらない")}   # ⏳ 送り方 chatwork はデータベースがまだ受けない（CL-2 へお願い 10）
        for _ in range(6):
            if self._deliver(eid, broken, tries=1) == "gave_up":
                break
        self.assertEqual(notices.delivery_state([r[1] for r in self._rows(eid)]), "gave_up")
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, self.s["tenant"], "架空の契約者"), b"test-only-secret"))
        watch = http.get("/t/watch").text
        part = watch[watch.index('id="undelivered"'):]
        self.assertIn("PC のプログラムに新しい版があります（試験）", part[:3000])
        self.assertIn("送れていない知らせ", http.get("/t").text)               # ホームの「気をつけること」に出る

    def test_a_delivered_notice_is_not_sent_twice(self):
        eid, sms = self._event(), FakeTransport()
        self.assertEqual(self._deliver(eid, {"sms": sms}), "sent")
        self.assertEqual(self._deliver(eid, {"sms": sms}), "done")
        self.assertEqual(len(sms.sent), 1)
        self.assertEqual(self._rows(eid), [("sms", "sent", 1)])

    def test_failures_are_recorded_and_given_up_after_the_tries(self):
        eid, sms = self._event(), FakeTransport(fail="端末に届きませんでした（試験）")
        self.assertEqual([self._deliver(eid, {"sms": sms}) for _ in range(4)], ["failed", "failed", "gave_up", "done"])
        self.assertEqual(self._rows(eid), [("sms", "failed", 1), ("sms", "failed", 2), ("sms", "gave_up", 3)])

    def test_the_notice_screen_shows_a_notice_that_could_not_be_sent(self):
        from fastapi.testclient import TestClient
        from salescall.web.app import create_app
        from salescall.web.context import Person
        eid = self._event()
        for _ in range(3):
            self._deliver(eid, {"sms": FakeTransport(fail="試験")}, tries=2)
        person = Person("試験の契約者", False, self.s["tenant"], "架空の契約者")
        page = TestClient(create_app(app_access(), lambda r: person, b"t")).get("/t/notices").text
        self.assertIn("送れていません", page)


if __name__ == "__main__":
    unittest.main()
