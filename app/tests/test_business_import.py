"""業務設定の取込（引き継ぎの (10)。core/business_import.py）の試験。架空の業務。"""

from __future__ import annotations

import io
import unittest

import openpyxl

from salescall.core import business_import as bi
from salescall.core import business_settings as bs
from tests.support import OPERATOR, World

REASONS = ("担当ではない", "まだ決まっていない", "いまは分からない", "理由を言わなかった")


def book(*, sheets=None, drop=(), rename=None) -> bytes:
    """標準のひな形どおりの、架空の業務設定のファイル。sheets で中身を差し替え、drop でシートを外す。"""
    content = {
        "基本": [("項目", "値"), ("業務の目的", "予約の希望日時を伺う"), ("自社名", "株式会社テスト物流"),
               ("自社名の読み", "かぶしきがいしゃてすとぶつりゅう"), ("担当者名", "山田"), ("担当者名の読み", "やまだ"),
               ("用件の一文", "ご予約の確認でお電話いたしました。"), ("目的の人", "誰でも"), ("目的の人の役職", None),
               ("忙しいと言われたときの一言", None), ("声の型", "S2")],
        "質問": [("質問ID", "質問の文", "聞くこと", "型", "必須", "選択肢", "断りの答え"),
               ("Q1", "「テスト便」のご予約はなさいますか。", "ご予約", "選択肢", "はい", "する／しない", "しない"),
               ("Q2", "ご希望の日時をお聞かせください。", "ご希望の日時", "日時", "はい", None, None)],
        "分岐": [("質問ID", "回答条件", "次の質問ID"), ("Q1", "しない", "終わり"), ("Q1", "その他", "Q2")],
        "わからない・答えたくない": [("質問ID", "答え", "理由", "扱い", "結論の名前")]
        + [("全部", "わからない", r, "日を改める", None) for r in REASONS]
        + [("全部", "答えたくない", r, "結論として終える", "回答不可") for r in ("決まりで言えない", "答えたくない")],
        "かけ直し": [("結果", "かけ直すか", "回数", "間隔")]
        + [(k, "かける", "3", "2時間おき") for k in ("会話に至らなかった", "一部だけ取れた", "不在", "取り次いでもらえなかった")]
        + [("かけ直しの時刻が決まっていない", "かける", "2", "1日後")],
        "言ってはいけないこと": [("名前", "言ってはいけないこと", "正しい案内"), ("F1", "初月は無料です", "初月は半額です")],
        "名前": [("表記", "読み"), ("テスト便", "てすとびん")],
    }
    content.update(sheets or {})
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in content.items():
        if name in drop:
            continue
        ws = wb.create_sheet((rename or {}).get(name, name))
        for r in rows:
            ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ALL_NAMES = [("株式会社テスト物流", "かぶしきがいしゃてすとぶつりゅう"), ("山田", "やまだ"), ("テスト便", "てすとびん")]


