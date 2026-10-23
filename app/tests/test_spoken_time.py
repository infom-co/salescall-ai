"""かけ直しの時刻の読み方の試験（core/spoken_time.py）。言葉を「かけてよい時間の幅」で読み、1つに決まらない言い方は
確かめ（CONFIRM）に、読めない言い方は読めない（UNREADABLE）にする（推し量らない）。2026-10-19 ユーザー決定。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from salescall.core.spoken_time import AT, CONFIRM, DAY, JST, UNREADABLE, UNSPECIFIED, WINDOW, next_occurrence, read

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
        （daily は無い＝時間の言葉が無い。理由の分からない日は、日が過ぎたら時刻の決まっていない約束の方針）。"""
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
            "月末": (at(2026, 10, 29), at(2026, 11, 1)),                     # 月の最後の3日（2026-10-19 ユーザー）
            "来月": (at(2026, 11, 1), at(2026, 12, 1)),
            "来月はいつでも": (at(2026, 11, 1), at(2026, 12, 1)),
            "来月の上旬": (at(2026, 11, 1), at(2026, 11, 11)),
            "来月の中旬": (at(2026, 11, 11), at(2026, 11, 21)),
            "来月下旬": (at(2026, 11, 21), at(2026, 12, 1)),
            "来月の頭": (at(2026, 11, 1), at(2026, 11, 4)),                  # 1〜3日（2026-10-19 ユーザー）
            "来月初め": (at(2026, 11, 1), at(2026, 11, 4)),
            "11月末": (at(2026, 11, 28), at(2026, 12, 1)),
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


