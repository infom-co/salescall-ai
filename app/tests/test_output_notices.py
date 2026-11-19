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


class Timing(unittest.TestCase):
    """いつ送るか（✅ 2026-11-19 管理者「(1) (2)はすぐ　そのほかはそれでいい。ダッシュボードには全てすぐに報告」）。"""
    HOURS = {k: [["07:00", "22:00"]] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")} | {"holiday": []}

    def at(self, kind, when, urgent=False, holiday=False, hours=HOURS):
        from datetime import datetime
        return notices.send_time(kind, urgent, datetime(*when), hours, lambda d: holiday and d == datetime(*when).date())   # その日だけ祝日

    def test_every_notice_kind_has_a_timing(self):
        self.assertEqual(set(notices.TIMING), set(notify.KINDS))

    def test_stops_and_approvals_go_at_once_even_at_night(self):
        for kind in ("stopped", "resumed", "more_calls", "correction_call", "forbidden_speech"):
            with self.subTest(kind=kind):
                st = self.at(kind, (2026, 11, 19, 3, 0))
                self.assertEqual((st.at.hour, st.bundled), (3, False))

    def test_urgent_ones_go_at_once_whatever_the_kind(self):
        self.assertFalse(self.at("quality_stage", (2026, 11, 19, 3, 0), urgent=True).bundled)

    def test_problems_go_at_once_in_the_hours_and_are_bundled_for_the_next_opening_outside(self):
        self.assertEqual(self.at("complaint", (2026, 11, 19, 10, 0)), notices.SendTime(self.at("complaint", (2026, 11, 19, 10, 0)).at, False))
        night = self.at("complaint", (2026, 11, 19, 23, 0))
        self.assertEqual((night.at.day, night.at.hour, night.bundled), (20, 7, True))     # 翌朝、まとめて
        off = self.at("deficit_warning", (2026, 11, 23, 10, 0), holiday=True)            # 休みの日（送ってよい時間が無い日）
        self.assertTrue(off.bundled)
        self.assertGreater(off.at.day, 23)

    def test_reports_wait_for_the_morning_bundle(self):
        late = self.at("quality_stage", (2026, 11, 19, 10, 0))
        self.assertEqual((late.at.day, late.at.hour, late.bundled), (20, 7, True))
        early = self.at("quality_stage", (2026, 11, 19, 3, 0))
        self.assertEqual((early.at.day, early.at.hour, early.bundled), (19, 7, True))     # その日の朝

    def test_without_the_setting_everything_goes_at_once(self):   # 設定の定義が届くまで（CL-2 へのお願い 52）は取りこぼさない側
        self.assertFalse(self.at("quality_stage", (2026, 11, 19, 3, 0), hours=None).bundled)

    def test_a_bundle_fits_one_sms(self):
        t = notices.bundle_text(["complaint", "complaint", "deficit_warning", "quality_stage", "pc_down", "backup_failed", "budget"])
        self.assertLessEqual(len(t), notices.SMS_LIMIT)
        self.assertTrue(t.startswith(notices.HEAD + "知らせ7件："))


class Delivery(unittest.TestCase):
    """実際のデータベースで：1回届けば終わり・送れなければ回数まで・使い切れば記録して止める（止めない＝ほかは何も変わらない）。"""

    @classmethod
    def setUpClass(cls):
        cls.s = World().setup()
        from salescall.core import settings
        anytime = {k: [["00:00", "24:00"]] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")}
        # 送る流れの試験は、流す時刻で答えが変わらないように、この契約者の「知らせを送ってよい時間」をいつでもにする（契約者が選べる値。
        # いつ送るかの決まりそのものは Timing の試験が確かめる）
        app_access().in_tenant(OPERATOR, cls.s["tenant"], lambda tx: settings.set_value(
            tx, notices.SEND_HOURS, anytime, scope="tenant", status="decided", rationale="試験：いつでも送ってよい", review_when="試験のあいだ",
            tenant_id=cls.s["tenant"]))

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

    def test_bundled_notices_are_sent_in_one_message_and_recorded_for_each(self):
        import unittest.mock
        from salescall.core import calling_hours
        eids = [self._event() for _ in range(3)]
        due = lambda tx, tenant_id: (lambda e: notices.SendTime(calling_hours.local(e["occurred_at"]), True))   # noqa: E731  いま送る時刻の、まとめる知らせ
        sms = FakeTransport()
        with unittest.mock.patch.object(notices, "schedule", due):
            self.assertEqual(self._deliver(eids[0], {"sms": sms}), "bundled")             # 1件ずつは送らない
            got = notices.deliver_bundle(app_access(), SYSTEM, self.s["tenant"], {"sms": sms}, 2)
            self.assertEqual(len(sms.sent), 1)                                            # 1通にまとめた
            self.assertGreaterEqual(got["sent"], 3)
            for eid in eids:
                self.assertEqual(self._rows(eid), [("sms", "sent", 1)])                   # 含めた知らせのそれぞれに記録
            self.assertEqual(notices.deliver_bundle(app_access(), SYSTEM, self.s["tenant"], {"sms": sms}, 2), {})   # もう送らない
            self.assertEqual(len(sms.sent), 1)

    def test_a_bundle_that_cannot_be_sent_is_retried_then_given_up_for_each(self):
        import unittest.mock
        from salescall.core import calling_hours
        due = lambda tx, tenant_id: (lambda e: notices.SendTime(calling_hours.local(e["occurred_at"]), True))   # noqa: E731
        broken = FakeTransport("試験：つながらない")
        with unittest.mock.patch.object(notices, "schedule", due):
            eid = self._event()
            for _ in range(6):
                notices.deliver_bundle(app_access(), SYSTEM, self.s["tenant"], {"sms": broken}, 1)
            self.assertEqual(notices.delivery_state([r[1] for r in self._rows(eid)]), "gave_up")

    def test_the_sender_called_by_the_supervisor_reads_where_to_send_and_sends_once(self):   # CL-2 4-434・4-442（plugins.NoticeSender）
        from salescall.core import settings
        sent = FakeTransport()
        sender = notices.Sender(chatwork_token="", sms=lambda number: sent)
        none = World().setup()                                                    # 送り先を入れていない契約者：何も送らない・記録もしない
        app_access().in_tenant(OPERATOR, none["tenant"], lambda tx: notify.emit(tx, "stopped", key="試験：送り先の無い知らせ",
                                                                                 summary="架空の止めた知らせ"))
        self.assertEqual(sender.deliver(app_access(), SYSTEM, none["tenant"]), {})
        app_access().in_tenant(OPERATOR, self.s["tenant"], lambda tx: settings.set_value(
            tx, "notify.sms_to", "+81300000000", scope="tenant", status="decided", rationale="試験：架空の番号", review_when="試験のあいだ",
            tenant_id=self.s["tenant"]))
        eid = self._event()
        got = sender.deliver(app_access(), SYSTEM, self.s["tenant"])
        self.assertGreaterEqual(got.get("sent", 0), 1)
        self.assertIn(("sms", "sent", 1), self._rows(eid))
        before = len(sent.sent)
        sender.deliver(app_access(), SYSTEM, self.s["tenant"])
        self.assertEqual(len(sent.sent), before)                                  # 届いた知らせは、もう送らない

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