class Import(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def tx(self, work):
        return self.w.a.in_client(OPERATOR, self.s["tenant"], self.s["client"], work)

    def preview(self, data, **kw):
        return self.tx(lambda tx: bi.preview(tx, "業務設定.xlsx", data, **kw))

    def test_preview_confirm_names_and_register(self):
        data = book()
        p = self.preview(data)
        self.assertEqual([n["text"] for n in p.names_to_confirm], [t for t, _ in ALL_NAMES])   # 人が読みを確かめる
        self.assertTrue(all("読みを、まだ人が確かめていません" in x for x in p.problems), p.problems)
        p = self.preview(data, confirmed_names=ALL_NAMES)
        self.assertEqual((p.problems, p.names_to_confirm), ([], []))
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                           confirmed_names=ALL_NAMES))
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual((got.next_of("Q1", "しない"), got.rules.decline["Q1"], got.rules.retry["callback_unspecified_time"].interval),
                         ("END", frozenset({"しない"}), ("days", 1)))
        self.assertEqual(got.rules.handling[("Q2", "refused", "policy")].conclusion, "回答不可")   # 「全部」の行が効く

    def test_register_refuses_a_changed_preview(self):
        p = self.preview(book(), confirmed_names=ALL_NAMES)
        other = book(sheets={"基本": [("項目", "値"), ("業務の目的", "目的を書き換えた"), ("自社名", "株式会社テスト物流"),
                                     ("自社名の読み", "かぶしきがいしゃてすとぶつりゅう"), ("担当者名", "山田"),
                                     ("担当者名の読み", "やまだ"), ("目的の人", "誰でも"), ("声の型", "S2")]})
        with self.assertRaises(bi.PreviewChanged):
            self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", other, expected_digest=p.digest,
                                           confirmed_names=ALL_NAMES))

    def test_problems_name_the_sheet_and_row(self):
        bad = book(sheets={"質問": [("質問ID", "質問の文", "聞くこと", "型", "必須", "選択肢", "断りの答え"),
                                   ("Q1", "ご予約はなさいますか。", None, "色", "はい", None, None)]}, drop=("分岐",))
        p = self.preview(bad, confirmed_names=ALL_NAMES)
        self.assertTrue(any("シート「質問」の 2 行目の型" in x for x in p.problems), p.problems)

    def test_missing_required_sheet_and_mapping(self):
        p = self.preview(book(drop=("かけ直し",)), confirmed_names=ALL_NAMES)
        self.assertTrue(any("かけ直し" in x for x in p.problems), p.problems)
        renamed = book(rename={"質問": "質問一覧"})
        p = self.preview(renamed, confirmed_names=ALL_NAMES, mapping={"sheets": {"questions": "質問一覧"}})
        self.assertEqual(p.problems, [])                                  # 依頼主の形を対応付けて読む

    def test_words_in_quotes_that_may_be_names(self):
        p = self.preview(book(sheets={"名前": [("表記", "読み")]}), confirmed_names=ALL_NAMES)
        self.assertEqual(p.maybe_names, ["テスト便"])                    # 「」の中の、名前の一覧に無い言葉

    def test_missing_reading_uses_the_ai_suggestion_and_a_person_confirms(self):
        """読みが無ければ AI の読みの案を出し、人が確かめる（直してもよい）。2026-10-16 ユーザー：「検証中はAIが認識したものを
        私が確認します」。"""
        data = book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]})
        asked = []

        def ai(texts):
            asked.extend(texts)
            return {"テスト便": "てすとべん"}                            # AI の案（誤り）
        p = self.preview(data, suggest_readings=ai)
        self.assertEqual(asked, ["テスト便"])
        self.assertIn({"text": "テスト便", "reading": "てすとべん", "source": "script", "by": "AI の読みの案"},
                      p.names_to_confirm)
        fixed = ALL_NAMES                                                 # 人が「てすとびん」に直して確かめた
        p = self.preview(data, suggest_readings=ai, confirmed_names=fixed)
        self.assertEqual(p.problems, [])
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                           confirmed_names=fixed, suggest_readings=ai))
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertIn(bs.Name("テスト便", "てすとびん", "script"), got.names)

    def test_no_reading_and_no_suggestion_is_a_problem(self):
        p = self.preview(book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]}), confirmed_names=ALL_NAMES[:2])
        self.assertTrue(any("名前「テスト便」の読みがありません" in x for x in p.problems), p.problems)

    def test_other_entrances_go_the_same_way(self):
        """依頼主の画面から抜き出した中身（同じ形）も、同じ確かめと登録の道を通る。"""
        from tests.test_business_settings import raw
        extracted = raw()
        for n in extracted["names"]:
            n["confirmed"] = False                                        # 抜き出した側は確かめを持たない
        p = self.tx(lambda tx: bi.review(tx, extracted))
        self.assertTrue(p.problems)                                       # 人が読みを確かめるまで登録できない
        ok = [("テスト物流", "てすとぶつりゅう"), ("株式会社テスト物流", "かぶしきがいしゃてすとぶつりゅう")]
        p = self.tx(lambda tx: bi.review(tx, extracted, confirmed_names=ok))
        self.assertEqual(p.problems, [])
        v = self.tx(lambda tx: bi.register_reviewed(tx, self.s["business"], p, expected_digest=p.digest))
        self.assertEqual(self.tx(lambda tx: bs.load(tx, v)).company.text, "株式会社テスト物流")

    def test_nothing_is_written_on_problems(self):
        bad = book(drop=("かけ直し",))
        p = self.preview(bad, confirmed_names=ALL_NAMES)
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", bad, expected_digest=p.digest,
                                           confirmed_names=ALL_NAMES))


if __name__ == "__main__":
    unittest.main()
