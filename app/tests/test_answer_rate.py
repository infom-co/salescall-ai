"""番号ごとの応答率の見張りの試験（Intent.md 8-6。DESIGN.md 5-2-3。core/answer_rate.py・表の作り 0068）。

- 落ちたかは数だけで決まる1か所（answer_rate.judge）：今日の割合を、前の日々の同じ時間帯の割合と比べる。本数が少ない・
  いつもの割合が分からない・偶然でも起きる下がり方では知らせない。
- 数えるのはデータベースの1か所（line_answer_counts）。鳴らせなかった通話・お詫び・かけ直しは数えない。
- 落ちたら、その契約者へ1回線1日1回知らせる。発信は止めない。
⚠️ 中身は架空のもの。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from salescall.core import answer_rate, attempts
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, World

TODAY = date(2026, 11, 10)


def rows(day, hour, dials, answers):
    return {"day": day, "hour": hour, "dials": dials, "answers": answers}


def baseline(rate_by_hour, per_hour=20):
    """前の14日の、時間帯ごとの数（1日あたり per_hour 本ずつ）。"""
    return [rows(TODAY - timedelta(days=d), h, per_hour, round(per_hour * r)) for d in range(1, 15)
            for h, r in rate_by_hour.items()]


class Judge(unittest.TestCase):
    def judge(self, data, **kw):
        return answer_rate.judge(data, TODAY, ratio=kw.get("ratio", 0.5), min_dials=kw.get("min_dials", 30))

    def test_a_clear_drop_is_found(self):
        got = self.judge(baseline({10: 0.3, 14: 0.3}) + [rows(TODAY, 10, 20, 1), rows(TODAY, 14, 20, 2)])
        self.assertIsNotNone(got)
        self.assertEqual((got.dials, got.answers), (40, 3))
        self.assertAlmostEqual(got.expected, 0.3, places=2)
        self.assertLess(got.chance, answer_rate.SIGNIFICANCE)

    def test_hours_that_are_always_low_are_not_a_drop(self):
        """昼休みは、いつも出てもらいにくい：今日が昼休みばかりでも、同じ時間帯どうしで比べるので知らせない。"""
        data = baseline({10: 0.4, 12: 0.1}) + [rows(TODAY, 12, 40, 4)]
        self.assertIsNone(self.judge(data))

    def test_too_few_dials_or_no_history_or_chance_is_not_a_drop(self):
        base = baseline({10: 0.3})
        self.assertIsNone(self.judge(base + [rows(TODAY, 10, 20, 0)]))                     # 今日の本数が足りない
        self.assertIsNone(self.judge(baseline({10: 0.3}, per_hour=5) + [rows(TODAY, 10, 40, 0)]))   # いつもが分からない
        self.assertIsNone(self.judge(base + [rows(TODAY, 10, 40, 9)]))                     # 少し低いだけ（比の内側）
        self.assertIsNone(self.judge(base + [rows(TODAY, 10, 40, 5)], ratio=0.3))          # 設定の比より上
        self.assertIsNone(self.judge(baseline({10: 0.08}) + [rows(TODAY, 10, 30, 1)]))     # 偶然でも起きる下がり方

    def test_days_outside_the_window_are_ignored(self):
        old = [rows(TODAY - timedelta(days=30), 10, 500, 400)]                           # 古すぎる日は、いつもに入れない
        got = self.judge(old + baseline({10: 0.3}) + [rows(TODAY, 10, 40, 2)])
        self.assertAlmostEqual(got.expected, 0.3, places=2)


class Counts(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def dial(self, path):
        aid = self.w.insert_claim(self.s)
        self.w.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: attempts.start_dialing(tx, aid))
        for st in path:
            self.w.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx, st=st: attempts.move(tx, aid, st))
        return aid

    def test_only_rung_calls_are_counted_and_only_for_that_contractor(self):
        self.dial(["ringing", "answered", "in_conversation", "ending", "ended"])
        self.dial(["ringing", "no_answer"])
        self.dial(["busy"])
        self.dial(["call_failed"])                         # 鳴らせなかった：番号の評判と関係ない
        today = self.w.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        got = self.w.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: tx.all(
            "SELECT sum(dials)::int AS d, sum(answers)::int AS a FROM salescall.line_answer_counts(%s, %s, %s)",
            (self.s["line"], today, today)))[0]
        self.assertEqual((got["d"], got["a"]), (3, 1))
        other = World().setup()
        seen = self.w.a.in_tenant(SYSTEM, other["tenant"], lambda tx: tx.all(
            "SELECT * FROM salescall.line_answer_counts(%s, %s, %s)", (self.s["line"], today, today)))
        self.assertEqual(seen, [])                                      # ほかの契約者の回線は数えない
        inside = self.w.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: tx.all(
            "SELECT * FROM salescall.line_answer_counts(%s, %s, %s)", (self.s["line"], today, today)))
        self.assertEqual(inside, [])                                    # 依頼者の取引からは読まない（回線は契約者のもの）


class Watch(unittest.TestCase):
    def test_a_drop_is_told_to_the_contractor_once_a_day_and_nothing_stops(self):
        w = World()
        s = w.setup()
        drop = answer_rate.Drop(40, 3, 0.075, 0.3, 0.0001)
        with mock.patch.object(answer_rate, "check", return_value=drop):
            for _ in range(2):
                w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: answer_rate.watch(tx, s["tenant"]))
        notes = w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'answer_rate_drop'"))
        self.assertEqual(len(notes), 1)                                  # 1回線1日1回
        self.assertIn("40本中 3本", notes[0]["summary"])
        stops = w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all("SELECT 1 FROM salescall.open_stop_events"))
        self.assertEqual(stops, [])                                      # 止めない（お客様に影響しない）

    def test_settings_are_per_contractor(self):
        w = World()
        s = w.setup()
        with self.assertRaises(Rejected):                                # 依頼者ごとの値は入れられない（回線は契約者のもの）
            w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale, review_when,"
                " set_by) VALUES ('line.answer_drop_ratio', 'client', %s, %s, '0.3', 'decided', '試験', '試験', '試験')",
                (s["tenant"], s["client"])))


if __name__ == "__main__":
    unittest.main()
