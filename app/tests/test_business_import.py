"""業務設定の取込（引き継ぎの (10)。core/business_import.py）の試験。架空の業務。"""

from __future__ import annotations

import io
import unittest

import openpyxl

from salescall.core import business_import as bi
from salescall.core import business_settings as bs
from salescall.core import settings
from tests.support import OPERATOR, World

REASONS = ("担当ではない", "まだ決まっていない", "いまは分からない", "理由を言わなかった")


def book(*, sheets=None, drop=(), rename=None) -> bytes:
    """標準のひな形どおりの、架空の業務設定のファイル。sheets で中身を差し替え、drop でシートを外す。"""
    content = {
        "基本": [("項目", "値"), ("業務の目的", "予約の希望日時を伺う"), ("お客様の区分", "法人"), ("自社名", "株式会社テスト物流"),
               ("自社名の読み", "かぶしきがいしゃてすとぶつりゅう"), ("担当者名", "山田"), ("担当者名の読み", "やまだ"),
               ("用件の一文", "ご予約の確認でお電話いたしました。"), ("目的のお客様", "誰でも"), ("目的のお客様の役職", None),
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


def book_rows(sheet: str) -> list[tuple]:
    """標準のひな形の、そのシートの行（試験で書き換えるため）。"""
    wb = openpyxl.load_workbook(io.BytesIO(book()))
    return [tuple(c.value for c in r) for r in wb[sheet].iter_rows()]


def finder(*items: dict):
    """名前を洗い出す処理の代わり（CL-1 の names.decide の戻り値の形を返す。聞かれた台本を残す）。"""
    def find(text: str) -> list[dict]:
        find.asked.append(text)
        return [dict(x) for x in items]
    find.asked = []
    return find


class Import(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def tx(self, work):
        return self.w.a.in_client(OPERATOR, self.s["tenant"], self.s["client"], work)

    def preview(self, data, **kw):
        return self.tx(lambda tx: bi.preview(tx, self.s["business"], "業務設定.xlsx", data, **kw))

    def start_verification(self):
        """検証モードの業務（AI の読みの案を契約者が確かめる。切り替えは契約者の承認と同じ1つ）。"""
        self.tx(lambda tx: settings.set_value(tx, "approval.human", True, scope="business", status="decided",
                                              rationale="試験：検証モード", review_when="試験",
                                              tenant_id=self.s["tenant"], client_id=self.s["client"],
                                              business_id=self.s["business"]))

    def test_preview_and_register(self):
        """入口が持ってきた読みは、契約者が確かめずに使う（管理者も契約者も介在しない。確かめるのは検証モードの AI の案だけ）。"""
        data = book()
        p = self.preview(data)
        self.assertEqual((p.problems, p.names_to_confirm), ([], []))
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest))
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual((got.next_of("Q1", "しない"), got.rules.decline["Q1"], got.rules.retry["callback_unspecified_time"].interval),
                         ("END", frozenset({"しない"}), ("days", 1)))
        self.assertEqual(got.rules.handling[("Q2", "refused", "policy")].conclusion, "回答不可")   # 「全部」の行が効く
        self.assertIn(bs.Name("テスト便", "てすとびん", "script", "given"), got.names)

    def test_register_refuses_a_changed_preview(self):
        p = self.preview(book())
        other = book(sheets={"基本": [("項目", "値"), ("業務の目的", "目的を書き換えた"), ("お客様の区分", "法人"), ("自社名", "株式会社テスト物流"),
                                     ("自社名の読み", "かぶしきがいしゃてすとぶつりゅう"), ("担当者名", "山田"),
                                     ("担当者名の読み", "やまだ"), ("目的のお客様", "誰でも"), ("声の型", "S2")]})
        with self.assertRaises(bi.PreviewChanged):
            self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", other, expected_digest=p.digest))

    def test_problems_name_the_sheet_and_row(self):
        bad = book(sheets={"質問": [("質問ID", "質問の文", "聞くこと", "型", "必須", "選択肢", "断りの答え"),
                                   ("Q1", "ご予約はなさいますか。", None, "色", "はい", None, None)]}, drop=("分岐",))
        p = self.preview(bad)
        self.assertTrue(any("シート「質問」の 2 行目の型" in x for x in p.problems), p.problems)

    def test_missing_required_sheet_and_mapping(self):
        p = self.preview(book(drop=("かけ直し",)))
        self.assertTrue(any("かけ直し" in x for x in p.problems), p.problems)
        renamed = book(rename={"質問": "質問一覧"})
        p = self.preview(renamed, mapping={"sheets": {"questions": "質問一覧"}})
        self.assertEqual(p.problems, [])                                  # 依頼者の形を対応付けて読む

    def test_names_found_in_the_script_are_added(self):
        """台本の名前は、名前を洗い出す処理（CL-1 の names.decide）が見つける。入口の名前に無いものも、名前の一覧に入る。"""
        found = finder({"surface": "テスト便", "reading": "てすとびん", "source": "モデル2つ", "candidates": []},
                       {"surface": "中島倉庫", "reading": "", "source": "", "candidates": ["なかじまそうこ", "なかしまそうこ"],
                        "likely": "なかじまそうこ"})
        p = self.preview(book(sheets={"名前": [("表記", "読み")]}), find_names=found)
        self.assertEqual(p.problems, [])
        self.assertIn("テスト便", found.asked[0])                      # 台本（質問の文など）をまとめて1回
        got = {n["text"]: n for n in p.raw["names"]}
        self.assertEqual((got["テスト便"]["reading"], got["テスト便"]["reading_by"]), ("てすとびん", "ai"))
        self.assertEqual((got["中島倉庫"]["reading"], got["中島倉庫"]["likely"]), (None, "なかじまそうこ"))

    def test_an_undecided_reading_does_not_stop_the_import(self):
        """読みが決まらない名前でも止めない。考えられる読みとまず言ってみる読みを持って登録し、会話の中で確かめる（2026-10-16
        ユーザー：「なかじまとなかしまなどは人が見ても判断できません」）。検証モードでも、契約者の確かめの対象にしない。"""
        self.start_verification()
        data = book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]})
        found = finder({"surface": "テスト便", "reading": "", "source": "", "candidates": ["てすとびん", "てすとべん"],
                        "likely": "てすとびん"})
        p = self.preview(data, find_names=found)
        self.assertEqual((p.problems, p.names_to_confirm), ([], []))
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest, ai=p.ai))
        self.assertIn(bs.Name("テスト便", None, "script", None, ("てすとびん", "てすとべん"), "てすとびん"),
                      self.tx(lambda tx: bs.load(tx, v)).names)
        p = self.preview(data)                                            # 名前を洗い出す処理が答えなくても止めない
        self.assertEqual(p.problems, [])

    def test_during_verification_a_person_confirms_the_ai_reading(self):
        """検証モードでは、AI が決めた読みを契約者が確かめてから（直してもよい）。2026-10-16 ユーザー：「検証中は
        AIが認識したものを私が確認します」。"""
        self.start_verification()
        data = book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]})
        found = finder({"surface": "テスト便", "reading": "てすとべん", "source": "モデル2つ", "candidates": []})   # AI（誤り）
        p = self.preview(data, find_names=found)
        self.assertEqual(p.names_to_confirm, [{"text": "テスト便", "reading": "てすとべん", "source": "script"}])
        self.assertTrue(any("AI が決めた読みです" in x for x in p.problems), p.problems)
        fixed = [("テスト便", "てすとびん")]                                # 契約者が「てすとびん」に直して確かめた
        p = self.preview(data, find_names=found, confirmed_names=fixed)
        self.assertEqual((p.problems, p.names_to_confirm), ([], []))
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                           confirmed_names=fixed, ai=p.ai))
        self.assertIn(bs.Name("テスト便", "てすとびん", "script", "person"), self.tx(lambda tx: bs.load(tx, v)).names)

    def test_after_verification_the_ai_reading_is_used_without_a_person(self):
        """フルオート（検証モードでない業務）では、AI が決めた読みをそのまま使う（管理者も契約者も介在しない）。台本の読みがなは
        入口の読み。どれで決めたかは残る。"""
        data = book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]})
        found = finder({"surface": "テスト便", "reading": "てすとびん", "source": "モデル2つ", "candidates": []},
                       {"surface": "東海林", "reading": "しょうじ", "source": "台本の読みがな", "candidates": []})
        p = self.preview(data, find_names=found)
        self.assertEqual((p.problems, p.names_to_confirm), ([], []))
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                           ai=p.ai))
        names = self.tx(lambda tx: bs.load(tx, v)).names
        self.assertIn(bs.Name("テスト便", "てすとびん", "script", "ai"), names)
        self.assertIn(bs.Name("東海林", "しょうじ", "script", "given"), names)

    def test_the_callers_name_needs_its_reading_from_the_entrance(self):
        """担当者名の読みは、入口で必ず入れる（2026-10-16 ユーザー決定＝案C）。AI が読みを決めても使わない。"""
        rows = [r for r in book_rows("基本") if r[0] != "担当者名の読み"]
        found = finder({"surface": "山田", "reading": "やまだ", "source": "モデル2つ", "candidates": []})
        p = self.preview(book(sheets={"基本": rows}), find_names=found)
        self.assertTrue(any("担当者名の読みは必ず入れます" in x for x in p.problems), p.problems)

    def test_a_version_with_an_unconfirmed_ai_reading_is_refused_in_verification_mode(self):
        """プレビューを通さずに版を作っても、同じ確かめ（業務設定の1か所）が止める。"""
        from tests.test_business_settings import raw
        r = raw()
        r["names"][2]["reading_by"] = "ai"
        self.assertIsNotNone(self.tx(lambda tx: bs.save_version(tx, self.s["business"], r)))   # フルオート
        self.start_verification()
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))

    def test_the_ai_is_asked_once_and_registration_uses_the_previewed_answer(self):
        """AI は同じ問いに毎回同じ答えを返すとは限らない。プレビューで1回だけ聞き、登録はその答えを使う（聞き直すと、契約者が
        見たものと違う中身になり、登録できなくなる。2026-10-16 に見つけた穴）。"""
        data = book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]})
        answers = iter(["てすとびん", "てすとべん"])                 # 2回目は違う答え
        asked = []

        def ai(text):
            asked.append(text)
            return [{"surface": "テスト便", "reading": next(answers), "source": "モデル2つ", "candidates": []}]
        p = self.preview(data, find_names=ai)
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest, ai=p.ai))
        self.assertEqual(len(asked), 1)                                   # 登録では聞かない
        self.assertIn(bs.Name("テスト便", "てすとびん", "script", "ai"), self.tx(lambda tx: bs.load(tx, v)).names)

    def test_a_reading_must_be_kana(self):
        """AI が漢字やローマ字を返したら、読みとして使わない（読みが決まらない側に倒す）。入口の読みが使えなければ問題に出す。
        読みはひらがなで持つ（カタカナはひらがなに直す）。"""
        data = book(sheets={"名前": [("表記", "読み"), ("テスト便", None)]})
        p = self.preview(data, find_names=finder({"surface": "テスト便", "reading": "test bin", "source": "モデル2つ",
                                                  "candidates": []}))
        self.assertEqual(p.problems, [])
        self.assertEqual({n["text"]: n["reading"] for n in p.raw["names"]}["テスト便"], None)
        bad = book(sheets={"名前": [("表記", "読み"), ("テスト便", "手素都便")]})
        p = self.preview(bad)
        self.assertTrue(any("ひらがなかカタカナ" in x for x in p.problems), p.problems)
        kata = book(sheets={"名前": [("表記", "読み"), ("テスト便", "テストビン")]})
        self.assertEqual({n["text"]: n["reading"] for n in self.preview(kata).raw["names"]}["テスト便"], "てすとびん")

    def test_units_of_number_questions_are_decided_without_a_person(self):
        """数の質問の単位は、単位を決める処理（CL-1 の1か所）の答えを入れる。管理者も契約者も確かめない。決まらなければ空。"""
        rows = [("質問ID", "質問の文", "聞くこと", "型", "必須", "選択肢", "断りの答え"),
                ("Q1", "「テスト便」のご予約はなさいますか。", "ご予約", "選択肢", "はい", "する／しない", "しない"),
                ("Q2", "ご希望の日時をお聞かせください。", "ご希望の日時", "日時", "はい", None, None),
                ("Q3", "ご利用は何名様でしょうか。", "人数", "数", "はい", None, None),
                ("Q4", "拠点の数を教えていただけますか。", "拠点の数", "数", "いいえ", None, None)]
        data = book(sheets={"質問": rows, "分岐": [("質問ID", "回答条件", "次の質問ID"), ("Q1", "しない", "終わり"),
                                                  ("Q1", "その他", "Q2")]})
        asked = []

        def decide(texts):
            asked.extend(texts)
            return {"ご利用は何名様でしょうか。": "名", "拠点の数を教えていただけますか。": ""}   # 2つ目は決まらない
        p = self.preview(data, decide_units=decide)
        self.assertEqual(p.problems, [])
        self.assertEqual(asked, ["ご利用は何名様でしょうか。", "拠点の数を教えていただけますか。"])   # 数の質問だけ
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                           ai=p.ai))
        got = {q.id: q.unit for q in self.tx(lambda tx: bs.load(tx, v)).questions}
        self.assertEqual(got, {"Q1": None, "Q2": None, "Q3": "名", "Q4": None})

    def test_what_to_ask_about_is_decided_without_a_person(self):
        """質問の「聞くこと」は、入口に無ければ CL-1 の決め方（topics.decide）の答えを入れる。決まらなければ空。"""
        rows = [("質問ID", "質問の文", "聞くこと", "型", "必須", "選択肢", "断りの答え"),
                ("Q1", "「テスト便」のご予約はなさいますか。", "ご予約", "選択肢", "はい", "する／しない", "しない"),
                ("Q2", "ご希望の日時をお聞かせください。", None, "日時", "はい", None, None)]
        data = book(sheets={"質問": rows})
        asked = []

        def decide(texts):
            asked.extend(texts)
            return {"ご希望の日時をお聞かせください。": "ご希望の日時"}
        p = self.preview(data, decide_topics=decide)
        self.assertEqual((p.problems, asked), ([], ["ご希望の日時をお聞かせください。"]))   # 入口に無い質問だけ
        v = self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", data, expected_digest=p.digest, ai=p.ai))
        got = {q.id: q.ask_about for q in self.tx(lambda tx: bs.load(tx, v)).questions}
        self.assertEqual(got, {"Q1": "ご予約", "Q2": "ご希望の日時"})

    def test_other_entrances_go_the_same_way(self):
        """依頼者の画面から抜き出した中身（同じ形）も、同じ確かめと登録の道を通る。"""
        from tests.test_business_settings import raw
        extracted = raw()
        extracted["names"].append({"text": "テスト便", "reading": None, "source": "script"})
        p = self.tx(lambda tx: bi.review(tx, self.s["business"], extracted))
        self.assertEqual(p.problems, [])                                   # 読みが決まらなくても止めない（会話で確かめる）
        v = self.tx(lambda tx: bi.register_reviewed(tx, self.s["business"], p, expected_digest=p.digest))
        self.assertEqual(self.tx(lambda tx: bs.load(tx, v)).company.text, "株式会社テスト物流")

    def test_nothing_is_written_on_problems(self):
        bad = book(drop=("かけ直し",))
        p = self.preview(bad)
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bi.register(tx, self.s["business"], "業務設定.xlsx", bad, expected_digest=p.digest))


if __name__ == "__main__":
    unittest.main()
