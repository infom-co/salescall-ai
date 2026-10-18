"""番号の正規化の試験（Q1）。書き方のゆれを、種類ごとに確かめる（2026-10-13 ユーザー：「書き方のゆれに漏れがないか再確認」）。

⚠️ 試験の番号は、局番のあとが「0000」で始まるもの（日本では、市外局番・携帯の頭のあとの番号は 0 で始まらないので、
実在しない番号）だけを使う。実在の会社・お客様の番号は使わない。
"""

from __future__ import annotations

import itertools
import random
import unittest

from salescall.core import phone
from salescall.core.phone import PhoneNumber, normalize

FIXED = ("fixed", "+81300000001", "0300000001")
MOBILE = ("mobile", "+819000000001", "09000000001")


def got(raw, **kw):
    n = normalize(raw, **kw)
    return (n.kind, n.e164, n.national)


class Separators(unittest.TestCase):
    """区切りの文字（文字の種類で取り除く）。"""

    def test_each_kind_of_separator(self):
        seps = {
            "ハイフン": "-", "全角ハイフン": "－", "ハイフン(U+2010)": "‐", "改行しないハイフン": "‑",
            "数字幅ダッシュ": "‒", "enダッシュ": "–", "emダッシュ": "—", "横線": "―",
            "マイナス記号": "−", "全角マイナス": "－", "小さいハイフン": "﹣", "長音": "ー", "半角長音": "ｰ",
            "罫線": "─", "太い罫線": "━", "中黒": "・", "半角中黒": "･", "点": ".", "全角点": "．",
            "空白": " ", "全角空白": "　", "ノーブレーク空白": " ", "タブ": "\t", "改行": "\n",
            "幅ゼロの空白": "​", "ソフトハイフン": "­", "BOM": "﻿",
        }
        for name, sep in seps.items():
            with self.subTest(name=name):
                self.assertEqual(got(f"03{sep}0000{sep}0001"), FIXED)
                self.assertEqual(got(f"090{sep}0000{sep}0001"), MOBILE)

    def test_parentheses(self):
        for raw in ("03(0000)0001", "(03)0000-0001", "（03）0000-0001", "03（0000）0001", "+81 (0)3 0000 0001",
                    "+81(0)3-0000-0001", "(+81)3-0000-0001"):
            with self.subTest(raw=raw):
                self.assertEqual(got(raw), FIXED)

    def test_fullwidth_and_mixed(self):
        for raw in ("０３－００００－０００１", "０３-0000-０００１", "＋８１３０００００００１", "+81-3-0000-0001",
                    " 03-0000-0001 ", "​03-0000-0001​"):
            with self.subTest(raw=raw):
                self.assertEqual(got(raw), FIXED)

    def test_generated_combinations(self):
        """区切りを、数字のあいだのどこに・どれだけ入れても、同じ番号になる（決まった種で作る）。"""
        rnd = random.Random(20261013)
        seps = ["-", "－", "‐", "ー", "─", " ", "　", "​", ".", "・", "−"]
        for digits, expected in (("0300000001", FIXED), ("09000000001", MOBILE)):
            for _ in range(300):
                s = "".join(d + "".join(rnd.choice(seps) for _ in range(rnd.choice((0, 0, 1, 2)))) for d in digits)
                if rnd.random() < 0.5:
                    s = s.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
                with self.subTest(raw=repr(s)):
                    self.assertEqual(got(s), expected)