class MoreWords(unittest.TestCase):
    """2026-10-19 ユーザー：「週中や再来週、明後日 明々後日、あさって、しあさって、なども大丈夫ですか？他にもあるんじゃないかな！？」"""

    def test_more_day_and_time_words(self):
        cases = {   # 言葉 ⇒ (答え, 始まり, 終わり, 1日の中の幅, 日の種類)。通話は 2026-10-14（水）10:00
            "しあさって": (WINDOW, at(2026, 10, 17), at(2026, 10, 18), None, None),
            "明々後日の午後": (WINDOW, at(2026, 10, 17, 13), at(2026, 10, 18), (13 * H, DAY), None),
            "週中": (WINDOW, NOW, at(2026, 10, 16), None, None),                     # 火〜木（今日から）
            "来週の週中": (WINDOW, at(2026, 10, 20), at(2026, 10, 23), None, None),
            "来週半ば": (WINDOW, at(2026, 10, 20), at(2026, 10, 23), None, None),
            "再来週": (WINDOW, at(2026, 10, 26), at(2026, 11, 2), None, None),
            "週明け": (WINDOW, at(2026, 10, 19), at(2026, 10, 20), None, None),          # 明日以後でいちばん近い月曜
            "今日中": (WINDOW, NOW, at(2026, 10, 15), None, None),
            "明日中": (WINDOW, at(2026, 10, 15), at(2026, 10, 16), None, None),
            "今晩": (WINDOW, at(2026, 10, 14, 18), at(2026, 10, 14, 20), (18 * H, 20 * H), None),
            "明朝": (WINDOW, at(2026, 10, 15), at(2026, 10, 15, 10), (0, 10 * H), None),
            "3日後": (WINDOW, at(2026, 10, 17), at(2026, 10, 18), None, None),
            "1週間後": (WINDOW, at(2026, 10, 21), at(2026, 10, 22), None, None),
            "2か月後": (WINDOW, at(2026, 12, 14), at(2026, 12, 15), None, None),
            "2〜3日後": (WINDOW, at(2026, 10, 16), at(2026, 10, 18), None, None),
            "年内": (WINDOW, NOW, at(2027, 1, 1), None, None),
            "3時台": (WINDOW, at(2026, 10, 14, 15), at(2026, 10, 14, 16), (15 * H, 16 * H), None),
            "夕方以降": (WINDOW, at(2026, 10, 14, 16), at(2026, 10, 15), (16 * H, DAY), None),
            "午後一": (WINDOW, at(2026, 10, 14, 13), at(2026, 10, 14, 14), (13 * H, 14 * H), None),
            "午後イチでお願いします": (WINDOW, at(2026, 10, 14, 13), at(2026, 10, 14, 14), (13 * H, 14 * H), None),
            "3時以降でしたら大丈夫です": (WINDOW, at(2026, 10, 14, 15), at(2026, 10, 15), (15 * H, DAY), None),
            "平日の夕方": (WINDOW, NOW, None, (16 * H, 18 * H), ("mon", "tue", "wed", "thu", "fri")),
            "土日祝ならいつでも": (WINDOW, NOW, None, None, ("sat", "sun", "holiday")),
            "来週の平日の午前中": (WINDOW, at(2026, 10, 19), at(2026, 10, 25, 12), (0, 12 * H), ("mon", "tue", "wed", "thu", "fri")),
            "今晩8時": (AT, at(2026, 10, 14, 20), None, None, None),
            "明朝9時": (AT, at(2026, 10, 15, 9), None, None, None),
            "30分後": (WINDOW, at(2026, 10, 14, 10, 30), None, None, None),   # できるだけ早く（「すぐ」と同じ。2026-10-19 ユーザー）
            "1時間半後": (WINDOW, at(2026, 10, 14, 11, 30), None, None, None),
            "4時前後": (AT, at(2026, 10, 14, 16), None, None, None),
            "あとで": (UNSPECIFIED, None, None, None, None),
            "後ほどお願いします": (UNSPECIFIED, None, None, None, None),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.end, r.daily, r.days), want)
        for text in ("年明け", "年末", "数日後", "近いうちに", "連休明け", "夕方過ぎ", "夕方まで", "お昼前", "深夜", "月初"):
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).kind, CONFIRM)                 # 幅が1つに決まらない（月初＝今月は過ぎた）

    def test_words_added_in_the_review(self):
        """2026-10-19 の見直しで足した言い方（前は読めず、判定待ちになっていた）と、年の付いた日・過ぎた日・遠い日。"""
        cases = {
            "来月3日": (WINDOW, at(2026, 11, 3), at(2026, 11, 4), None),
            "来月の3日の午後2時": (AT, at(2026, 11, 3, 14), None, None),
            "今月の20日": (WINDOW, at(2026, 10, 20), at(2026, 10, 21), None),
            "来月の初め": (WINDOW, at(2026, 11, 1), at(2026, 11, 4), None),
            "来月の月の頭": (WINDOW, at(2026, 11, 1), at(2026, 11, 4), None),
            "月の終わり": (WINDOW, at(2026, 10, 29), at(2026, 11, 1), None),
            "来年の1月5日": (WINDOW, at(2027, 1, 5), at(2027, 1, 6), None),
            "2027年1月5日なら": (WINDOW, at(2027, 1, 5), at(2027, 1, 6), None),
            "来年の9月中": (WINDOW, at(2027, 9, 1), at(2027, 10, 1), None),
            "4月10日": (WINDOW, at(2027, 4, 10), at(2027, 4, 11), None),          # 半年の内なら来年
            "3時がいいです": (AT, at(2026, 10, 14, 15), None, None),
            "明日の夕方ごろ": (WINDOW, at(2026, 10, 15, 16), at(2026, 10, 15, 18), (16 * H, 18 * H)),
            "明日の正午ごろ": (AT, at(2026, 10, 15, 12), None, None),
            "30分後くらい": (WINDOW, at(2026, 10, 14, 10, 30), None, None),
            "1時間後ぐらい": (WINDOW, at(2026, 10, 14, 11), None, None),
            "明日の午前中ならいつでも": (WINDOW, at(2026, 10, 15), at(2026, 10, 15, 12), (0, 12 * H)),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.end, r.daily), want)
        for text in ("明日の3時10分前",     # 2時50分とも、3時10分より前とも取れる
                     "10月13日",            # 昨日：言い違いか来年か決まらない（前は1年後の同じ日と読んだ）
                     "5月10日", "9月中",    # 半年より先：同上
                     "今月13日", "来月31日", "2025年1月5日",
                     "月の初め"):           # 今月の初めは過ぎた
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).kind, CONFIRM)
        self.assertEqual(read("13日", NOW).start, at(2026, 11, 13))           # 日だけは今月か来月
        for now, text in ((at(2026, 11, 5, 10), "31日"), (at(2027, 2, 3, 10), "30日"),   # その月に無い日：次の月へ送らない
                          (NOW, "明日の午前0時まで")):                                  # 幅が空になる
            with self.subTest(text=text):
                self.assertEqual(read(text, now).kind, CONFIRM)
        self.assertEqual(read("明日の3時前", NOW).end, at(2026, 10, 15, 15))   # 分の無い「前」はこれまでどおり

    def test_am_or_pm_from_business_hours(self):
        """午前・午後の無い時刻は、お客様の営業時間に入るほう（2026-10-19 ユーザー：「通常午前3時に電話をかけてほしい人はいない
        ので、営業時間の範囲だけ」）。営業時間は [始まり, 終わり)：終わりの時刻ちょうどは入らない（⛔ 見直し15：前は時の単位で
        終わりの時も含め、「明日の7時」を19時＝営業の終わった時刻と読んで、依頼者の時間帯の外でもかけた）。"""
        company = (9 * H, 19 * H)
        self.assertEqual(read("3時", NOW, company).start, at(2026, 10, 14, 15))
        self.assertEqual(read("6時半", NOW, company).start, at(2026, 10, 14, 18, 30))
        for text in ("7時", "明日の7時", "7時以降", "明日の7時半"):   # 7時（始まりより前）も19時（終わりちょうど）も入らない ⇒ 確かめる
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW, company).kind, CONFIRM)
        self.assertEqual(read("明日の9時", NOW, (9 * H + 30 * 60, 19 * H)).kind, CONFIRM)   # 分まで見る（9時は9時半より前）
        self.assertEqual(read("明日の9時", NOW, company).start, at(2026, 10, 15, 9))
        self.assertEqual(read("12時", NOW, company).start, at(2026, 10, 14, 12))    # 12時は昼の12時
        r = read("3時まで", NOW, company)
        self.assertEqual((r.kind, r.end), (WINDOW, at(2026, 10, 14, 15)))
        r = read("明日の3時から5時", NOW, company)
        self.assertEqual((r.start, r.end), (at(2026, 10, 15, 15), at(2026, 10, 15, 17)))
        night = (18 * H, 3 * H)                                                       # 夜のお店（日をまたぐ）
        self.assertEqual(read("明日の1時", NOW, night).start, at(2026, 10, 15, 1))
        self.assertEqual(read("明日の3時", NOW, night).kind, CONFIRM)                 # 3時は終わりちょうど
        self.assertEqual(read("9時", NOW, night).start, at(2026, 10, 14, 21))
        for hours in ((0, 23 * H), (13 * H, 14 * H)):                                 # 両方入る・どちらも入らない ⇒ 確かめる
            with self.subTest(hours=hours):
                self.assertEqual(read("明日の3時", NOW, hours).kind, CONFIRM)


