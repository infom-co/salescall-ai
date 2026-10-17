"""業務設定の版（引き継ぎの (10)。core/business_settings.py）の試験。"""

from __future__ import annotations

import copy
import unittest

from salescall.core import business_settings as bs
from salescall.core.access import Rejected
from tests.support import OPERATOR, World, owner_access, rules_raw

FIELD_TYPES = frozenset({"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address", "yes_no",
                         "choice", "free_text"})


def name(text, reading, source, by="given", candidates=(), likely=None) -> dict:
    return {"text": text, "reading": reading, "source": source, "reading_by": by if reading else None,
            "candidates": list(candidates), "likely": likely}


def raw() -> dict:
    """試験の業務設定（架空）：予約するか（しない＝断り＝終わり）→ 希望の日時。"""
    r = rules_raw(required=("Q1", "Q2"), decline={"Q1": ["しない"]})
    return {
        "purpose": "予約の希望日時を伺う",
        "target_class": "corporate",
        "caller": {"company": {"text": "株式会社テスト物流", "reading": "かぶしきがいしゃてすとぶつりゅう"},
                   "person": {"text": "山田", "reading": "やまだ"},
                   "purpose_line": "ご予約の確認でお電話いたしました。"},
        "target_person": {"kind": "anyone", "role": None},
        "busy_line": None,
        "voice": "S2",
        "questions": [
            {"id": "Q1", "text": "ご予約はなさいますか。", "ask_about": "ご予約", "type": "choice", "required": True,
             "choices": ["する", "しない"], "next": {"しない": "END", "*": "Q2"}, "unit": None, "meanings": None, "replies": None},
            {"id": "Q2", "text": "ご希望の日時をお聞かせください。", "ask_about": "ご希望の日時", "type": "datetime",
             "required": True, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None},
        ],
        "names": [name("株式会社テスト物流", "かぶしきがいしゃてすとぶつりゅう", "caller"), name("山田", "やまだ", "caller"),
                  name("テスト物流", "てすとぶつりゅう", "script")],
        "rules": r,
    }


def problems_of(r: dict) -> list[str]:
    with unittest.TestCase().assertRaises(bs.SettingsInvalid) as e:
        bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=True)
    return e.exception.problems


