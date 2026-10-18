"""かけ直しの時刻の読み方の試験（core/spoken_time.py）。言葉を「かけてよい時間の幅」で読み、1つに決まらない言い方は
確かめ（CONFIRM）に、読めない言い方は読めない（UNREADABLE）にする（推し量らない）。2026-10-19 ユーザー決定。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from salescall.core.spoken_time import AT, CONFIRM, DAY, JST, UNREADABLE, UNSPECIFIED, WINDOW, read

# 2026-10-14（水）10:00 の通話
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=JST)
H = 3600


def at(*args):
    return datetime(*args, tzinfo=JST)


class Exact(unittest.TestCase):
    def test_exact_times(self):
        """日と時刻がはっきり決まる言い方は、ちょうどの時刻（お客様が指定した時刻＝NG時間の枠でもかけてよい根拠）。"""
        cases = {
            "明日の午後3時": at(2026, 10, 15, 15, 0),
            "あしたの午前10時半": at(2026, 10, 15, 10, 30),
            "明後日の15時": at(2026, 10, 16, 15, 0),
            "今日の午後3時頃": at(2026, 10, 14, 15, 0),
            "20日の午後1時": at(2026, 10, 20, 13, 0),
            "10日の午前11時": at(2026, 11, 10, 11, 0),       # 過ぎた日は次の月
            "1月5日の午前9時": at(2027, 1, 5, 9, 0),         # 過ぎた月は次の年
            "来週の月曜日の午前10時": at(2026, 10, 19, 10, 0),
            "再来週の金曜の夕方5時": at(2026, 10, 30, 17, 0),
            "金曜日の夜7時": at(2026, 10, 16, 19, 0),        # 週の言葉が無い曜日は、明日以後でいちばん近いその曜日
            "明日の正午": at(2026, 10, 15, 12, 0),
            "明日の朝9時": at(2026, 10, 15, 9, 0),
            "明日の12時": at(2026, 10, 15, 12, 0),
            "明日の午後三時": at(2026, 10, 15, 15, 0),         # 漢数字
            "十五日の午後二時": at(2026, 10, 15, 14, 0),
            "明日 の 午後 3 時": at(2026, 10, 15, 15, 0),      # 空白
            "夕方5時頃": at(2026, 10, 14, 17, 0),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.exact), (AT, want, True))


class Windows(unittest.TestCase):
    def test_time_words_are_windows(self):
        """「夕方」「3時まで」なども、それに合わせてかける（幅。daily＝1日の中の幅＝次の日の同じ幅にかけ直すのに使う）。"""
        cases = {
            "夕方": (at(2026, 10, 14, 16), at(2026, 10, 14, 18), (16 * H, 18 * H)),
            "午後3時まで": (NOW, at(2026, 10, 14, 15), (0, 15 * H)),              # 始まりは、いまより前にしない
            "午前中": (NOW, at(2026, 10, 14, 12), (0, 12 * H)),
            "15時以降": (at(2026, 10, 14, 15), at(2026, 10, 15), (15 * H, DAY)),
            "午後3時から5時": (at(2026, 10, 14, 15), at(2026, 10, 14, 17), (15 * H, 17 * H)),
            "明日の午後": (at(2026, 10, 15, 13), at(2026, 10, 16), (13 * H, DAY)),
            "金曜日の午前中": (at(2026, 10, 16), at(2026, 10, 16, 12), (0, 12 * H)),
            "10月20日(火)の夕方": (at(2026, 10, 20, 16), at(2026, 10, 20, 18), (16 * H, 18 * H)),
            "来週の夕方": (at(2026, 10, 19, 16), at(2026, 10, 25, 18), (16 * H, 18 * H)),
            "明日の3時から": None,                                                 # 午前か午後か（下の確かめ）
        }
        for text, want in cases.items():
            if want is None:
                continue
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.end, r.daily, r.exact), (WINDOW, *want, False))

    def test_days_without_a_time(self):
        """日だけの約束（「明日ならいつでも」「13日であれば、そちらのご都合で」「来週」「月末」など）は、その日のいつでも
        （daily は無い＝時間の言葉が無い。日が過ぎたら契約者の間隔＝案A）。"""
        cases = {
            "明日ならいつでも": (at(2026, 10, 15), at(2026, 10, 16)),
            "13日であれば、そちらのご都合で": (at(2026, 11, 13), at(2026, 11, 14)),
            "明日なるべく早く": (at(2026, 10, 15), at(2026, 10, 16)),
            "来週": (at(2026, 10, 19), at(2026, 10, 26)),
            "来週中": (at(2026, 10, 19), at(2026, 10, 26)),
            "今週中": (NOW, at(2026, 10, 19)),
            "週末": (at(2026, 10, 17), at(2026, 10, 19)),
            "来週末": (at(2026, 10, 24), at(2026, 10, 26)),
            "来週の土日": (at(2026, 10, 24), at(2026, 10, 26)),
            "月末": (at(2026, 10, 27), at(2026, 11, 1)),
            "来月": (at(2026, 11, 1), at(2026, 12, 1)),
            "来月はいつでも": (at(2026, 11, 1), at(2026, 12, 1)),
            "来月の上旬": (at(2026, 11, 1), at(2026, 11, 11)),
            "来月の中旬": (at(2026, 11, 11), at(2026, 11, 21)),
            "来月下旬": (at(2026, 11, 21), at(2026, 12, 1)),
            "来月の頭": (at(2026, 11, 1), at(2026, 11, 6)),
            "11月末": (at(2026, 11, 26), at(2026, 12, 1)),
            "11月": (at(2026, 11, 1), at(2026, 12, 1)),
            "今月中": (NOW, at(2026, 11, 1)),
            "下旬": (at(2026, 10, 21), at(2026, 11, 1)),
            "2月": (at(2027, 2, 1), at(2027, 3, 1)),                        # 過ぎた月は次の年
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.end, r.daily), (WINDOW, *want, None))
        r = read("週末の午後", NOW)
        self.assertEqual((r.start, r.end, r.daily), (at(2026, 10, 17, 13), at(2026, 10, 19), (13 * H, DAY)))

    def test_asap_is_from_now(self):
        """すぐの答えは、いまから（門が許す最も早い時刻にかける）。終わりは無い。"""
        for text in ("すぐ", "今すぐ", "なるべく早く", "できるだけ早くお願いします", "至急"):
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.end, r.daily), (WINDOW, NOW, None, None))

    def test_unspecified(self):
        for text in (None, "", "いつでも", "いつでも大丈夫です", "そちらのご都合で結構です"):
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).kind, UNSPECIFIED)


class NotGuessed(unittest.TestCase):
    def test_needs_confirming(self):
        """言い方は分かるが、いつかが1つに決まらない ⇒ 通話の中で確かめる（推し量らない）。"""
        for text in ("昼", "お昼", "昼過ぎ", "お昼過ぎ", "明日のお昼", "明日の昼1時",   # 2026-10-19 ユーザー：昼は確かめる
                     "3時", "3時まで", "明日の3時から",                            # 午前か午後かが決まらない
                     "午前9時",                                                    # 過ぎた時刻（今日の 9:00）
                     "水曜日の午後3時",                                            # 今日と同じ曜日
                     "来週の日曜日の午後3時",                                      # 日曜日（週の始まりで変わる）
                     "10月20日(水)午後2時",                                        # 日と曜日が合わない
                     "2月30日の午後1時", "明日の午前12時", "明日の夜3時", "明日の25時", "明日の午後3時70分",
                     "来週の3時", "来週の午後3時",                                  # いくつもの日にまたがる時刻
                     "上旬",                                                       # 今月の上旬は過ぎた
                     "午後5時から3時"):
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start), (CONFIRM, None))
                self.assertTrue(r.why)

    def test_unreadable(self):
        for text in ("明日か明後日の午後3時", "明日の3時でしたら大丈夫です", "来月の午後3時ごろの予定です", "折り返し",
                     "近いうちに"):
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertIn(r.kind, (UNREADABLE, CONFIRM))
                self.assertIsNone(r.start)
        self.assertEqual(read("近いうちに", NOW).kind, UNREADABLE)

    def test_past_windows_need_confirming(self):
        evening = datetime(2026, 10, 14, 19, 0, tzinfo=JST)
        self.assertEqual(read("夕方", evening).kind, CONFIRM)               # 今日の夕方は過ぎた（明日か決まらない）
        self.assertEqual(read("午前中", evening).kind, CONFIRM)
        r = read("今週の夕方", evening)                                      # 今週の残りの日の夕方
        self.assertEqual((r.kind, r.start, r.end), (WINDOW, evening, at(2026, 10, 18, 18)))

    def test_week_words_on_a_sunday(self):
        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=JST)
        for text in ("来週の月曜日の午後3時", "来週", "今週中", "週末", "来週末"):
            with self.subTest(text=text):
                self.assertEqual(read(text, sunday).kind, CONFIRM)
        self.assertEqual(read("月曜日の午後3時", sunday).start, at(2026, 10, 19, 15, 0))

    def test_uses_japan_time(self):
        """通話の時刻が別の時差で渡されても、日本の日付で読む（日本の 10-15 01:00＝世界時の 10-14 16:00）。"""
        utc = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(read("明日の午後3時", utc).start, at(2026, 10, 16, 15, 0))


if __name__ == "__main__":
    unittest.main()