class SaidEdges(unittest.TestCase):
    """幅の端のうち、お客様が実際に言った端（Reading.said）と、その日を言ったか（Reading.day_said）。依頼者の時間帯の外でもかけて
    よいのは、言った端だけ（calling_hours.callee_hours。見直し15：前は「朝なら」で0時、「午後なら」で23時にかけえた）。"""

    def test_which_edges_were_said(self):
        cases = {"4時以降": (True, False), "3時まで": (False, True), "明日の3時から5時": (True, True), "3時台": (True, True),
                 "夕方": (False, False), "朝": (False, False), "午後": (False, False), "夕方から": (False, False),
                 "明日の午前中": (False, False), "明日ならいつでも": (False, False), "すぐ": (False, False)}
        for text, said in cases.items():
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).said, said)

    def test_whether_the_day_was_said(self):
        """日を言わない「夕方なら」「3時なら」「すぐ」も、いま話している今日の回は言った日（見直し16）。次の日からの回は、
        言っていない（next_action の Window.later）。"""
        for text, said in {"夕方なら": True, "3時なら": True, "すぐ": True, "30分後": True, "明日の夕方": True,
                           "金曜なら": True, "13日なら": True, "平日の夕方": True, "土日祝ならいつでも": True,
                           "明日の15時": True, "今晩": True}.items():
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).day_said, said)


    def test_a_said_edge_beyond_the_designated_limit_is_confirmed(self):
        """お客様が口にした端が「お客様指定の歯止め時間」（初め 7〜22時）の外で、反対の端を歯止めで埋めると幅が空になる言い方は、
        通話の中で確かめる（判断は calling_hours.callee_range の1か所。⏳ どう埋めるかはユーザーに伺うまでの形。見直し16b：前は空の
        幅が時刻の決まっていない約束になり、お客様の言ったことと反対の時刻＝依頼者のいつもの時間にかけた）。口にした時刻そのもの
        （「明日の朝6時」）や、両端を口にした幅は、歯止めの外でもそのまま（2026-10-22・10-23 ユーザー）。"""
        for text in ("今日の23時以降", "明日の朝6時まで", "明日の朝6時半まで"):
            with self.subTest(text=text):
                self.assertEqual(read(text, NOW).kind, CONFIRM)
        self.assertEqual(read("明日の朝6時", NOW).kind, AT)
        self.assertEqual(read("今日の21時以降", NOW).kind, WINDOW)
        self.assertEqual(read("今日の23時から23時半", NOW).kind, WINDOW)
        self.assertEqual(read("今日の21時以降", NOW, limit=(8 * H, 21 * H)).kind, CONFIRM)   # 歯止め時間は業務ごと


