"""かけ直しの時刻の読み方の試験（core/spoken_time.py）。読めるものは正しい時刻に、決まらないものは「読めない」に（推し量らない）。"""

from __future__ import annotations

import unittest
from datetime import datetime

from salescall.core.spoken_time import AT, JST, UNREADABLE, UNSPECIFIED, read

# 2026-10-14（水）10:00 の通話
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=JST)


def at(*args):
    return datetime(*args, tzinfo=JST)


class Readable(unittest.TestCase):
    def test_exact_times(self):
        cases = {
            "明日の午後3時": at(2026, 10, 15, 15, 0),
            "あしたの午前10時半": at(2026, 10, 15, 10, 30),
            "明後日の15時": at(2026, 10, 16, 15, 0),
            "今日の午後3時頃": at(2026, 10, 14, 15, 0),
            "午後3時以降": at(2026, 10, 14, 15, 0),          # 日が無ければ今日
            "14時30分から": at(2026, 10, 14, 14, 30),
            "20日の午後1時": at(2026, 10, 20, 13, 0),
            "10日の午前11時": at(2026, 11, 10, 11, 0),       # 過ぎた日は次の月
            "10月20日(火)午後2時": at(2026, 10, 20, 14, 0),
            "1月5日の午前9時": at(2027, 1, 5, 9, 0),         # 過ぎた月は次の年
            "来週の月曜日の午前10時": at(2026, 10, 19, 10, 0),
            "再来週の金曜の夕方5時": at(2026, 10, 30, 17, 0),
            "金曜日の夜7時": at(2026, 10, 16, 19, 0),        # 週の言葉が無い曜日は、明日以後でいちばん近いその曜日
            "明日の正午": at(2026, 10, 15, 12, 0),
            "明日の昼1時": at(2026, 10, 15, 13, 0),
            "明日の朝9時": at(2026, 10, 15, 9, 0),
            "明日の12時": at(2026, 10, 15, 12, 0),
            "明日の午後三時": at(2026, 10, 15, 15, 0),         # 漢数字
            "十五日の午後二時": at(2026, 10, 15, 14, 0),
            "明日 の 午後 3 時": at(2026, 10, 15, 15, 0),      # 空白
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual((read(text, NOW).kind, read(text, NOW).at), (AT, want))

    def test_unspecified(self):
        for text in (None, "", "いつでも", "いつでも大丈夫です", "そちらのご都合で結構です"):
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).kind, UNSPECIFIED)

    def test_asap_is_the_call_time(self):
        """すぐの答えは、通話の時刻（門が許す最も早い時刻にかける）。"""
        for text in ("すぐ", "今すぐ", "なるべく早く", "できるだけ早くお願いします", "至急"):
            with self.subTest(text=text):
                self.assertEqual((read(text, NOW).kind, read(text, NOW).at), (AT, NOW))


class NotGuessed(unittest.TestCase):
    def test_unreadable(self):
        for text in ("明日の朝", "夕方", "午前中", "3時",                # 時刻が無い・午前か午後かが決まらない
                     "明日の3時までに", "明日の午後3時でしたら",       # 決まりに無い言葉
                     "午前9時",                                       # 過ぎた時刻（今日の 9:00）
                     "水曜日の午後3時",                               # 今日と同じ曜日
                     "来週の日曜日の午後3時",                         # 日曜日（週の始まりで変わる）
                     "10月20日(水)午後2時",                           # 日と曜日が合わない
                     "2月30日の午後1時", "明日の午前12時", "明日の夜3時", "明日の25時", "明日の午後3時70分",
                     "来月の午後3時", "明日か明後日の午後3時"):
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.at), (UNREADABLE, None))
                self.assertTrue(r.why)

    def test_week_words_on_a_sunday(self):
        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=JST)
        self.assertEqual(read("来週の月曜日の午後3時", sunday).kind, UNREADABLE)
        self.assertEqual(read("月曜日の午後3時", sunday).at, at(2026, 10, 19, 15, 0))

    def test_uses_japan_time(self):
        """通話の時刻が別の時差で渡されても、日本の日付で読む（日本の 10-15 01:00＝世界時の 10-14 16:00）。"""
        from datetime import timezone
        utc = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(read("明日の午後3時", utc).at, at(2026, 10, 16, 15, 0))


if __name__ == "__main__":
    unittest.main()
