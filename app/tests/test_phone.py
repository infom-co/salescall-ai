"""番号の正規化の試験（Q1）。

⚠️ 試験の番号は、局番のあとが「0000」で始まるもの（日本では、市外局番・携帯の頭のあとの番号は 0 で始まらないので、
実在しない番号）だけを使う。実在の会社・人の番号は使わない。
"""

from __future__ import annotations

import unittest

from salescall.core.phone import normalize


class SameNumberDifferentWritings(unittest.TestCase):
    def test_writings_of_one_fixed_number(self):
        for raw in ("03-0000-0001", "0300000001", "03(0000)0001", "(03)0000-0001", "０３－００００－０００１",
                    "03 0000 0001", "03ー0000ー0001", "+81-3-0000-0001", "+81 (0)3 0000 0001", "＋８１３０００００００１"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual((n.kind, n.e164, n.national), ("fixed", "+81300000001", "0300000001"))
                self.assertFalse(n.system_forbidden)

    def test_kinds(self):
        for raw, kind, e164 in [
            ("090-0000-0001", "mobile", "+819000000001"),
            ("070-0000-0001", "mobile", "+817000000001"), ("050-0000-0001", "ip", "+815000000001"),
            ("0120-000-001", "toll_free", "+81120000001"), ("0800-000-0001", "toll_free", "+818000000001"),
            ("0570-000-001", "navi_dial", "+81570000001"), ("060-0000-0001", "fmc", "+816000000001"),
            ("011-000-0001", "fixed", "+81110000001"), ("0123-00-0001", "fixed", "+81123000001"),
            # 「080-0000-0001」と書いても、数字の並びは 0800 のフリーダイヤルと同じ（実在の 080 の携帯は「080-0」で始まらない）。
            # 区切りの位置では決めない（番号は数字の並びだけで決まる）。
            ("080-0000-0001", "toll_free", "+818000000001"),
        ]:
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual((n.kind, n.e164), (kind, e164))

    def test_system_forbidden_kinds(self):
        for raw, kind in [("110", "special"), ("119", "special"), ("104", "special"), ("#7119", "special"),
                          ("0990-000-001", "premium"), ("0180-000-001", "premium"), ("020-0000-0001", "m2m"),
                          ("+1-202-000-0001", "international"), ("010-1-202-000-0001", "international"),
                          ("001-1-202-000-0001", "unknown"), ("0033-03-0000-0001", "unknown")]:
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual(n.kind, kind)
                self.assertTrue(n.system_forbidden)
                self.assertTrue(n.reason)


class UnknownIsNotGuessed(unittest.TestCase):
    """そろえられない番号は「不明」。推測で直さない（原則10）。"""

    def test_unknowns(self):
        for raw in (None, "", "   ", "03-0000-001", "090-0000-00011", "0120-000-0001", "81300000001",
                    "03-0000-0001 内線12", "03-0000-0001/090-0000-0001", "03-0000-0001、03-0000-0002",
                    "TEL:03-0000-0001", "0000000001", "3-0000-0001"):
            with self.subTest(raw=raw):
                n = normalize(raw)
                self.assertEqual((n.kind, n.e164), ("unknown", None))
                self.assertTrue(n.reason)
                self.assertTrue(n.system_forbidden)


if __name__ == "__main__":
    unittest.main()