class Repeat(unittest.TestCase):
    """幅の中で話せなかったときの次の回は、お客様がその日を選んだ理由に合わせる（2026-10-19 ユーザー決定）。"""

    def test_the_reason_decides_the_next_round(self):
        cases = {   # 通話は 2026-10-14（水）10:00
            "金曜なら": ("week", at(2026, 10, 16), (at(2026, 10, 23), at(2026, 10, 24))),
            "金曜日の夕方": ("week", at(2026, 10, 16, 16), (at(2026, 10, 23, 16), at(2026, 10, 23, 18))),
            "週末なら": ("week", at(2026, 10, 17), (at(2026, 10, 24), at(2026, 10, 26))),
            "週中": ("week", at(2026, 10, 13), (at(2026, 10, 20), at(2026, 10, 23))),   # 水曜に言われても、次の回は火曜から
            "週明け": ("week", at(2026, 10, 19), (at(2026, 10, 26), at(2026, 10, 27))),
            "明日の夕方": ("day", at(2026, 10, 15, 16), (at(2026, 10, 16), at(2026, 10, 17))),   # 1日の中の幅は予定が絞る
            "3時以降": ("day", at(2026, 10, 14, 15), (at(2026, 10, 15), at(2026, 10, 16))),
        }
        for text, (repeat, since, nxt) in cases.items():
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.repeat, r.since), (repeat, since))
                self.assertEqual(next_occurrence(r.repeat, r.since, r.end), nxt)
        for text in ("13日なら", "明日なら", "3日後", "来週", "月末なら", "11月なら"):   # 理由の分からない日
            with self.subTest(text=text):
                self.assertIsNone(read(text, NOW).repeat)
        self.assertEqual(read("平日ならいつでも", NOW).repeat, "day")                  # 日の種類だけ：かけられる日の1日が1回
        self.assertEqual(read("すぐ", NOW).repeat, "day")

    def test_month_rounds_only_with_a_monthly_reason(self):
        """月の中の日・頃は、たまたまのことが多い。月の理由（月ぎめの業務・入金の間隔など。会話の報告の cycle＝month）が
        あるときだけ、次の月の同じ日・頃（ユーザー）。"""
        r = read("月末なら", NOW, monthly=True)
        self.assertEqual(r.repeat, "month:part:月末")
        self.assertEqual(next_occurrence(r.repeat, r.since, r.end), (at(2026, 11, 28), at(2026, 12, 1)))
        r = read("31日なら", at(2026, 10, 14, 10), monthly=True)
        self.assertEqual(next_occurrence(r.repeat, r.since, r.end), (at(2026, 11, 30), at(2026, 12, 1)))   # その月の終わりの日
        r = read("11月なら", NOW, monthly=True)
        self.assertEqual(next_occurrence(r.repeat, r.since, r.end), (at(2026, 12, 1), at(2027, 1, 1)))


