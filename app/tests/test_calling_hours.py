"""時間帯の決まり（Q12。データベースを使わない部品）の試験：境目・24:00・日をまたぐ区間・祝日・次の始まり。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from salescall.core.calendar import CalendarOutOfRange
from salescall.core.calling_hours import band_of, covers, day_key, next_opening, next_opening_in_new_band

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
    """出なかった相手へ、前と別の時間帯にかけ直す（2026-10-15 ユーザー）。区切り [12, 15]＝午前・昼過ぎ・夕方。"""
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

    def test_no_other_band_means_another_day(self):
        night = {k: [["18:00", "22:00"]] for k in KEYS}                                         # 夜だけ開くお店
        self.assertEqual(self.find(night, at(MON, 19), {2}), at(MON + timedelta(days=1), 19))  # 直後でなく、翌日の同じ頃
        friday_evening = at(MON + timedelta(days=4), 16)
        self.assertEqual(self.find(WEEKDAY_9_18, friday_evening, {0, 1, 2}), at(MON + timedelta(days=7), 9))   # 土日を飛ばす


if __name__ == "__main__":
    unittest.main()
