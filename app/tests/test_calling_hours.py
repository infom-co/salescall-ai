"""時間帯の決まり（Q12。データベースを使わない部品）の試験：境目・24:00・日をまたぐ区間・祝日・次の始まり。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from salescall.core.calendar import CalendarOutOfRange
from salescall.core.calling_hours import allowed, band_of, covers, day_key, next_opening, next_opening_in_new_band, subtract

KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")
WEEKDAY_9_18 = {k: ([["09:00", "18:00"]] if k in ("mon", "tue", "wed", "thu", "fri") else []) for k in KEYS}
MON = date(2030, 1, 7)     # 架空の日付（月曜）
no_holiday = lambda d: False


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss)


class Covers(unittest.TestCase):
    def test_start_inclusive_end_exclusive(self):
        self.assertTrue(covers(WEEKDAY_9_18, no_holiday, at(MON, 9), at(MON, 9)))
        self.assertFalse(covers(WEEKDAY_9_18, no_holiday, at(MON, 8, 59, 59), at(MON, 8, 59, 59)))
        self.assertFalse(covers(WEEKDAY_9_18, no_holiday, at(MON, 18), at(MON, 18)))
        self.assertTrue(covers(WEEKDAY_9_18, no_holiday, at(MON, 17, 59, 30), at(MON, 18)))     # 区間の終わりは 18:00 ちょうどまで
        self.assertFalse(covers(WEEKDAY_9_18, no_holiday, at(MON, 17, 59, 45), at(MON, 18, 0, 15)))  # 予約証の期限が外へ出る

    def test_weekend_and_holiday(self):
        sat = MON + timedelta(days=5)
        self.assertFalse(covers(WEEKDAY_9_18, no_holiday, at(sat, 10), at(sat, 10, 0, 30)))
        self.assertFalse(covers(WEEKDAY_9_18, lambda d: d == MON, at(MON, 10), at(MON, 10, 0, 30)))  # 祝日は holiday の時間帯
        shop = dict(WEEKDAY_9_18, holiday=[["10:00", "20:00"]])
        self.assertTrue(covers(shop, lambda d: d == MON, at(MON, 19), at(MON, 19, 0, 30)))
        self.assertEqual(day_key(MON, no_holiday), "mon")

    def test_until_midnight_and_crossing_days(self):
        all_day = {k: [["00:00", "24:00"]] for k in KEYS}
        self.assertTrue(covers(all_day, no_holiday, at(MON, 23, 59, 50), at(MON + timedelta(days=1), 0, 0, 20)))
        evening = {k: [["20:00", "24:00"]] for k in KEYS}
        self.assertFalse(covers(evening, no_holiday, at(MON, 23, 59, 50), at(MON + timedelta(days=1), 0, 0, 20)))  # 翌日の 0:00 は外

    def test_two_ranges_in_a_day_do_not_bridge_the_gap(self):
        split = dict(WEEKDAY_9_18, mon=[["09:00", "12:00"], ["13:00", "18:00"]])
        self.assertTrue(covers(split, no_holiday, at(MON, 11, 59), at(MON, 12)))
        self.assertFalse(covers(split, no_holiday, at(MON, 11, 59, 50), at(MON, 12, 0, 20)))
        self.assertFalse(covers(split, no_holiday, at(MON, 12, 30), at(MON, 12, 30)))

    def test_unknown_calendar_is_not_guessed(self):
        def is_holiday(d):
            raise CalendarOutOfRange("試験")
        with self.assertRaises(CalendarOutOfRange):
            covers(WEEKDAY_9_18, is_holiday, at(MON, 10), at(MON, 10, 0, 30))

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            covers(WEEKDAY_9_18, no_holiday, at(MON, 10), at(MON, 9))


class NextOpening(unittest.TestCase):
    def test_next_start(self):
        length = timedelta(seconds=30)
        self.assertEqual(next_opening(WEEKDAY_9_18, no_holiday, at(MON, 10), length), at(MON, 10))
        self.assertEqual(next_opening(WEEKDAY_9_18, no_holiday, at(MON, 7), length), at(MON, 9))
        self.assertEqual(next_opening(WEEKDAY_9_18, no_holiday, at(MON, 18), length), at(MON + timedelta(days=1), 9))
        fri = MON + timedelta(days=4)
        self.assertEqual(next_opening(WEEKDAY_9_18, no_holiday, at(fri, 17, 59, 45), length), at(MON + timedelta(days=7), 9))
        never = {k: [] for k in KEYS}
        self.assertIsNone(next_opening(never, no_holiday, at(MON, 10), length))



class OtherBand(unittest.TestCase):
    """出なかったお客様へ、前と別の時間帯にかけ直す（2026-10-15 ユーザー）。区切り [12, 15]＝午前・昼過ぎ・夕方。"""
    B = [12, 15]
    L = timedelta(seconds=30)

    def find(self, hours, after, tried):
        return next_opening_in_new_band(hours, no_holiday, after, self.L, self.B, tried)

    def test_band_of(self):
        self.assertEqual([band_of(at(MON, h, m), self.B) for h, m in ((9, 0), (11, 59), (12, 0), (14, 59), (15, 0), (23, 0))],
                         [0, 0, 1, 1, 2, 2])

    def test_moves_to_a_band_not_tried_yet(self):
        self.assertEqual(self.find(WEEKDAY_9_18, at(MON, 10, 30), {0}), at(MON, 12))           # 午前に出なかった ⇒ 昼過ぎ
        self.assertEqual(self.find(WEEKDAY_9_18, at(MON, 12, 30), {0, 1}), at(MON, 15))        # ⇒ 夕方
        self.assertEqual(self.find(WEEKDAY_9_18, at(MON, 16), {1, 2}), at(MON + timedelta(days=1), 9))   # 翌日の午前
        self.assertEqual(self.find(WEEKDAY_9_18, at(MON, 13), {0}), at(MON, 13))               # いまが試していない時間帯

    def test_fractional_seconds_do_not_skip_the_current_band(self):
        """いまの時刻に秒の端数があっても、いまの時間帯を飛ばさない（データベースの now() には端数がある。2026-10-16 の見直し）。"""
        after = at(MON, 14) + timedelta(microseconds=500000)
        self.assertEqual(self.find(WEEKDAY_9_18, after, {0}), after)

    def test_no_other_band_means_another_day(self):
        night = {k: [["18:00", "22:00"]] for k in KEYS}                                         # 夜だけ開くお店
        self.assertIsNone(self.find(night, at(MON, 19), {2}))       # 試していない時間帯が無い（どうするかは呼ぶ側：翌日の同じ頃）
        self.assertIsNone(self.find(WEEKDAY_9_18, at(MON, 16), {0, 1, 2}))

    def test_ng_hours_are_skipped(self):
        """NG時間（昼休み 12〜13時）を引いた時間帯では、昼過ぎの時間帯は 13時から。"""
        hours = subtract(WEEKDAY_9_18, {k: [["12:00", "13:00"]] for k in KEYS})
        self.assertEqual(self.find(hours, at(MON, 10, 30), {0}), at(MON, 13))
        self.assertFalse(covers(hours, no_holiday, at(MON, 12, 30), at(MON, 12, 30)))


class CalleeTime(unittest.TestCase):
    """NG時間は通常は控えるが、お客様が自分で指定した時刻なら、かけてよい（2026-10-15 ユーザー）。"""
    NG = {k: [["12:00", "13:00"]] for k in KEYS}
    L = timedelta(seconds=30)

    def ok(self, start, callee_at=None):
        return allowed(WEEKDAY_9_18, self.NG, no_holiday, start, start + self.L, callee_at=callee_at)

    def test_ng_is_avoided_unless_the_callee_named_the_time(self):
        self.assertTrue(self.ok(at(MON, 10)))
        self.assertFalse(self.ok(at(MON, 12, 30)))                                     # こちらからは控える
        self.assertTrue(self.ok(at(MON, 12, 30), at(MON, 12, 30)))                     # お客様が指定した
        self.assertTrue(self.ok(at(MON, 12, 45), at(MON, 12, 30)))                     # 同じ日の同じ枠の中（少し遅れた）
        self.assertTrue(self.ok(at(MON, 11, 59, 50), at(MON, 12, 30)))                 # 枠の手前から枠へ入る
        self.assertFalse(self.ok(at(MON + timedelta(days=1), 12, 30), at(MON, 12, 30)))   # 日がずれたら控える
        self.assertFalse(self.ok(at(MON, 19), at(MON, 19)))                            # かけてよい時間帯はいつも守る
        self.assertFalse(self.ok(at(MON, 12, 30), at(MON, 10)))                        # 指定がNG時間の外：例外にならない

    def test_only_the_frame_with_the_named_time_is_excused(self):
        """NG時間の枠が2つあるとき、お客様が指定した時刻を含む枠だけかけてよい（ほかの枠は控える。見直し7で試験を足した）。"""
        ng = {k: [["12:00", "13:00"], ["15:00", "16:00"]] for k in KEYS}
        ok = lambda start: allowed(WEEKDAY_9_18, ng, no_holiday, start, start + self.L, callee_at=at(MON, 12, 30))   # noqa: E731
        self.assertEqual((ok(at(MON, 12, 40)), ok(at(MON, 15, 30))), (True, False))


class Subtract(unittest.TestCase):
    def test_cuts(self):
        restaurant = {k: [["11:00", "14:00"], ["17:00", "21:00"]] for k in KEYS}     # ご飯時
        got = subtract({k: [["10:00", "22:00"]] for k in KEYS}, restaurant)
        self.assertEqual(got["mon"], [["10:00", "11:00"], ["14:00", "17:00"], ["21:00", "22:00"]])
        self.assertEqual(subtract(WEEKDAY_9_18, {k: [] for k in KEYS}), WEEKDAY_9_18)
        self.assertEqual(subtract(WEEKDAY_9_18, {k: [["00:00", "24:00"]] for k in KEYS})["mon"], [])
        self.assertEqual(subtract({k: [["00:00", "24:00"]] for k in KEYS}, {k: [["12:00", "13:00"]] for k in KEYS})["sun"],
                         [["00:00", "12:00"], ["13:00", "24:00"]])


if __name__ == "__main__":
    unittest.main()