class Check(unittest.TestCase):
    def test_valid_and_flow(self):
        s = bs.check(raw(), field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(s.questions[1].choices, ("答えた",))            # 値を聞く質問の選択肢は型で決まる
        self.assertEqual((s.next_of("Q1", "しない"), s.next_of("Q1", "する"), s.next_of("Q1", "わからない"),
                          s.next_of("Q2", "答えた")), ("END", "Q2", "Q2", "END"))

    def test_meanings_and_replies_of_choice_questions(self):
        """はい／いいえ・選択肢の質問は、意味の言葉（選択肢 → 言葉）と曖昧な言葉の意味（言葉 → 選択肢）を持てる。宣言した選択肢に
        だけ付く（システムの答えには付けない。CL-1 の連絡帳 4-17）。"""
        r = raw()
        r["questions"][0]["meanings"] = {"する": "ご予約なさるということ", "しない": "ご予約はなさらないということ"}
        r["questions"][0]["replies"] = {"結構": "しない"}
        s = bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.questions[0].meanings["する"], s.questions[0].replies), ("ご予約なさるということ", {"結構": "しない"}))
        self.assertEqual((s.questions[1].meanings, s.questions[1].replies), ({}, {}))
        for key, bad in (("meanings", {"わからない": "わからないということ"}), ("meanings", {"する": " "}),
                         ("replies", {"結構": "どちらでも"}), ("replies", {"たぶん": "する"}), ("meanings", ["する"])):
            with self.subTest(key=key, bad=bad):
                r = raw()
                r["questions"][0][key] = bad
                self.assertTrue(any("意味" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][1]["meanings"] = {"答えた": "お答えいただいたということ"}
        self.assertTrue(any("はい／いいえ・決まった選択肢の質問だけ" in p for p in problems_of(r)), problems_of(r))

    def test_any_is_a_system_answer_of_choice_questions(self):
        """決まった選択肢の質問には、システムが「どちらでも」を足す（お客様がこちらに任せた＝取れた答え。2026-10-17 ユーザー。
        CL-1 の連絡帳 4-17）。同じ名前の選択肢は書けない。分岐を書くなら、その行き先も要る（受け皿でよい）。はい／いいえには足さない。"""
        s = bs.check(raw(), field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(bs.possible_answers(s.questions[0]), ("する", "しない", "どちらでも", "わからない", "答えたくない"))
        self.assertEqual(s.next_of("Q1", "どちらでも"), "Q2")                       # 受け皿へ
        r = raw()
        r["questions"][0]["choices"] = ["する", "しない", "どちらでも"]
        self.assertTrue(any("選択肢「どちらでも」は、システムが足す答え" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][0]["next"] = {"する": "Q2", "しない": "END", "わからない": "Q2", "答えたくない": "Q2"}
        self.assertTrue(any("受け皿がありません" in p and "どちらでも" in p for p in problems_of(r)), problems_of(r))
        yes_no = bs.Question("Q9", "ご予約はお済みですか。", None, "yes_no", True, bs.YES_NO_CHOICES, {})
        self.assertNotIn("どちらでも", bs.possible_answers(yes_no))

    def test_many_optional_follow_ups_check_quickly_and_still_find_skips(self):
        """「はい ⇒ 詳しく聞く、いいえ ⇒ 次へ」が続く業務（40問）でも、確かめはすぐ終わる（前は質問の数に対して何倍にも
        増え、36問で15秒かかった＝2026-10-16 の見直し I2）。必須を飛ばす道は、これまでどおり見つける。"""
        import time
        r = raw()
        qs = [r["questions"][0]]
        n = 20
        for i in range(n):
            ask, detail = f"A{i}", f"D{i}"
            nxt = f"A{i + 1}" if i + 1 < n else "Q2"
            qs.append({"id": ask, "text": "ご利用はございますか？", "ask_about": "ご利用", "type": "choice", "required": False,
                       "choices": ["ある", "ない"], "next": {"ある": detail, "*": nxt}, "unit": None, "meanings": None, "replies": None})
            qs.append({"id": detail, "text": "詳しくお聞かせください。", "ask_about": "詳しく", "type": "free_text",
                       "required": False, "choices": None, "next": {"*": nxt}, "unit": None, "meanings": None, "replies": None})
        qs[0] = dict(qs[0], next={"しない": "END", "*": "A0"})
        qs.append(r["questions"][1])
        r["questions"] = qs
        started = time.monotonic()
        bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertLess(time.monotonic() - started, 2.0)
        qs[-2] = dict(qs[-2], next={"*": "END"})                         # 最後の詳しく聞く質問から、必須 Q2 を飛ばして終わる
        self.assertTrue(any("必須の質問 Q2 を聞かずに終わる道" in p for p in problems_of(r)))

    def test_malformed_values_are_problems_not_crashes(self):
        """画面・抜き出しの入口から形の違う値が来ても、処理が落ちずに問題として並べる（2026-10-16 の見直し I9）。"""
        r = raw(); r["questions"][1]["choices"] = 5
        self.assertTrue(any("選択肢" in p for p in problems_of(r)))
        r = raw(); r["questions"][1]["type"] = ["x"]
        self.assertTrue(any("型" in p for p in problems_of(r)))

    def test_each_problem_is_found(self):
        cases = []
        r = raw(); r["questions"][0]["next"] = {"しない": "Q9", "*": "Q2"}; cases.append((r, "分岐先 Q9 がありません"))
        r = raw(); r["questions"][0]["next"] = {"しない": "END", "する": "Q2"}; cases.append((r, "受け皿がありません"))
        r = raw(); r["questions"][1]["next"] = {"*": "Q1"}; cases.append((r, "終わらない輪"))
        r = raw(); r["questions"][0]["next"] = {"*": "END"}; cases.append((r, "質問 Q2 へは、どこからも行きません"))
        r = raw(); r["rules"]["decline"] = {}; cases.append((r, "必須の質問 Q2 を聞かずに終わる道があります"))
        r = raw(); r["names"][2]["reading_by"] = "ai"; cases.append((r, "AI が決めた読みです。検証モードでは、契約者が確かめてから"))
        r = raw(); r["names"][2]["reading_by"] = None; cases.append((r, "読みを決めたもの"))
        r = raw(); r["names"][2]["reading"] = "テストブツリュウ"; cases.append((r, "ひらがなで"))      # 持つのはひらがな
        r = raw(); r["names"][2].update(reading=None, reading_by=None, candidates=["中島"]); cases.append((r, "ひらがなです"))
        r = raw(); r["names"][2]["candidates"] = ["てすと"]; cases.append((r, "決まった読みには候補を付けません"))
        r = raw(); r["names"].append(name("テスト物流", "てすとものながれ", "script")); cases.append((r, "同じ表記が2つ"))
        r = raw(); r["caller"]["company"]["reading"] = "べつのよみ"; cases.append((r, "名前の一覧の読みと違います"))
        r = raw(); del r["names"][1]; cases.append((r, "名乗りの担当者名「山田」が、名前の一覧（名乗り）にありません"))
        # 担当者名の読みは、入口で必ず入れる（2026-10-16 ユーザー決定＝案C。読みが決まらない・AI の読みは通さない）
        r = raw(); r["names"][1].update(reading=None, reading_by=None, candidates=["やまだ"], likely="やまだ")
        r["caller"]["person"]["reading"] = None; cases.append((r, "担当者名の読みは必ず入れます"))
        r = raw(); r["names"][1]["reading_by"] = "ai"; cases.append((r, "担当者名の読みは必ず入れます"))
        r = raw(); r["questions"][1]["unit"] = "人"; cases.append((r, "単位は、数の型（number）の質問だけ"))
        r = raw(); r["questions"][0]["choices"] = ["する", "わからない"]; cases.append((r, "システムが足す答え"))
        r = raw(); r["questions"][0]["choices"] = ["する", "-"]; cases.append((r, "出力の印"))
        r = raw(); r["rules"]["decline"] = {"Q1": ["やめる"]}; cases.append((r, "断りの答え「やめる」は、その質問の選択肢にありません"))
        r = raw(); r["questions"][1]["type"] = "color"; cases.append((r, "型の一覧にありません"))
        r = raw(); r["questions"][1]["choices"] = ["はい"]; cases.append((r, "決まっています"))
        r = raw(); r["target_person"] = {"kind": "anyone", "role": "社長"}; cases.append((r, "役職を書きません"))
        r = raw(); r["target_class"] = "individual"; cases.append((r, "お客様が個人の業務は、まだ選べません"))
        r = raw(); r["target_class"] = None; cases.append((r, "お客様の区分（target_class）は"))
        r = raw(); r["questions"][0]["next"] = {"しない": "END", "*": "Q2", "やめる": "END"}
        cases.append((r, "分岐の答え「やめる」は、その質問の答えにありません"))
        r = raw()
        for q in r["questions"]:
            q["required"] = False
        cases.append((r, "必須の質問が1つもありません"))
        for r, expected in cases:
            with self.subTest(expected):
                self.assertTrue(any(expected in p for p in problems_of(r)), problems_of(r))

    def test_no_false_reason_when_a_question_cannot_be_read(self):
        r = raw(); r["questions"][1]["type"] = "color"                     # Q2 が読めない
        got = problems_of(r)
        self.assertTrue(any("型の一覧にありません" in p for p in got))
        self.assertFalse(any("分岐先" in p or "どこからも" in p for p in got), got)   # 読めない質問の巻き添えの理由を出さない

    def test_all_problems_are_listed_at_once(self):
        r = raw(); r["purpose"] = ""; r["voice"] = ""; r["names"][2]["reading_by"] = "ai"
        self.assertGreaterEqual(len(problems_of(r)), 3)                # 1つずつではなく、まとめて示す

    def test_system_answers_may_skip(self):
        """「わからない」「答えたくない」で次へ進むのは、理由×扱いで決まる（飛ばしでは止めない）。"""
        r = raw()
        r["questions"][0]["next"] = {"しない": "END", "わからない": "END", "*": "Q2"}
        bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=True)

    def test_ai_reading_and_unit(self):
        """AI が決めた読みは、フルオートでは契約者が確かめずに通る。読みが決まらない名前も、検証モードでも通る（会話の中で確かめる。
        2026-10-16 ユーザー）。自社名の読みは決まらなくてもよい。数の質問は単位を持てる。"""
        r = raw(); r["names"][2]["reading_by"] = "ai"
        s = bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=False)
        self.assertEqual(s.names[2].reading_by, "ai")
        r = raw(); r["names"][2].update(reading=None, reading_by=None, candidates=["てすとぶつりゅう", "てすともつりゅう"],
                                        likely="てすとぶつりゅう")
        r["names"][0].update(reading=None, reading_by=None); r["caller"]["company"]["reading"] = None
        s = bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.names[2].reading, s.names[2].likely, s.company.reading), (None, "てすとぶつりゅう", None))
        r = raw(); r["questions"][1].update(type="number", unit="名")
        self.assertEqual(bs.check(r, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[1].unit, "名")


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
        self.assertEqual(outcome, "mock_needed")                       # 依頼者で最初の模擬通話を待つ
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v",
                                                   (self.s["run"],))["v"]), back)

    def test_assigning_a_new_version_releases_reservations_of_the_old_one(self):
        """版を替えたら、前の版で取ったまだ発信していない予約を取り消す（門の前提が変わったときの決まり。2026-10-16 の見直し G7）。"""
        claim = self.w.insert_claim(self.s)                            # 前の版での予約
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.tx(lambda tx: bs.assign(tx, self.s["run"], v, reason="試験：新しい版に切り替え"))
        state = self.tx(lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s", (claim,)))["state"]
        self.assertEqual(state, "released")

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
