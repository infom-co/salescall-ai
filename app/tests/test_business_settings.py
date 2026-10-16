"""業務設定の版（引き継ぎの (10)。core/business_settings.py）の試験。"""

from __future__ import annotations

import copy
import unittest

from salescall.core import business_settings as bs
from salescall.core.access import Rejected
from tests.support import OPERATOR, World, owner_access, rules_raw

FIELD_TYPES = frozenset({"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address", "yes_no",
                         "choice", "free_text"})


def raw() -> dict:
    """試験の業務設定（架空）：予約するか（しない＝断り＝終わり）→ 希望の日時。"""
    r = rules_raw(required=("Q1", "Q2"), decline={"Q1": ["しない"]})
    return {
        "purpose": "予約の希望日時を伺う",
        "caller": {"company": {"text": "株式会社テスト物流", "reading": "かぶしきがいしゃてすとぶつりゅう"},
                   "person": {"text": "山田", "reading": "やまだ"},
                   "purpose_line": "ご予約の確認でお電話いたしました。"},
        "target_person": {"kind": "anyone", "role": None},
        "busy_line": None,
        "voice": "S2",
        "questions": [
            {"id": "Q1", "text": "ご予約はなさいますか。", "ask_about": "ご予約", "type": "choice", "required": True,
             "choices": ["する", "しない"], "next": {"しない": "END", "*": "Q2"}},
            {"id": "Q2", "text": "ご希望の日時をお聞かせください。", "ask_about": "ご希望の日時", "type": "datetime",
             "required": True, "choices": None, "next": None},
        ],
        "names": [{"text": "テスト物流", "reading": "てすとぶつりゅう", "source": "caller", "confirmed": True}],
        "rules": r,
    }


def problems_of(r: dict) -> list[str]:
    with unittest.TestCase().assertRaises(bs.SettingsInvalid) as e:
        bs.check(r, field_types=FIELD_TYPES)
    return e.exception.problems


class Check(unittest.TestCase):
    def test_valid_and_flow(self):
        s = bs.check(raw(), field_types=FIELD_TYPES)
        self.assertEqual(s.questions[1].choices, ("答えた",))            # 値を聞く質問の選択肢は型で決まる
        self.assertEqual((s.next_of("Q1", "しない"), s.next_of("Q1", "する"), s.next_of("Q1", "わからない"),
                          s.next_of("Q2", "答えた")), ("END", "Q2", "Q2", "END"))

    def test_each_problem_is_found(self):
        cases = []
        r = raw(); r["questions"][0]["next"] = {"しない": "Q9", "*": "Q2"}; cases.append((r, "分岐先 Q9 がありません"))
        r = raw(); r["questions"][0]["next"] = {"しない": "END", "する": "Q2"}; cases.append((r, "受け皿がありません"))
        r = raw(); r["questions"][1]["next"] = {"*": "Q1"}; cases.append((r, "終わらない輪"))
        r = raw(); r["questions"][0]["next"] = {"*": "END"}; cases.append((r, "質問 Q2 へは、どこからも行きません"))
        r = raw(); r["rules"]["decline"] = {}; cases.append((r, "必須の質問 Q2 を聞かずに終わる道があります"))
        r = raw(); r["names"][0]["confirmed"] = False; cases.append((r, "読みを、まだ人が確かめていません"))
        r = raw(); r["questions"][0]["choices"] = ["する", "わからない"]; cases.append((r, "システムが足す答え"))
        r = raw(); r["questions"][0]["choices"] = ["する", "-"]; cases.append((r, "出力の印"))
        r = raw(); r["rules"]["decline"] = {"Q1": ["やめる"]}; cases.append((r, "断りの答え「やめる」は、その質問の選択肢にありません"))
        r = raw(); r["questions"][1]["type"] = "color"; cases.append((r, "型の一覧にありません"))
        r = raw(); r["questions"][1]["choices"] = ["はい"]; cases.append((r, "決まっています"))
        r = raw(); r["caller"]["company"]["reading"] = ""; cases.append((r, "名乗りの自社名の読み が空です"))
        r = raw(); r["target_person"] = {"kind": "anyone", "role": "社長"}; cases.append((r, "役職を書きません"))
        r = raw(); r["questions"][0]["next"] = {"しない": "END", "*": "Q2", "やめる": "END"}
        cases.append((r, "分岐の答え「やめる」は、その質問の答えにありません"))
        r = raw()
        for q in r["questions"]:
            q["required"] = False
        cases.append((r, "必須の質問が1つもありません"))
        for r, expected in cases:
            with self.subTest(expected):
                self.assertTrue(any(expected in p for p in problems_of(r)), problems_of(r))

    def test_all_problems_are_listed_at_once(self):
        r = raw(); r["purpose"] = ""; r["voice"] = ""; r["names"][0]["confirmed"] = False
        self.assertGreaterEqual(len(problems_of(r)), 3)                # 1つずつではなく、まとめて示す

    def test_system_answers_may_skip(self):
        """「わからない」「答えたくない」で次へ進むのは、理由×扱いで決まる（飛ばしでは止めない）。"""
        r = raw()
        r["questions"][0]["next"] = {"しない": "END", "わからない": "END", "*": "Q2"}
        bs.check(r, field_types=FIELD_TYPES)


class Versions(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def tx(self, work, actor=OPERATOR):
        return self.w.a.in_client(actor, self.s["tenant"], self.s["client"], work)

    def test_save_load_restore_and_assign(self):
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual((got.company.text, [q.id for q in got.questions], got.rules.required),
                         ("株式会社テスト物流", ["Q1", "Q2"], ("Q1", "Q2")))
        rows = self.tx(lambda tx: tx.all("SELECT question_id, position, type_name, required FROM salescall.questions"
                                         " WHERE version_id = %s ORDER BY position", (v,)))
        self.assertEqual([(r["question_id"], r["type_name"]) for r in rows], [("Q1", "choice"), ("Q2", "datetime")])
        back = self.tx(lambda tx: bs.restore(tx, v))                    # 前の版へ戻す＝同じ中身の新しい版
        self.assertNotEqual(back, v)
        self.assertEqual(self.tx(lambda tx: bs.load(tx, back)).raw, got.raw)
        outcome = self.tx(lambda tx: bs.assign(tx, self.s["run"], back, reason="試験：新しい版に切り替え"))
        self.assertEqual(outcome, "mock_needed")                       # 依頼主で最初の模擬通話を待つ
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v",
                                                   (self.s["run"],))["v"]), back)

    def test_nothing_is_saved_when_invalid(self):
        before = self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.business_versions")["n"])
        bad = raw(); bad["rules"]["retry"] = {}                        # 業務の決まりの抜け（版を作る途中で見つかる）
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bs.save_version(tx, self.s["business"], bad))
        bad2 = copy.deepcopy(raw()); bad2["questions"][0]["next"] = {"*": "Q9"}
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bs.save_version(tx, self.s["business"], bad2))
        after = self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.business_versions")["n"])
        self.assertEqual(after, before)                                # 全部か、何も無いか

    def test_contents_are_frozen(self):
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        own = owner_access()
        for sql in ("UPDATE salescall.business_contents SET content = '{}'", "DELETE FROM salescall.business_contents"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, self.s["tenant"], self.s["client"], lambda tx: tx.execute(sql))
        self.assertIsNotNone(v)


if __name__ == "__main__":
    unittest.main()