class PeriodsBySetting(unittest.TestCase):
    def test_periods_come_from_the_client_setting(self):
        """言葉ごとの幅は依頼者ごとの設定（0088）。設定を変えれば読み方が変わる。終わりが始まり以前なら、その日の終わりまで。"""
        from salescall.core.spoken_time import DEFAULT_PERIODS, PERIODS, periods_from, range_seconds
        self.assertEqual(periods_from(DEFAULT_PERIODS), PERIODS)
        self.assertEqual(range_seconds(["13:00", "00:00"]), (13 * H, DAY))
        self.assertEqual(range_seconds(["18:00", "02:00"]), (18 * H, DAY))
        shop = periods_from({**DEFAULT_PERIODS, "evening": range_seconds(["17:00", "19:00"]),
                             "afternoon": range_seconds(["14:00", "00:00"])})
        r = read("明日の夕方", NOW, periods=shop)
        self.assertEqual((r.start, r.end), (at(2026, 10, 15, 17), at(2026, 10, 15, 19)))
        r = read("明日の午後一", NOW, periods=shop)
        self.assertEqual((r.start, r.end), (at(2026, 10, 15, 14), at(2026, 10, 15, 15)))


class NotGuessed(unittest.TestCase):
    def test_needs_confirming(self):
        """言い方は分かるが、いつかが1つに決まらない ⇒ 通話の中で確かめる（推し量らない）。"""
        for text in ("昼", "お昼", "昼過ぎ", "お昼過ぎ", "明日のお昼", "明日の昼1時",   # 2026-10-19 ユーザー：昼は確かめる
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
        for text in ("明日か明後日の午後3時", "来月の午後3時ごろの予定です", "折り返し", "そのへんで"):
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertIn(r.kind, (UNREADABLE, CONFIRM))
                self.assertIsNone(r.start)
        self.assertEqual(read("そのへんで", NOW).kind, UNREADABLE)

    def test_separators_and_kanji_runs_are_not_joined(self):
        """区切り（・ , .）を消して数をつなげない・十の無い漢数字をつなげない（⚠️ 前は「1・2時間後」を12時間後、「二三日後」を
        23日後と読んだ＝2026-10-19 の見直し）。続いた2つの数は範囲（二三日後＝2〜3日後）。区切りの残る時刻は読まない。"""
        two_three = (WINDOW, at(2026, 10, 16), at(2026, 10, 18))
        for text in ("二三日後", "2・3日後", "2、3日後", "2,3日後"):
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertEqual((r.kind, r.start, r.end), two_three)
        for text in ("1・2時", "明日の1.2時", "1,2時間後", "明日の一二時", "二四日後", "十十日", "二十十日"):   # 読めない書き方で落ちない
            with self.subTest(text=text):
                r = read(text, NOW)
                self.assertIn(r.kind, (UNREADABLE, CONFIRM))
                self.assertIsNone(r.start)
        self.assertEqual(read("十二日", NOW).start, at(2026, 11, 12))           # 十のある漢数字はこれまでどおり
        self.assertEqual(read("二〇二七年一月五日なら", NOW), read("2027年1月5日なら", NOW))   # 〇を含む位取り
        self.assertEqual(read("2027年1月5日なら", NOW).start, at(2027, 1, 5))

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