class SpreadsheetArtifacts(unittest.TestCase):
    """表計算ソフトから来る形。"""

    def test_text_markers(self):
        for raw in ("'0300000001", "’03-0000-0001", '"03-0000-0001"', '="0300000001"', '= "03-0000-0001"'):
            with self.subTest(raw=raw):
                self.assertEqual(got(raw), FIXED)

    def test_leading_zero_lost_is_restored_only_when_the_importer_says_so(self):
        self.assertEqual(got("300000001")[0], "unknown")                                # 1つの値だけでは決めない
        self.assertEqual(got("300000001", leading_zero_lost=True), FIXED)
        self.assertEqual(got("9000000001", leading_zero_lost=True), MOBILE)
        self.assertEqual(got("30000000011", leading_zero_lost=True)[0], "unknown")       # 桁が合わなければ足さない
        self.assertEqual(got("0300000001", leading_zero_lost=True), FIXED)               # もともと 0 があれば、そのまま

    def test_number_shapes_that_lost_digits(self):
        for raw in ("300000001.0", "9000000001.00", "3.00000E+09", "3e9", "9.00000E+9"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual(n.kind, "unknown")
                self.assertIn("数", n.reason)


class LabelsAndNotes(unittest.TestCase):
    """電話を表す頭の言葉・数字を含まない注記は取り除く。FAX・内線は止める。"""

    def test_labels(self):
        for raw in ("TEL:03-0000-0001", "TEL：03-0000-0001", "Tel. 03-0000-0001", "tel 03-0000-0001", "℡03-0000-0001",
                    "☎03-0000-0001", "電話：03-0000-0001", "電話番号 03-0000-0001", "代表：03-0000-0001",
                    "PHONE: 03-0000-0001", "【TEL】03-0000-0001"):
            with self.subTest(raw=raw):
                self.assertEqual(got(raw), FIXED)
        self.assertEqual(got("携帯：090-0000-0001"), MOBILE)

    def test_notes_without_digits(self):
        for raw in ("03-0000-0001（代表）", "03-0000-0001 (代表)", "03-0000-0001【本社】", "(本社)03-0000-0001"):
            with self.subTest(raw=raw):
                self.assertEqual(got(raw), FIXED)

    def test_fax_is_never_a_phone(self):
        for raw in ("FAX:03-0000-0001", "TEL/FAX 03-0000-0001", "03-0000-0001（FAX）", "ファックス 03-0000-0001",
                    "ﾌｧｯｸｽ 03-0000-0001", "Fax 03-0000-0001"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual(n.kind, "unknown")
                self.assertIn("FAX", n.reason)

    def test_extensions(self):
        for raw in ("03-0000-0001 内線12", "03-0000-0001（内線12）", "03-0000-0001 ext.12", "03-0000-0001#12",
                    "03-0000-0001 Ext 12"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual(n.kind, "unknown")
                self.assertIn("内線", n.reason)


class KindsAndSpecials(unittest.TestCase):
    def test_kinds(self):
        for raw, kind, e164 in [
            ("070-0000-0001", "mobile", "+817000000001"), ("050-0000-0001", "ip", "+815000000001"),
            ("0120-000-001", "toll_free", "+81120000001"), ("0800-000-0001", "toll_free", "+818000000001"),
            ("0570-000-001", "navi_dial", "+81570000001"), ("060-0000-0001", "fmc", "+816000000001"),
            ("011-000-0001", "fixed", "+81110000001"), ("0123-00-0001", "fixed", "+81123000001"),
            ("04-0000-0001", "fixed", "+81400000001"),
            # 「080-0000-0001」と書いても、数字の並びは 0800 のフリーダイヤルと同じ（実在の 080 の携帯は「080-0」で始まらない）。
            # 区切りの位置では決めない（番号は数字の並びだけで決まる）。
            ("080-0000-0001", "toll_free", "+818000000001"),
        ]:
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual((n.kind, n.e164), (kind, e164))

    def test_system_forbidden_kinds(self):
        for raw, kind in [("110", "special"), ("119", "special"), ("１１９", "special"), ("104", "special"),
                          ("#7119", "special"), ("＃７１１９", "special"),
                          ("0990-000-001", "premium"), ("0180-000-001", "premium"), ("020-0000-0001", "m2m"),
                          ("+1-202-000-0001", "international"), ("010-1-202-000-0001", "international"),
                          ("001-1-202-000-0001", "unknown"), ("0033-03-0000-0001", "unknown")]:
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual(n.kind, kind)
                self.assertTrue(n.system_forbidden)
                self.assertTrue(n.reason)

    def test_184_186_prefix(self):
        for raw in ("184-03-0000-0001", "186 090 0000 0001"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual(n.kind, "unknown")
                self.assertIn("184", n.reason)


class ForbiddenAlwaysHasAReason(unittest.TestCase):
    """システムが決してかけない種類には、必ず理由の文がある（取込のプレビュー・門の理由でそのまま見せる。CL-3 の 4-1f の2）。"""
    def test_every_forbidden_result_has_a_reason(self):
        for raw in ("#9110", "110", "119", "+1 212 555 0100", "010-1-212-555-0100", "0990-123-456", "020-1234-5678",
                    "abc", "", "03-1234", "0120-12-345"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                if n.system_forbidden:
                    self.assertTrue(n.reason and n.reason.strip(), n)

    def test_a_forbidden_kind_without_a_reason_cannot_be_made(self):
        with self.assertRaises(ValueError):
            PhoneNumber("special", None, None, None)


class UnknownIsNotGuessed(unittest.TestCase):
    """そろえられない番号は「不明」と理由。推測で直さない（原則10）。"""

    def test_unknowns(self):
        for raw in (None, "", "   ", "-", "－", "なし", "不明", "03-0000-001", "090-0000-00011", "0120-000-0001",
                    "81300000001", "81-3-0000-0001", "03-0000-0001/090-0000-0001", "03-0000-0001、03-0000-0002",
                    "03-0000-0001 or 03-0000-0002", "03-0000-0001～3", "03-0000-0001〜0003",
                    "03-0000-0001\n090-0000-0001", "0000000001", "3-0000-0001", "0000-0001", "03-0000-0001 山田",
                    "「03-0000-0001」"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual((n.kind, n.e164), ("unknown", None))
                self.assertTrue(n.reason)
                self.assertTrue(n.system_forbidden)

    def test_two_numbers_never_become_one(self):
        """2つの番号を、どの区切りで並べても、1つの番号としてはそろわない。"""
        for a, b in itertools.product(("03-0000-0001", "090-0000-0001", "0120-000-001"), repeat=2):
            for sep in ("", " ", "\n", "・", "-", "　", "　/　", "、", "　"):
                with self.subTest(a=a, b=b, sep=repr(sep)):
                    self.assertEqual(normalize(a + sep + b).kind, "unknown")


class Spoken(unittest.TestCase):
    """お客様が電話で言った番号（会話の報告。CL-1 の連絡帳 4-22「番号は話し言葉のまま」）。"""

    def test_digit_words_and_pauses(self):
        for raw in ("ゼロサン、ゼロゼロゼロゼロのゼロゼロゼロイチ", "〇三の〇〇〇〇の〇〇〇一", "ぜろさん ぜろぜろぜろぜろ ぜろぜろぜろいち",
                    "まるさんのまるまるまるまるのまるまるまるいち", "れいさん0000-0001", "03の0000の0001", "03、0000、0001"):
            with self.subTest(raw=raw):
                self.assertEqual(got(raw, spoken=True), FIXED)
        self.assertEqual(got("ゼロキュウゼロのゼロゼロゼロゼロのゼロゼロゼロイチ", spoken=True), MOBILE)
        n = normalize("ゼロキュウゼロのイチニーサンヨンのゴーロクナナハチ", spoken=True)
        self.assertEqual(n.national, "09012345678")
        n = normalize("ゼロロクのシチシチシチシチのキューキュークク", spoken=True)   # 「しち」を「し」＋「ち」にしない・「く」と「きゅー」
        self.assertEqual(n.national, "0677779999")
        self.assertEqual(normalize("にいち", spoken=True).reason, normalize("21").reason)   # 「に」＋「いち」（「にい」＋「ち」にしない）

    def test_only_digit_words_are_read(self):
        """位の言葉・ほかの言葉は直さない（残れば不明）。FAX は FAX。ふつうの読み（spoken でない）は、話し言葉を読まない。"""
        for raw in ("ゼロサンの十二", "やまださんの番号", "ゼロサンのいちまん", "ファックスはゼロサンゼロゼロゼロゼロゼロゼロゼロイチ"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw, spoken=True).kind, "unknown")
        self.assertIn("FAX", normalize("ファックスはゼロサンゼロゼロゼロゼロゼロゼロゼロイチ", spoken=True).reason)
        self.assertEqual(normalize("ゼロサンゼロゼロゼロゼロゼロゼロゼロイチ").kind, "unknown")
        self.assertEqual(normalize("03、0000、0001").kind, "unknown")

    def test_two_spoken_numbers_never_become_one(self):
        for a, b in itertools.product(("ゼロサンのゼロゼロゼロゼロのゼロゼロゼロイチ", "090の0000の0001"), repeat=2):
            for sep in ("", "、", "の", " "):
                with self.subTest(a=a, b=b, sep=sep):
                    self.assertEqual(normalize(a + sep + b, spoken=True).kind, "unknown")

    def test_longest_first_never_misreads(self):
        """ある読みが別の読みの頭になっているとき、残りの字で始まる読みは無い（長いほうを先に当てても読み誤らない）。"""
        words = list(phone._SPOKEN_DIGITS)
        for short, long in itertools.permutations(words, 2):
            if long.startswith(short):
                rest = long[len(short):]
                with self.subTest(short=short, long=long):
                    self.assertFalse(any(w.startswith(rest[0]) for w in words))


if __name__ == "__main__":
    unittest.main()
