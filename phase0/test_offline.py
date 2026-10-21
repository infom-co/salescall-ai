"""接続せずに確かめられる部分の試験（python -m unittest test_offline）。

- .env の読み方（書き方の違いを吸収できるか）
- キーの値が、画面・結果のファイルに出ないか（DESIGN.md I-10。わざと漏らそうとして、伏せられるか）
- 試験用の流れの制御（g06_flow.MiniFlow）が、決まりどおりに進む・止まるか（I-18・I-20 の入口）
"""
from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import common


def fc(name, **args):
    return SimpleNamespace(name=name, args=args, id="x")


class EnvAndRedactionTest(unittest.TestCase):
    def setUp(self):
        common._LOADED_SECRETS.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / ".env"
        self.secret = "AIza" + "S" * 35
        self.env.write_text(
            "# comment\n"
            f"GEMINI_API_KEY_1={self.secret}\n"
            "export GEMINI_API_KEY_2='AIzaQUOTED_________________________x'\n"
            "GOOGLE_API_KEY_3=\"AIzaDOUBLE_________________________y\" # memo\n"
            "OTHER=value\n",
            encoding="utf-8",
        )

    def test_reads_all_forms(self):
        keys = common.load_keys(self.env)
        self.assertEqual([k.name for k in keys], ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GOOGLE_API_KEY_3"])
        self.assertTrue(keys[1].value.startswith("AIzaQUOTED"))
        self.assertTrue(keys[2].value.endswith("y"))

    def test_label_and_repr_hide_value(self):
        k = common.load_keys(self.env)[0]
        self.assertNotIn(self.secret, k.label)
        self.assertNotIn(self.secret, repr(k))

    def test_redact_in_errors_and_results(self):
        common.load_keys(self.env)
        err = RuntimeError(f"failed with key={self.secret}")
        info = common.describe_error(err)
        self.assertNotIn(self.secret, json.dumps(info))
        common.RESULTS_DIR = self.tmp / "results"
        path = common.save_result("TEST", {"leak": self.secret, "nested": {"also": f"x{self.secret}x"}})
        self.assertNotIn(self.secret, path.read_text(encoding="utf-8"))

    def test_falls_back_to_environ_when_no_file(self):
        import os
        os.environ["GEMINI_API_KEY_TEST"] = "AIza" + "E" * 35
        try:
            keys = common.load_keys(self.tmp / "no_such.env", ["GEMINI_API_KEY_TEST"])
            self.assertEqual(keys[0].name, "GEMINI_API_KEY_TEST")
            self.assertEqual(common.redact(f"x{keys[0].value}x"), "x***x")
        finally:
            del os.environ["GEMINI_API_KEY_TEST"]

    def test_explicit_names_missing(self):
        with self.assertRaises(SystemExit):
            common.load_keys(self.env, ["NOT_THERE"])


class FlowTest(unittest.TestCase):
    """流れの制御（flowctl.Flow）。DESIGN.md 4-4・6-2・6-11 の決まりどおりに進む・止まるか。"""

    def flow(self, *, q1_required=True):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますか。", "yes_no", required=q1_required, conditions=("yes", "no")),
                     Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("answered",))])

    def call(self, f, name, **args):
        return f.apply(name, args)  # 中の名前での入口（AIからの入口 handle の置き換えは、AIToJapaneseTest で確かめる）

    def test_yes_no_accepted_and_advances(self):
        f = self.flow()
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes", value="はい、予約しています。")
        self.assertEqual((r["result"], f.current, f.items["Q1"].state), ("next", "Q2", "accepted"))
        self.assertEqual(f.export()["Q1"], "yes")  # 選択肢の型は、相手の言葉ではなく宣言された選択肢を渡す

    def test_wrong_question_and_undeclared_condition_rejected(self):
        f = self.flow()
        self.assertEqual(self.call(f, "record_answer", question_id="Q2", condition_id="answered")["result"], "rejected")
        self.assertEqual(self.call(f, "record_answer", question_id="Q1", condition_id="maybe")["result"], "rejected")
        self.assertEqual((f.current, f.items["Q1"].state), ("Q1", "empty"))

    def test_confirm_type_held_until_callee_affirms(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "15日の午後2時"))  # 言い直すのは記録した値そのもの
        self.assertEqual(f.phase, "asking")  # 確かめる前に終わりへ進まない（I-18）
        # 相手の返事が届く前の報告は差し戻す（I-24）
        self.assertEqual(self.call(f, "confirm_field", question_id="Q2", result="affirmed")["result"], "rejected")
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((r["result"], f.items["Q2"].state, f.phase), ("end", "confirmed", "closing"))

    def test_ask_back_up_to_limit_then_unresolved_then_one_recheck_at_end(self):
        from flowctl import Limits
        from phrases import ASK_BACK
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        says = []
        for k in range(Limits().ask_back):  # 必須の項目は、その場で3回まで（✅ 2026-10-14 ユーザー：大事なポイントは3回までOK）
            r = self.call(f, "report_unclear", question_id="Q2")
            self.assertEqual((r["result"], r["ask_n"]), ("ask_back", k + 1))
            says.append(r["say"])
        self.assertEqual(says[0], ASK_BACK)
        self.assertEqual(says[1], "もう一回よろしいでしょうか？")  # 録音「申し訳ございません、」のあと（申し訳なさそうに）
        self.assertEqual(len(set(says)), len(says))                 # 同じ言い方をくり返さない
        r = self.call(f, "report_unclear", question_id="Q2")  # 使い切ったら、もう聞き返させない（I-20）
        self.assertEqual((r["result"], r["question_id"]), ("recheck", "Q2"))  # 締めの前に念のための確認を1回
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].rechecks), ("unresolved", 1))
        r = self.call(f, "report_unclear", question_id="Q2")  # 念のための確認でも聞けない
        self.assertEqual((r["result"], f.items["Q2"].state), ("end", "unresolved"))
        self.assertEqual(f.export()["Q2"], "-")

    def test_recheck_answer_is_read_back(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "report_unclear", question_id="Q2")
        self.call(f, "report_unclear", question_id="Q2")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        self.assertEqual(r["result"], "confirm_required")  # 念のための確認で聞けたら復唱する（6-11）
        f.on_callee()
        self.assertEqual(self.call(f, "confirm_field", question_id="Q2", result="affirmed")["result"], "end")
        self.assertEqual(f.export()["Q2"], "15日の午後2時")

    def test_yes_no_after_ask_back_needs_readback(self):
        f = self.flow()
        self.call(f, "report_unclear", question_id="Q1")
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.assertEqual(r["result"], "confirm_required")  # 聞き返したあとは、確かめない型でも復唱する（6-11）
        self.assertNotIn("None", r["say"])                 # 値の無い型は、選んだ選択肢で言い直す（⛔ 前は「Noneで、…」を作っていた）
        self.assertIn("yes", r["say"])

    def test_correction_read_back_once_more_then_unresolved(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected", correction="16日の午後2時")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "16日の午後2時"))
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected", correction="17日の午後2時")
        self.assertEqual(f.items["Q2"].state, "unresolved")  # 訂正のあとの復唱は1回まで
        self.assertEqual(r["result"], "recheck")  # 「-」の必須項目なので、締めの前に1回だけ念のため

    def test_refused_never_asked_again_and_exported_as_cross(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="refused")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="refused")
        self.assertEqual(r["result"], "end")  # 念のための確認にも入れない
        self.assertEqual(f.export(), {"Q1": "✕", "Q2": "✕"})

    def test_unknown_answer_is_dash_without_recheck(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="unknown")
        self.assertEqual((r["result"], f.items["Q2"].state), ("end", "dont_know"))  # 答えは聞けたので、念のため聞き直さない
        self.assertEqual(f.export()["Q2"], "-")

    def test_dont_know_is_not_got(self):
        # 2026-10-13 ユーザー決定（CL-2 から依頼）：必須の質問が「わからない」なら、業務は完了ではない。
        # 前は accepted（＝取れた）にしていたので、ほかがそろうと「すべて取れた」になっていた
        from flowctl import GOT
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="unknown")
        self.assertNotIn(f.items["Q2"].state, GOT)
        self.assertEqual(sorted(q for q, it in f.items.items() if it.state in GOT), ["Q1"])

    def test_type_list(self):
        from flowctl import Flow, Question, normalize_value
        with self.assertRaises(AssertionError):  # 型の一覧に無い型は作れない（I-16）
            Question("Q1", "ご意見は。", "opinion", conditions=("答えた",))
        # 自由な意見は確かめない型で、相手の言葉のまま（言い終わりの「です」を外さない）。値は要る
        self.assertEqual(normalize_value("free_text", "とても良かったです。"), "とても良かったです。")
        f = Flow([Question("Q1", "ご意見をお聞かせください。", "free_text", conditions=("答えた",))])
        self.assertEqual(self.call(f, "record_answer", question_id="Q1", condition_id="答えた")["result"], "rejected")  # 値が無い
        r = self.call(f, "record_answer", question_id="Q1", condition_id="答えた", value="とても良かったです。")
        self.assertEqual((r["result"], f.items["Q1"].state), ("end", "accepted"))
        self.assertEqual(f.export(), {"Q1": "とても良かったです。"})

    def test_declined_close_is_fixed_phrase(self):
        # 2026-10-13 ユーザー確定：急いでいる・断られたときの締めは決まった言い方（G-22：AIが6本中5本で「また改めてご連絡いたします」と約束した）
        from phrases import CLOSE_DECLINED
        for reason in ("callee_hurry", "callee_refused_call"):
            f = self.flow()
            r = self.call(f, "request_end", reason=reason)
            self.assertEqual((r["result"], r["say"]), ("end", CLOSE_DECLINED), reason)
            self.assertIn(CLOSE_DECLINED, r["message"])
        self.assertEqual(CLOSE_DECLINED, "お忙しいところ申し訳ございませんでした。それでは失礼いたします。")  # ユーザーの言葉のまま
        # ふつうの締めも、AIが話す形では決まった言い方を渡す（⛔ 前は「締めの言葉を言って」とだけ伝え、AIが「お気軽にお問い合わせください」を足した）
        from phrases import CLOSING
        f = self.flow()
        f.current = f.order[-1]
        f.items[f.current].state = "accepted"
        for q in f.order:
            f.items[q].state = "accepted"
        r = f._advance("")
        self.assertEqual((r["result"], r["say"]), ("end", CLOSING))
        self.assertIn("言葉を足しません", r["message"])
        from instruction import Business, build
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=list(self.flow().questions.values()))
        self.assertIn("約束してはいけません", build(b, "S2")[1])

    def test_hurry_skips_recheck(self):
        f = self.flow()
        self.call(f, "report_unclear", question_id="Q1")
        self.call(f, "report_unclear", question_id="Q1")  # Q1 は「-」
        r = self.call(f, "request_end", reason="callee_hurry")
        self.assertEqual((r["result"], f.phase, f.items["Q1"].rechecks), ("end", "closing", 0))

    def test_done_rejected_while_questions_remain(self):
        f = self.flow()
        self.assertEqual(self.call(f, "request_end", reason="done")["result"], "rejected")

    def test_done_rejected_while_awaiting_readback_or_rechecking(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")  # 最後の質問の言い直し待ち
        self.assertEqual(self.call(f, "request_end", reason="done")["result"], "rejected")
        self.assertEqual(f.items["Q2"].state, "checking")
        from flowctl import Limits
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        for _ in range(Limits().ask_back + 1):
            self.call(f, "report_unclear", question_id="Q2")              # 聞き返しを使い切り「-」⇒ 念のための確認へ
        self.assertEqual((f.phase, f.current), ("rechecking", "Q2"))
        self.assertEqual(self.call(f, "request_end", reason="done")["result"], "rejected")  # 念のための確認を飛ばさない
        self.assertEqual(self.call(f, "request_end", reason="callee_hurry")["result"], "end")  # 急いでいる相手は終えてよい
        self.assertEqual(f.export()["Q2"], "-")

    def test_optional_item_not_asked_back(self):
        f = self.flow(q1_required=False)
        r = self.call(f, "report_unclear", question_id="Q1")
        self.assertEqual((r["result"], f.items["Q1"].state), ("next", "unresolved"))  # 必須でない項目は聞き返さない

    def test_record_answer_rejected_while_awaiting_readback_reply(self):
        # G-16：言い直した値に相手が「はい」と答えたのに、AIが record_answer を呼んだ ⇒ 差し戻して confirm_field へ導く
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        f.on_callee()
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("confirm_field", r["message"])

    def test_value_normalized(self):
        from flowctl import normalize_value
        self.assertEqual(normalize_value("datetime", "15日の午後2時です。"), "15日の午後2時")
        self.assertEqual(normalize_value("number", "3人です"), "3人")
        self.assertEqual(normalize_value("name", "あかね"), "あかね")  # 名前の終わりを削らない
        # 空白に意味の無い型は、値の中の空白も外す（G-34：AIが「15 日の午後 2 時」と、相手の声の文字のまま写した）
        self.assertEqual(normalize_value("datetime", "15 日の午後 2 時"), "15日の午後2時")
        self.assertEqual(normalize_value("number", "3　人"), "3人")
        self.assertEqual(normalize_value("name", "佐藤 太郎"), "佐藤 太郎")  # 名前・住所・メールは外さない
        self.assertEqual(normalize_value("yes_no", "はい、そうです。"), "はい、そうです。")  # 選択肢の型はそのまま（渡すのは選択肢）

    def test_every_rejection_says_what_to_do_now(self):
        # 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に、Q3 の答えとして報告された
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("yes", "no")),
                  Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("answered",)),
                  Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("answered",))])
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        f.on_callee()
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時です。")
        f.on_callee()
        for op, kw in (("record_answer", {"question_id": "Q3", "condition_id": "answered", "value": "3人"}),
                       ("report_unclear", {"question_id": "Q3"}), ("request_end", {"reason": "done"}),
                       ("confirm_field", {"question_id": "Q3", "result": "affirmed"}), ("no_such_op", {})):
            r = self.call(f, op, **kw)
            self.assertEqual(r["result"], "rejected", op)
            self.assertIn("15日の午後2時でよろしいでしょうか？", r["message"], op)  # 待っている言い直しと、その言い方
            self.assertIn("confirm_field", r["message"], op)
        self.assertEqual(f.items["Q3"].state, "empty")  # 差し戻した報告は、記録に入らない
        r = self.call(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((r["result"], r["question_id"]), ("next", "Q3"))
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.assertIn("いまの質問は Q3「", r["message"])  # 言い直しを待っていないときは、いまの質問とその文

    def test_value_required_for_value_types(self):
        # 2026-10-13 G-25：値の無い答えを受け付け、言い直しに「Noneで、よろしいでしょうか。」を入れていた
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")          # 選択肢の型は、値が無くてよい
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered")  # 値の要る型で値が無い
        self.assertEqual(r["result"], "rejected")
        self.assertNotIn("None", r["message"])
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value), ("empty", None))  # 記録は書き換えない
        r = self.call(f, "record_answer", question_id="Q2", condition_id="unknown")   # 「わからない」は値が無くてよい
        self.assertEqual(r["result"], "end")

    def test_correction_value_required(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected")       # 訂正の値が無い
        self.assertEqual(r["result"], "rejected")
        self.assertEqual((f.awaiting_confirm, f.items["Q2"].value, f.items["Q2"].corrections), ("Q2", "15日の午後2時", 0))
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected", correction="16日の午後2時です。")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "16日の午後2時"))

    def test_confirm_phrases_rotate_by_situation(self):
        from flowctl import Flow, Question
        from phrases import confirm
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("answered",)), Question("Q2", "人数は。", "number", conditions=("answered",)),
                  Question("Q3", "電話番号は。", "phone", conditions=("answered",))], lead_ins=False)  # 言い直しの言い方だけを見る
        r1 = self.call(f, "record_answer", question_id="Q1", condition_id="answered", value="15日")
        f.on_callee()
        self.call(f, "confirm_field", question_id="Q1", result="affirmed")
        r2 = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="3人")
        self.assertEqual(r1["say"], confirm("15日", "ふつう", 0))
        self.assertEqual(r2["say"], confirm("3人", "ふつう", 1))       # 同じ言い方を続けない（順番に回す）
        f.on_callee()
        self.assertIn(r2["say"], self.call(f, "record_answer", question_id="Q3", condition_id="answered", value="x")["message"])  # 差し戻しも同じ言い方
        self.call(f, "confirm_field", question_id="Q2", result="affirmed")
        self.call(f, "report_unclear", question_id="Q3")                       # 聞き返したあとの値
        r3 = self.call(f, "record_answer", question_id="Q3", condition_id="answered", value="090-1234-5678")
        self.assertEqual(r3["say"], confirm("090-1234-5678", "聞き取りにくかったあと", 0))
        f.on_callee()
        r4 = self.call(f, "confirm_field", question_id="Q3", result="corrected", correction="090-1234-5679")
        self.assertEqual(r4["say"], confirm("090-1234-5679", "聞き違いのあと", 0))  # こちらの聞き違いのあとは、その場面の言い方

    def test_lead_ins_rotate_and_default_on(self):
        from flowctl import Flow, Question
        from phrases import LEAD_IN
        qs = [Question("Q1", "日時は。", "datetime", conditions=("答えた",)), Question("Q2", "人数は。", "number", conditions=("答えた",))]
        f = Flow(qs, lead_ins=False)                                       # 付けない形（過去の比べの試験のためだけ）
        self.assertEqual(self.call(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日")["say"], "15日でよろしいでしょうか？")
        f = Flow(qs)                                                       # ✅ 2026-10-13 ユーザー承認：既定で付ける（G-27 の W1）
        r1 = self.call(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日")
        self.assertTrue(r1["say"].startswith(LEAD_IN["言い直し"][0]))
        f.on_callee()
        r2 = self.call(f, "confirm_field", question_id="Q1", result="affirmed")
        self.assertIn(f"「{LEAD_IN['次の質問'][0]}」から話し始めてください。", r2["message"])
        r3 = self.call(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")
        self.assertTrue(r3["say"].startswith(LEAD_IN["言い直し"][1]))   # 同じ受けの言葉を続けない
        f.on_callee()
        self.assertIn(r3["say"], self.call(f, "request_end", reason="done")["message"])  # 差し戻しも同じ言い方

    def test_apology_rule_in_instruction(self):
        # 2026-10-13 ユーザー：相手が聞き取れなかったとき・不快な思いをさせたときは、話し始めにお詫びの言葉を付ける
        from instruction import Business, barge_in_note, build
        from phrases import APOLOGY
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=list(self.flow().questions.values()))
        text = build(b, "S2")[1]
        self.assertTrue(all(a in text for a in APOLOGY))
        self.assertIn(APOLOGY[0], barge_in_note(b, ["お忙しいところ恐れ入ります。"], ["株式会社「アオバ物流」の「佐藤」と申します。"]))

    def test_busy_ask_once_when_set(self):
        from flowctl import Flow, Question
        qs = [Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("yes", "no"))]
        f = Flow(qs)                                                       # 既定：丁寧に終える
        self.assertEqual(self.call(f, "request_end", reason="callee_hurry")["result"], "end")
        f = Flow(qs, busy_ask="少しだけ、お時間をいただけますでしょうか。")      # 業務設定で「引き止める」を選んだ
        r = self.call(f, "request_end", reason="callee_hurry")
        self.assertEqual((r["result"], r["say"], f.phase), ("busy_ask", "少しだけ、お時間をいただけますでしょうか。", "asking"))
        self.assertEqual(self.call(f, "request_end", reason="callee_hurry")["result"], "end")  # 2回目は終える（引き止めは1回だけ）
        f = Flow(qs, busy_ask="少しだけ、お時間をいただけますでしょうか。")
        self.assertEqual(self.call(f, "request_end", reason="callee_refused_call")["result"], "end")  # 断られたら必ず終える（原則14）

    def test_confirm_phrase_is_the_fixed_one(self):
        from flowctl import Flow
        from phrases import confirm
        f = Flow(list(self.flow().questions.values()), lead_ins=False)  # 言い直しの言い方だけを見る
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="16日の午後2時")
        self.assertEqual(r["say"], confirm("16日の午後2時"))
        self.assertIn(f"「{confirm('16日の午後2時')}」", r["message"])

    def test_confirm_without_request_rejected(self):
        f = self.flow()
        self.assertEqual(self.call(f, "confirm_field", question_id="Q1", result="affirmed")["result"], "rejected")

    def test_instruction_quotes_names_only_once(self):
        from flowctl import Question
        from instruction import Business, build
        b = Business(company="アオバ物流", caller="佐藤", purpose="確認", questions=[Question("Q1", "質問", "yes_no", conditions=("yes",))])
        _, text = build(b, "L4")
        self.assertIn("株式会社「アオバ物流」の「佐藤」と申します", text)
        self.assertNotIn("「「", text)
        self.assertNotIn("倍速", text)
        self.assertNotIn("1文字も変えない", text)


class TurnCheckTest(unittest.TestCase):
    """AIの1ターンの見分け方（turn_check）。G-17 の実際の例で確かめる。"""

    def test_morae(self):
        from jtext import morae
        self.assertEqual(morae("失礼いたします。"), 9)  # しつれいいたします
        self.assertEqual(morae("株式会社「アオバ物流」"), morae("株式会社アオバ物流"))  # かぎかっこは数えない

    def test_classify_examples(self):
        from turn_check import classify
        # ふつう：Sadachbia「失礼いたします」1.0秒
        self.assertEqual(classify(voiced_s=1.0, voice="Sadachbia", expected_text="失礼いたします。")["kind"], "ok")
        # 途中で止まった：「失礼いたし」0.75秒（G-17 #33）
        self.assertEqual(classify(voiced_s=0.75, voice="Sadachbia", expected_text="失礼いたします。")["kind"], "cut_off")
        # 同じ文を2回言った：Leda 8.39秒（G-17 #24）
        self.assertEqual(classify(voiced_s=8.39, voice="Leda", expected_text="本日は、先日お送りした資料の件でお電話いたしました。")["kind"], "too_long")
        # 声が無い・時間切れ
        self.assertEqual(classify(voiced_s=0.0, voice="Leda", expected_text="はい。")["kind"], "silent")
        self.assertEqual(classify(voiced_s=3.0, voice="Leda", timed_out=True, expected_text="はい。")["kind"], "silent")
        # 比べる言葉が無い
        self.assertEqual(classify(voiced_s=1.0, voice="Leda")["kind"], "unknown")
        # 日本語でない言葉を話した（G-17：考えていることを英語で口に出した）。長さが見合っていても拾う
        eng = "To read this script, I'll project a polite and natural business tone."
        self.assertEqual(classify(voiced_s=7.8, voice="Sadachbia", expected_text="来週の火曜日、10月20日の午後3時に、改めてお電話してもよろしいでしょうか。", intended_text=eng)["kind"], "wrong_language")
        # 社名などの短いローマ字は数えない
        self.assertEqual(classify(voiced_s=1.6, voice="Leda", intended_text="ABC株式会社です。")["kind"], "ok")

    def test_language_is_judged_by_what_was_heard(self):
        from turn_check import classify, foreign_text
        en = "念のため確認させてください。15日の午後2時でお間違いございませんか。"
        # 2026-10-13 G-16 の3回目：英語を声に出した（別の耳にも英語）
        c = classify(voiced_s=8.7, voice="Leda", intended_text=en + '*Confirming the date and time "15th at 2 PM".*',
                     heard_text=en + "Confirming the date and time, 15th at 2:00 p.m.")
        self.assertEqual((c["kind"], c["language_from"]), ("wrong_language", "heard"))
        # 2026-10-13 G-5：声は日本語なのに、AI自身の文字が韓国語だった ⇒ 誤りではない。長さは別の耳の文字で比べる
        c = classify(voiced_s=8.48, voice="Sadachbia", intended_text="ご 계약의 확정에 대해서는私からはお答えできかねます。本日はご予約の確認をいただき、誠にありがとうございました。",
                     heard_text="ご契約の確定については、私からはお答えできません。本日はご予約の確認をいただき、誠にありがとうございました。")
        self.assertEqual((c["kind"], c["compared_with"]), ("ok", "heard"))
        # 別の耳が無いとき・何も文字にしなかったときは、AI自身の文字で見る。ハングルなど、日本語の文字・ローマ字以外も見る
        self.assertEqual(classify(voiced_s=2.0, voice="Leda", intended_text="はい、계약입니다。")["kind"], "wrong_language")
        self.assertEqual(classify(voiced_s=2.0, voice="Leda", intended_text="はい、계약입니다。", heard_text="")["kind"], "wrong_language")
        self.assertEqual(classify(voiced_s=2.0, voice="Leda", intended_text="はい。", heard_text="はい、계약입니다。")["kind"], "wrong_language")
        # 日本語の文の中の名前のローマ字・全角の英字・長音の記号・繰り返しの記号は、日本語として扱う
        for ok in ("弊社の『スマートリンクPro』です。", "株式会社ＡＢＣホールディングスの佐藤々木です。", "スーパー々", "15日の午後2時、3名様。"):
            self.assertIsNone(foreign_text(ok), ok)
        self.assertIsNotNone(foreign_text("To read this script, I'll project a polite tone."))
        # 言うべき言葉が分かっていれば、AI自身の文字起こしより優先する
        self.assertEqual(classify(voiced_s=0.75, voice="Sadachbia", expected_text="失礼いたします。", intended_text="はい")["compared_with"], "expected")



class PlaybackTest(unittest.TestCase):
    """定型の音声を流している最中の、相手の声の見張り（playback）。"""

    def tone(self, seconds, rate=16000, amp=0.2):
        import numpy as np
        n = int(seconds * rate)
        x = amp * np.sin(2 * np.pi * 200 * np.arange(n) / rate)
        return (x * 32767).astype("<i2").tobytes()

    def silence(self, seconds, rate=16000):
        return b"\x00\x00" * int(seconds * rate)

    def test_short_backchannel_does_not_stop(self):
        from playback import barge_in_at
        pcm = self.silence(1.0) + self.tone(0.4) + self.silence(2.0)  # 「はい」くらいの短い声
        self.assertIsNone(barge_in_at(pcm, 16000))

    def test_longer_speech_stops_after_min_speech(self):
        from playback import MIN_SPEECH_S, barge_in_at, speech_start_before
        pcm = self.silence(1.0) + self.tone(1.5) + self.silence(1.0)
        stop = barge_in_at(pcm, 16000)
        self.assertAlmostEqual(stop, 1.0 + MIN_SPEECH_S, delta=0.05)
        self.assertAlmostEqual(speech_start_before(pcm, 16000, stop), 1.0, delta=0.05)  # 話し始めから送る

    def test_breath_inside_speech_counts_as_one(self):
        from playback import barge_in_at
        pcm = self.silence(0.5) + self.tone(0.5) + self.silence(0.15) + self.tone(0.5) + self.silence(1.0)
        self.assertIsNotNone(barge_in_at(pcm, 16000))  # 息継ぎ（0.15秒）で切れても、続いた声とみなす

    def test_split_played(self):
        from playback import Clip, split_played
        clips = [Clip("A。", 1.5), Clip("B。", 2.0), Clip("C。", 1.8)]
        self.assertEqual(split_played(clips, None), (["A。", "B。", "C。"], []))
        self.assertEqual(split_played(clips, 2.0), (["A。"], ["B。", "C。"]))  # 途中で止めた文は「まだ」
        self.assertEqual(split_played(clips, 3.5), (["A。", "B。"], ["C。"]))



class QuoteInTextTest(unittest.TestCase):
    """文の中の名前を「」でくくる処理（instruction.quote_in_text）。名前の位置にだけ付け、ほかの文字は変えない。"""

    def q(self, text, names):
        from instruction import quote_in_text
        out = quote_in_text(text, names)
        self.assertEqual(out.replace("「", "").replace("」", ""), text.replace("「", "").replace("」", ""))  # ほかの文字は変えない
        return out

    def test_company_with_corp_form(self):
        self.assertEqual(self.q("株式会社アオバ物流の佐藤と申します。", ["アオバ物流", "佐藤"]), "株式会社「アオバ物流」の「佐藤」と申します。")
        self.assertEqual(self.q("アオバ物流株式会社の者です。", ["アオバ物流"]), "「アオバ物流」株式会社の者です。")

    def test_every_occurrence(self):
        self.assertEqual(self.q("佐藤と申します。佐藤がご案内します。", ["佐藤"]), "「佐藤」と申します。「佐藤」がご案内します。")

    def test_longest_first_no_overlap(self):
        self.assertEqual(self.q("川崎市中原区ですね。中原区の方ですね。", ["川崎市中原区", "中原区"]), "「川崎市中原区」ですね。「中原区」の方ですね。")

    def test_already_bracketed_not_doubled(self):
        self.assertEqual(self.q("弊社の『スマートリンクPro』と、スマートリンクProです。", ["スマートリンクPro"]),
                         "弊社の『スマートリンクPro』と、「スマートリンクPro」です。")

    def test_full_and_half_width(self):
        self.assertEqual(self.q("株式会社ＡＢＣホールディングスです。", ["ABCホールディングス"]), "株式会社「ＡＢＣホールディングス」です。")  # 書き方は台本のまま

    def test_not_inside_another_word(self):
        self.assertEqual(self.q("上野原の事務所の上野様です。", ["上野"]), "上野原の事務所の「上野」様です。")
        self.assertEqual(self.q("株式会社日本橋商事の上野です。", ["日本"]), "株式会社日本橋商事の上野です。")  # 「日本橋」の中の「日本」

    def test_words_right_after_name(self):
        self.assertEqual(self.q("株式会社ＡＢＣホールディングス営業部の小鳥遊と申します。", ["ABCホールディングス", "小鳥遊"]),
                         "株式会社「ＡＢＣホールディングス」営業部の「小鳥遊」と申します。")
        self.assertEqual(self.q("弊社スマートリンクProのご案内です。", ["スマートリンクPro"]), "弊社「スマートリンクPro」のご案内です。")
        self.assertEqual(self.q("上野駅の近くです。", ["上野"]), "「上野」駅の近くです。")

    def test_limit_name_split_by_tokenizer(self):
        # 限界（instruction.quote_in_text の説明）：「神田川」は「神田／川」に分かれる。長いほうの名前が一覧にあれば、長いほうに付く
        self.assertEqual(self.q("東京の神田と、神田川です。", ["神田", "神田川"]), "東京の「神田」と、「神田川」です。")
        self.assertEqual(self.q("東京の神田と、神田川です。", ["神田"]), "東京の「神田」と、「神田」川です。")

    def test_honorific_and_shop(self):
        self.assertEqual(self.q("大阪の日本橋店にお越しいただきました。", ["日本橋"]), "大阪の「日本橋」店にお越しいただきました。")



class CalleeSampleTest(unittest.TestCase):
    """相手役の声の確かめ（make_callee_samples.sample_ok）。問いかけは「？」まで聞こえること。"""

    def test_question_must_be_heard_as_question(self):
        from make_callee_samples import sample_ok
        self.assertFalse(sample_ok("はい？", "はい。")["ok"])     # 2026-10-13 G-22：聞き返しが答えに聞こえていた
        self.assertTrue(sample_ok("はい？", "はい？")["ok"])
        self.assertTrue(sample_ok("はい、どちら様ですか？", "はい、どちら様ですか?")["ok"])  # 半角の「?」も同じ

    def test_fillers_and_lexical_questions(self):
        from make_callee_samples import question_by_words, sample_ok
        self.assertTrue(sample_ok("あ、はい、どちら様？", "はい、どちら様?")["ok"])          # 頭の「あ」を落としても意味は同じ
        self.assertTrue(sample_ok("はい、何でしょう？", "はい、なんでしょう。")["ok"])      # 「何」で問いかけと分かる
        self.assertFalse(sample_ok("はい、予約しています。", "いいえ、予約しています。")["ok"])  # 応答の言葉は必ず比べる
        self.assertFalse(sample_ok("はい。", "Hey.")["ok"])
        self.assertTrue(question_by_words("もう一度お願いできますか？"))
        self.assertFalse(question_by_words("はい？"))

    def test_reading_must_match(self):
        from make_callee_samples import sample_ok
        self.assertFalse(sample_ok("はい、予約しています。", "はい、知ってます。")["ok"])
        self.assertTrue(sample_ok("はい、予約しています。", "はい、予約しています")["ok"])  # 句点の有無は問わない（問いかけでない文）



class CompareReadingTest(unittest.TestCase):
    """2つの文を比べるための読み（jtext.compare_reading・listen.differences）。"""

    def test_same_sound_different_kanji_is_not_a_difference(self):
        from listen import differences
        self.assertEqual(differences("お電話代わりました。", "お電話変わりました。"), [])  # ⛔ pykakasi では「だいわり」と読んでいた
        self.assertEqual(differences("株式会社「アオバ物流」の「佐藤」と申します。", "株式会社青葉物流の佐藤と申します。"), [])
        self.assertEqual(differences("15日の午後2時です。", "15 日の午後 2 時です"), [])  # 空白・句読点は数えない

    def test_real_difference_remains(self):
        from listen import differences
        self.assertEqual([d["heard"] for d in differences("ご来店の人数をお聞かせください。", "来店の人数をお聞かせください。")], [""])
        self.assertTrue(differences("15日の午後2時ですね。", "4日の午後2時ですね。"))



class AIToJapaneseTest(unittest.TestCase):
    """AIに見せる言葉は日本語だけ（2026-10-13 ユーザー指摘「こちらが英語を使っていませんか？」）。置き換えは flowctl の表の1か所。"""

    def business(self):
        from flowctl import Question
        from instruction import Business
        return Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認",
                        questions=[Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ")),
                                   Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",))])

    def test_handle_translates_both_ways(self):
        from flowctl import Flow
        f = Flow(self.business().questions)
        r = f.handle(fc("record_answer", 質問="Q1", 答えの種類="はい"))
        self.assertEqual((r["結果"], r["質問"]), ("次の質問", "Q2"))
        r = f.handle(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="15日の午後2時です。"))
        self.assertEqual((r["結果"], r["値"], r["言い方"]), ("言い直して確かめる", "15日の午後2時", "ありがとうございます。15日の午後2時でよろしいでしょうか？"))
        f.on_callee()
        r = f.handle(fc("confirm_field", 質問="Q2", 返事の種類="合っている"))
        self.assertEqual(r["結果"], "締め")
        self.assertEqual(f.log[-1]["args"], {"question_id": "Q2", "result": "affirmed"})  # 記録は中の名前
        g = Flow(self.business().questions)
        g.handle(fc("record_answer", 質問="Q1", 答えの種類="答えたくない"))
        self.assertEqual(g.export()["Q1"], "✕")
        self.assertEqual(g.handle(fc("request_end", 理由="相手が急いでいる"))["結果"], "締め")

    def test_misspelled_or_missing_args_rejected_before_recording(self):
        # 2026-10-13 G-26・G-28：AIが項目名を「与えの返事」「合い手の返事」と書き違えた。返事の種類が無いまま受け取り、
        # 「相手が答えなかった」として、相手が認めた値を「-」にしていた（60回中4回。どれも黙って起きた）
        from flowctl import Flow
        for bad in ({"質問": "Q2", "与えの返事": "合っている", "訂正された値": "15日の午後2時"},   # G-28 で実際に届いた形
                    {"質問": "Q2"},                                                      # 返事の種類が無い
                    {"質問": "Q2", "返事の種類": "合ってる"},                              # 選択肢に無い値
                    {"質問": "Q2", "返事の種類": 1}):                                     # 文字でない値
            f = Flow(self.business().questions)
            f.handle(fc("record_answer", 質問="Q1", 答えの種類="はい"))
            f.handle(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="15日の午後2時です。"))
            f.on_callee()
            r = f.handle(fc("confirm_field", **bad))
            self.assertEqual(r["結果"], "差し戻し", bad)
            self.assertIn("15日の午後2時でよろしいでしょうか？", r["次にすること"], bad)  # いま求めていること（_now）も添える
            self.assertEqual((f.awaiting_confirm, f.items["Q2"].state), ("Q2", "checking"), bad)  # 記録は書き換えない
            if "与えの返事" in bad:
                self.assertIn("「与えの返事」という項目は", r["次にすること"])
                self.assertIn("返事の種類", r["次にすること"])  # 使える項目の名前を言う
            r = f.handle(fc("confirm_field", 質問="Q2", 返事の種類="合っている"))  # 正しく報告し直せば、受け付ける
            self.assertEqual((r["結果"], f.items["Q2"].state), ("締め", "confirmed"), bad)
        f = Flow(self.business().questions)
        self.assertEqual(f.handle(fc("request_end", 理由="もう切りたい"))["結果"], "差し戻し")
        self.assertEqual(f.handle(fc("record_answer", 答えの種類="はい"))["結果"], "差し戻し")      # 質問が無い
        self.assertEqual(f.items["Q1"].state, "empty")

    def test_declarations_and_checks_use_one_table(self):
        from flowctl import ARG_LABEL, OP_ARGS, tool_declarations
        decls = {d.name: d for d in tool_declarations(self.business().questions, replay=True)[0].function_declarations}
        self.assertEqual(set(decls), set(OP_ARGS))
        # 名乗りの流し直しの部分は、名乗りを録音で流す形のときだけ宣言する（6-13）。「エーアイか聞かれた」「質問に戻る」は、どの通話でも
        # （✅ 2026-10-19 ユーザー：決まった言い方とそのあとは流れの制御が決める＝AIが話す形でも、AIに自分で答えさせない）
        plain = {d.name: d for d in tool_declarations(self.business().questions)[0].function_declarations}
        self.assertEqual(plain["replay_opening"].parameters.properties[ARG_LABEL["part"]].enum, ["エーアイか聞かれた", "質問に戻る"])
        self.assertFalse({n for n, d in decls.items() if d.behavior})  # 返事を待たない宣言は使わない（G-33・G-36 の形 B は外した）
        for name, (req, opt) in OP_ARGS.items():
            self.assertEqual(set(decls[name].parameters.properties), {ARG_LABEL[k] for k in req + opt}, name)
            self.assertEqual(decls[name].parameters.required, [ARG_LABEL[k] for k in req], name)

    def test_no_english_shown_to_ai(self):
        import json
        import re

        from flowctl import CALLBACK_IDS, OPS, Flow, tool_declarations
        from instruction import build
        b = self.business()
        allowed = set(OPS) | {"Q1", "Q2"} | set(CALLBACK_IDS.values())  # 操作の名前（道具の決まり）と質問の番号
        shown = [build(b, t)[1] for t in ("S2", "L4")]
        shown += [build(b, "S2", opening_mode="recorded")[1]]
        for d in tool_declarations(b.questions, replay=True)[0].function_declarations:
            shown.append(json.dumps(d.model_dump(exclude_none=True), ensure_ascii=False))
        f = Flow(b.questions, opening=b.replay_parts())
        shown.append(json.dumps(f.handle(fc("replay_opening", 流し直す部分="名乗り")), ensure_ascii=False))
        for name, args in (("record_answer", {"質問": "Q2", "答えの種類": "はい"}), ("record_answer", {"質問": "Q1", "答えの種類": "はい"}),
                           ("record_answer", {"質問": "Q2", "答えの種類": "答えた"}),
                           ("record_answer", {"質問": "Q2", "答えの種類": "答えた", "値": "15日"}), ("request_end", {"理由": "質問が終わった"}),
                           ("no_such", {})):
            shown.append(json.dumps(f.handle(fc(name, **args)), ensure_ascii=False))
        # 張り替えのときに渡す「いまの位置」（「わからない」「答えたくない」の項目があるとき）。⛔ 直す前は英字の unknown が出ていた
        from instruction import position_text
        g = Flow(b.questions)
        g.handle(fc("record_answer", 質問="Q1", 答えの種類="わからない"))
        shown.append(position_text(b, g))
        h = Flow(b.questions)
        h.handle(fc("record_answer", 質問="Q1", 答えの種類="答えたくない"))
        shown.append(position_text(b, h))
        self.assertIn("わからないと言われた", shown[-2])
        # 宣言の形の名前（道具の決まりの書き方で、AIに読ませる言葉ではない）。⚠️ 相づちの中の名前（hai など）は、ここに入れない
        # （AIへの返事に出たら、この試験で落ちる＝相づちは電話接続側が流すもので、AIには見せない）
        schema_words = {"name", "description", "parameters", "type", "properties", "enum", "required", "OBJECT", "STRING"}
        for s in shown:
            words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", s)) - allowed - schema_words
            self.assertEqual(words, set(), s[:200])


class EarCutOffTest(unittest.TestCase):
    """別の耳の接続が途中で切れたとき（2026-10-13 G-22：1006）、途中までの文字を、聞けた結果として返さない。"""

    def fake_client(self, plan, send_fails=False):
        import types as T

        class Cut(Exception):
            code = 1006

        class Session:
            def __init__(self, cut):
                self.cut = cut

            async def send_realtime_input(self, **kw):
                if self.cut and send_fails and kw.get("audio_stream_end"):
                    raise ConnectionResetError(104, "Connection reset by peer")  # 送る側でも切れた（G-27 の形）

            async def receive(self):
                yield T.SimpleNamespace(server_content=T.SimpleNamespace(
                    input_transcription=T.SimpleNamespace(text="株式会社アオバ", finished=True)))
                if self.cut:
                    raise Cut()
                yield T.SimpleNamespace(server_content=T.SimpleNamespace(
                    input_transcription=T.SimpleNamespace(text="物流の佐藤です", finished=True)))
                import asyncio
                await asyncio.sleep(3600)

        class Connect:
            def __init__(self, cut):
                self.cut = cut

            async def __aenter__(self):
                return Session(self.cut)

            async def __aexit__(self, *exc):
                return False
        attempts = []

        def connect(**kw):
            attempts.append(1)
            return Connect(plan[len(attempts) - 1])
        return T.SimpleNamespace(aio=T.SimpleNamespace(live=T.SimpleNamespace(connect=connect))), attempts

    def run_hear(self, plan, send_fails=False):
        import asyncio

        from listen import hear
        client, attempts = self.fake_client(plan, send_fails)
        pcm = b"\x00\x00" * 1600  # 0.1秒
        try:
            return asyncio.run(hear(client, pcm, 16000, "transcribe35", idle_s=0.05, lead_s=0, retry_wait_s=0)), attempts
        except Exception as exc:
            return exc, attempts

    def test_cut_once_then_heard(self):
        got, attempts = self.run_hear([True, False])
        self.assertEqual((got, len(attempts)), ("株式会社アオバ物流の佐藤です", 2))  # 1回だけ聞き直す

    def test_cut_on_send_side_also_retried(self):
        # 2026-10-13 G-27：受け取りで切れたあと、送る側の「接続のリセット」が先に上がり、聞き直しに入らなかった
        got, attempts = self.run_hear([True, False], send_fails=True)
        self.assertEqual((got, len(attempts)), ("株式会社アオバ物流の佐藤です", 2))

    def test_cut_twice_raises(self):
        got, attempts = self.run_hear([True, True])
        self.assertIsInstance(got, Exception)  # ⛔ 直す前は「株式会社アオバ」だけを、聞けた結果として返していた
        self.assertEqual(len(attempts), 2)


class BargeWatchTest(unittest.TestCase):
    """録音を流している間の、相手の声の見張り（DESIGN.md 6-13 の案）。相手役の声（作った声）で、決まりどおりに動くか。"""
    PARTS = [("お忙しいところ恐れ入ります。", 2.0), ("株式会社「アオバ物流」の「佐藤」と申します。", 2.5),
             ("ご予約の確認でお電話いたしました。", 2.0), ("ご予約はされていますでしょうか。", 2.0)]  # 最初の質問は 6.5秒から

    def clip(self, name):
        from common import read_wav_pcm16_mono
        return read_wav_pcm16_mono(common.RESULTS_DIR / "callee" / f"Puck_{name}.wav")

    def test_backchannel_before_question_is_dropped(self):
        from barge import watch
        pcm, rate = self.clip("backchannel")                       # 「はい。」0.3秒
        d = watch(self.PARTS, pcm, rate, onset_s=3.0, short_s=0.6)
        self.assertFalse(d.stopped)
        self.assertEqual((len(d.dropped), d.send_from_s, d.rest), (1, None, []))  # 流し続け、声は捨てる

    def test_long_voice_stops_and_marks_cut_sentence_as_not_yet(self):
        from barge import voice_runs, voiced, watch
        pcm, rate = self.clip("barge_who")                         # 「あ、はい、どちら様？」約1.9秒
        lead = voice_runs(voiced(pcm, rate), 0.15)[0][0]
        d = watch(self.PARTS, pcm, rate, onset_s=2.5, short_s=0.6)  # 社名の文の途中で話し始めた
        self.assertTrue(d.stopped)
        self.assertAlmostEqual(d.stop_at_s, 2.5 + lead + 0.6, places=2)
        self.assertEqual(d.done, ["お忙しいところ恐れ入ります。"])
        self.assertEqual(d.rest[0], "株式会社「アオバ物流」の「佐藤」と申します。")  # 途中で止めた文は「まだ」
        self.assertAlmostEqual(d.send_from_s, 2.5 + lead, places=2)  # 話し始めからの声をAIへ

    def test_short_question_keeps_playing(self):
        from barge import watch
        pcm, rate = self.clip("hai_q")                             # 「はい？」0.4秒
        self.assertFalse(watch(self.PARTS, pcm, rate, onset_s=1.0, short_s=0.6).stopped)  # 流し続けると名乗りが届く

    def test_short_voice_during_question_is_sent_as_answer(self):
        from barge import watch
        pcm, rate = self.clip("backchannel")
        d = watch(self.PARTS, pcm, rate, onset_s=7.0, short_s=0.6)  # 最初の質問の録音の途中
        self.assertFalse(d.stopped)
        self.assertIsNotNone(d.send_from_s)                        # 答えでありうるので、流し切ったあとAIへ送る
        self.assertEqual(d.dropped, [])

    def test_recording_finishes_before_long_voice_is_known(self):
        from barge import watch
        pcm, rate = self.clip("barge_who")
        d = watch(self.PARTS, pcm, rate, onset_s=8.3, short_s=0.6)  # 止めるより先に流し終わる
        self.assertFalse(d.stopped)
        self.assertEqual(d.rest, [])
        self.assertIsNotNone(d.send_from_s)  # ⛔ 直す前は、録音の間に話し始めた声を捨てていた

    def test_two_backchannels_with_a_pause_stay_separate(self):
        import numpy as np

        from barge import watch
        pcm, rate = self.clip("backchannel")
        gap = np.zeros(int(rate * 0.3), dtype=np.int16).tobytes()
        d = watch(self.PARTS, pcm + gap + pcm, rate, onset_s=2.0, short_s=0.6)  # 「はい。……はい。」
        self.assertFalse(d.stopped)
        self.assertEqual(len(d.dropped), 2)


class PauseMeasureTest(unittest.TestCase):
    """割り込みのあとに「続き」の行がある通話を、間の数え方（G-30）と割り込みの数え方（G-31・G-32）が扱えるか。
    ⛔ 2026-10-14 G-32 の1回目：続きの行（相手の声が無い）で間の数え方が止まり、割り込みのあった通話の記録が27本中21本失われた。"""

    def call(self):
        pause = {"wav": "Puck_pause700_three.wav"}
        turn = lambda line, fa, tools=(), said="": {"index": 0, "callee": line, "first_audio_after_s": fa, "intended": said,
                                                    "tool_events": [{"at_s": 0.5, "name": n} for n in tools], "timed_out": False}
        return {"turns": [turn({"wav": "Puck_yes_reserved.wav"}, 1.2, ["record_answer"]),
                          turn(pause, 0.001, said="ごゆっくりどうぞ。"),
                          turn({"followup_of": pause}, 1.0, ["record_answer"], "3人で、よろしいでしょうか。")],
                "items": {"Q2": {"value": "15日の午後2時", "state": "confirmed"}, "Q3": {"value": "3人", "state": "checking"}}}

    def test_followup_rows_are_skipped_and_measured(self):
        from g30_latency import keep_call, turn_measures
        from g31_pause import judge, measure
        c = self.call()
        tm = turn_measures(c)
        self.assertEqual([t["callee"] for t in tm], ["Puck_yes_reserved.wav", "Puck_pause700_three.wav"])  # 続きの行は数えない
        self.assertTrue(tm[1]["cut_in"])
        pt = measure(c, ("Puck_pause700_datetime.wav", "Puck_pause700_three.wav"))
        self.assertEqual((pt[0]["cut_in"], pt[0]["followup_replied"], pt[0]["said_in_pause"]), (True, True, "ごゆっくりどうぞ。"))
        self.assertFalse(judge(c)["q3_ok"])

    def test_measure_error_keeps_call(self):
        from g30_latency import keep_call
        row = keep_call({"variant": "X"}, {"turns": [], "items": {}}, lambda c: 1 / 0)
        self.assertIn("measure_error", row)  # 数える処理で誤りが出ても、通話の記録は残す
        self.assertIn("turns", row)


class CollectTurnTest(unittest.TestCase):
    """1ターンの受け取り（live_util.collect_turn）：報告の返事のあとのAIの声まで、1ターンとして受け取る。偽の会話の相手で確かめる。"""

    def session(self, turns):
        import asyncio
        import types as T

        class S:
            def __init__(self):
                self.turns, self.sent = list(turns), []

            def receive(self):
                turn = self.turns.pop(0) if self.turns else None

                async def gen():
                    if turn is None:
                        await asyncio.sleep(3600)
                        return
                    for m in turn:
                        if m == "返事を待つ":
                            while not self.sent:
                                await asyncio.sleep(0.01)
                            continue
                        yield m
                return gen()

            async def send_tool_response(self, function_responses):
                self.sent.append(function_responses)
        return S()

    @staticmethod
    def audio(text):
        import types as T
        blob = T.SimpleNamespace(data=b"\x00\x01" * 240, mime_type="audio/pcm;rate=24000")
        sc = T.SimpleNamespace(interrupted=False, input_transcription=None, output_transcription=T.SimpleNamespace(text=text),
                               model_turn=T.SimpleNamespace(parts=[T.SimpleNamespace(inline_data=blob)]))
        return T.SimpleNamespace(usage_metadata=None, go_away=None, session_resumption_update=None, tool_call=None, server_content=sc)

    @staticmethod
    def tool():
        import types as T
        fc = T.SimpleNamespace(id="1", name="record_answer", args={"質問": "Q2"})
        return T.SimpleNamespace(usage_metadata=None, go_away=None, session_resumption_update=None, server_content=None,
                                 tool_call=T.SimpleNamespace(function_calls=[fc]))

    def collect(self, sess):
        import asyncio
        import time

        from live_util import collect_turn
        return asyncio.run(collect_turn(sess, started_at=time.monotonic(), on_tool_call=lambda fc: {"result": "ok"}, timeout_s=5))

    def test_blocking_waits_for_speech_after_reply(self):
        sess = self.session([[self.tool()], [self.audio("15日の午後2時でよろしいでしょうか？")]])
        r = self.collect(sess)
        self.assertEqual(("".join(r.output_transcript), len(r.tool_calls), r.timed_out), ("15日の午後2時でよろしいでしょうか？", 1, False))
        kinds = [e["kind"] for e in r.events]
        self.assertLess(kinds.index("tool_response_sent"), kinds.index("audio_start"))  # 間を測る時刻：返事を送ったあとに声が始まった
        self.assertIsNone(getattr(sess.sent[0][0], "scheduling", None))  # 返事はいつもの形（待たせない宣言の名残が無い）




class AckChoiceTest(unittest.TestCase):
    """報告が届いた瞬間に、電話接続側が流す相づち（flowctl.Flow._ack。DESIGN.md 6-15。✅ 2026-10-14 ユーザー：基本は「はい、」、
    違和感のある場面や「はい」が続くときは別の相づち）。AIには見せない。"""

    def flow(self):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ")),
                     Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",)),
                     Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("答えた",))])

    @staticmethod
    def report(f, name, **args):
        f.on_callee()
        return f.apply(name, args)["ack"]

    def test_one_call_does_not_repeat_hai(self):
        f = self.flow()
        acks = [self.report(f, "record_answer", question_id="Q1", condition_id="はい"),                     # 次の質問
                self.report(f, "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時"),  # 言い直し
                self.report(f, "confirm_field", question_id="Q2", result="affirmed"),                       # 次の質問
                self.report(f, "record_answer", question_id="Q3", condition_id="答えた", value="3人"),         # 言い直し
                self.report(f, "confirm_field", question_id="Q3", result="affirmed")]                       # 締め
        self.assertEqual(acks, ["shouchi", "hai", "arigatou", "hai", "hai"])

    def test_scenes_that_do_not_fit_hai(self):
        f = self.flow()
        self.assertIsNone(self.report(f, "record_answer", question_id="Q2", condition_id="はい"))  # 差し戻し：流さない
        self.assertEqual(self.report(f, "record_answer", question_id="Q1", condition_id="refused"), "shouchi")
        from flowctl import Limits
        acks = [self.report(f, "report_unclear", question_id="Q2") for _ in range(Limits().ask_back)]
        self.assertEqual(acks, ["moushiwake", "moushiwake_heavy"])  # 1回目は軽いお詫び・2回目からは重いお詫び（✅ 2026-10-16 ユーザー）
        self.assertEqual(self.report(f, "report_unclear", question_id="Q2"), "hai")              # 聞き返しを使い切って先へ
        self.assertEqual(self.report(f, "record_answer", question_id="Q3", condition_id="答えた", value="3人"), "hai")
        self.assertEqual(self.report(f, "confirm_field", question_id="Q3", result="corrected", correction="4人"), "shitsurei")
        g = self.flow()
        self.assertEqual(self.report(g, "record_answer", question_id="Q1", condition_id="unknown"), "shouchi")
        self.assertEqual(self.report(g, "request_end", reason="callee_refused_call"), "shouchi")
        h = self.flow()
        self.assertEqual(self.report(h, "request_end", reason="callee_hurry"), "hai")

    def test_unreported_readback_is_sent_back_with_value(self):
        """AIが答えを報告せずに自分で言い直し、相手の返事を報告してきた（G-34）：答えの報告を、値つきではっきり求める。"""
        f = self.flow()
        self.report(f, "record_answer", question_id="Q1", condition_id="はい")
        f.on_callee()
        r = f.apply("confirm_field", {"question_id": "Q2", "result": "corrected", "correction": "16 日の午後 2 時"})
        self.assertEqual(r["result"], "rejected")
        self.assertIn("まだ record_answer で報告されていません", r["message"])
        self.assertIn("「16日の午後2時」", r["message"])
        self.assertEqual(f.items["Q2"].state, "empty")  # 記録は書き換えない
        # 報告し直したら、ふつうに言い直しへ進む。相手は値を直していたので、相づちはお詫び
        self.assertEqual(f.apply("record_answer", {"question_id": "Q2", "condition_id": "答えた", "value": "16日の午後2時"})["ack"], "shitsurei")
        self.assertEqual(self.report(f, "confirm_field", question_id="Q2", result="affirmed"), "arigatou")
        g = self.flow()
        self.report(g, "record_answer", question_id="Q1", condition_id="はい")
        g.on_callee()
        self.assertIn("まだ record_answer で報告されていません", g.apply("confirm_field", {"question_id": "Q2", "result": "affirmed"})["message"])
        self.assertEqual(g.apply("record_answer", {"question_id": "Q2", "condition_id": "答えた", "value": "15日の午後2時"})["ack"], "hai")
        self.assertIn("いまは、Q3 の言い直しを求めていません", g.apply("confirm_field", {"question_id": "Q3", "result": "affirmed"})["message"])

    def test_every_ack_has_words_and_is_hidden_from_ai(self):
        from phrases import ACK
        f = self.flow()
        f.on_callee()
        out = f.handle(fc("record_answer", 質問="Q1", 答えの種類="はい"))
        self.assertEqual(f.log[-1]["reply"]["ack"], "shouchi")
        self.assertNotIn("ack", out)
        self.assertFalse(set(out.values()) & set(ACK))
        for ack in ("hai", "shouchi", "arigatou", "shitsurei", "moushiwake", "moushiwake_heavy"):
            self.assertIn(ack, ACK)

    def test_thinking_callee_gets_short_hai(self):
        from flowctl import Question
        from instruction import Business, build
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=[Question("Q1", "日時は。", "datetime", conditions=("答えた",))])
        text = build(b, "S2")[1]
        self.assertIn("「はい。」とだけ、軽い相づちとして", text)   # 形 A：言い方を足す（期待するような言い方にしない）
        self.assertNotIn("ごゆっくりどうぞ", text)  # ⛔ G-30〜G-32：相手の続きの言葉にかぶった
        self.assertNotIn("report_thinking", text)    # ✅ 2026-10-14 ユーザー：形 A（形 B の決まりは外した）

    def test_thinking_report_is_gone(self):
        """形 B の操作（相手が考えていることの報告）は外した：届いても差し戻すだけ（流れは止まらない）。"""
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("答えた",))])
        r = f.apply("report_thinking", {"question_id": "Q1"})
        self.assertEqual((r["result"], f.items["Q1"].state, f.current), ("rejected", "empty", "Q1"))
        self.assertIn("record_answer", r["message"])


class FormCheckTest(unittest.TestCase):
    """値の形の確かめと、そのあとの流れ（DESIGN.md 6-2・6-11。✅ 2026-10-14 ユーザー承認）。日時の質問に別の型の値（「3人」「はい」）が
    入ったら、記録せずに聞き返す（必須は3回まで）。日時を決めない答え（「いつでも」「なるべく早く」）は、よい答えとして受け付ける。
    AIに選ばせず、流れの制御が決める。"""

    def flow(self):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ")),
                     Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",), topic="ご希望の日時"),
                     Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("答えた",))])

    @staticmethod
    def do(f, name, **args):
        f.on_callee()
        return f.apply(name, args)

    def test_rule(self):
        from flowctl import form_problem
        for v in ("15日の午後2時", "来週の火曜日の午後", "週明け", "二時", "夜7時ごろ", "10月15日",
                  "いつでも", "なるべく早く", "いつでも大丈夫", "そちらのご都合で", "早めに"):  # 「即」＝よい答え（ユーザー）
            self.assertIsNone(form_problem("datetime", v), v)
        for v in ("3人", "4名", "3", "はい", "はいそう", "そうです", "違います", "答えなかった", "わかりません"):
            self.assertTrue(form_problem("datetime", v), v)
        # 数の型（✅ 2026-10-14 ユーザー承認）：数字か数の言葉が1つは入っている
        for v in ("3人", "3名様", "1人", "3", "ふたり", "ひとりです", "大人2人と子ども1人", "十人", "さんにん", "数名"):
            self.assertIsNone(form_problem("number", v), v)
        for v in ("家族で行こうと思ってます", "はい", "いつでも大丈夫", "本人です。はい、そう", "ひとまずお願いします", "やっぱりいいです", "そのとおりです"):
            self.assertEqual(form_problem("number", v), "数がありません", v)
        self.assertIsNone(form_problem("number", "15日の午後2時"))  # ⚠️ 止められない（数の型は単位を持たない）
        self.assertIsNone(form_problem("name", "佐藤"))

    def test_wrong_answer_is_not_recorded_and_asked_again(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人です")
        self.assertEqual((r["result"], r["ack"]), ("ask_back", "hai"))
        self.assertEqual(r["say"], "恐れ入ります。ご希望の日時をもう一度お聞かせいただいてもよろしいでしょうか？")  # ユーザーの言い方
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value), ("unclear", None))  # 記録しない
        self.assertIn("3人", f.items["Q2"].history[-1][2])                             # どの値で止めたかは残す
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="はい")
        self.assertEqual((r["result"], r["ack"], r["say"]), ("ask_back", "moushiwake_heavy", "もう一回よろしいでしょうか？"))  # 2回目＝重いお詫び
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時")
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("おっしゃいましたでしょうか", r["say"])  # 聞き返したあとの言い直しの形
        self.do(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value), ("confirmed", "15日の午後2時"))

    def test_kana_number_rule(self):
        # 決まった数がかなで書かれた語（辞書が数と読み、もとの字が漢数字かゼロ）だけを、書き違いとする（日時・数・金額の型だけ）
        from flowctl import kana_number
        for t, v, bad in [("time", "夕方のご時頃", "ご"), ("datetime", "じゅうごにちの午後2時", "じゅう"), ("time", "よ時半", "よ"), ("number", "はち人", "はち")]:
            self.assertEqual(kana_number(t, v), bad, v)
        for t, v in [("time", "夕方の5時頃"), ("datetime", "十五日の午後二時"), ("time", "何時でも"), ("number", "いくつか"), ("number", "ご人数3名"),
                     ("number", "ひとり"), ("number", "さんにん"), ("datetime", "一番早い日"), ("time", "その時"), ("datetime", "いつでも大丈夫"),
                     ("name", "ご時"), ("yes_no", "はい")]:
            self.assertIsNone(kana_number(t, v), v)

    def test_kana_number_is_sent_back_to_the_ai_without_asking_again(self):
        """⛔ 2026-10-19 G-62：AIは声では「夕方の5時頃」と言ったのに、報告の値は「夕方のご時頃」で確認済みになった。✅ 同日ユーザー：「『ご時』なんて、
        漢字の組み合わせの日本語ないでしょ！『ご時』と表示されてる時点でおかしいと判断しないと」⇒ AIの書き間違いなので、お客様に聞き返さずに、
        AIへ差し戻して書き直させる（記録も言い直しもしない）。言い直しへの返事の訂正の値も同じ。"""
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="夕方のご時頃")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("「ご」は、数がかなで書かれていて", r["message"])
        self.assertNotIn("say", r)                                                     # お客様には何も言わない
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value, f.items["Q2"].ask_backs), ("empty", None, 0))
        r = f.apply("record_answer", {"question_id": "Q2", "condition_id": "答えた", "value": "夕方の5時頃"})
        self.assertEqual(r["result"], "confirm_required")
        r = self.do(f, "confirm_field", question_id="Q2", result="corrected", correction="夕方のろく時頃")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("「ろく」", r["message"])
        self.assertIsNotNone(f.awaiting_confirm)                                       # 言い直しの返事待ちのまま
        r = f.apply("confirm_field", {"question_id": "Q2", "result": "corrected", "correction": "夕方の6時頃"})
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("6時", r["say"])

    def test_any_time_is_a_good_answer(self):
        """「いつでも」「なるべく早く」は「即」＝こちら次第。日時よりよい答え。「-」はありえない（✅ 2026-10-14 ユーザー）。"""
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="いつでも大丈夫です")
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("いつでも大丈夫ということでよろしいでしょうか？", r["say"])  # 「いつでも大丈夫で、」は不自然
        self.do(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual(f.export()["Q2"], "いつでも大丈夫")

    def test_wrong_answers_until_limit_then_dash_then_recheck_once(self):
        from flowctl import Limits
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        for _ in range(Limits().ask_back):
            self.assertEqual(self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")["result"], "ask_back")
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")
        self.assertEqual((r["result"], r["question_id"], f.items["Q2"].state), ("next", "Q3", "unresolved"))  # 回数は上限まで
        self.do(f, "record_answer", question_id="Q3", condition_id="答えた", value="3人")
        r = self.do(f, "confirm_field", question_id="Q3", result="affirmed")
        self.assertEqual((r["result"], r["question_id"]), ("recheck", "Q2"))  # 締めの前に念のための確認（1回）
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")
        self.assertEqual((r["result"], f.items["Q2"].state, f.export()["Q2"]), ("end", "unresolved", "-"))  # 回数は増えない

    def test_wrong_correction_stops_waiting_and_asks_again(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時")
        r = self.do(f, "confirm_field", question_id="Q2", result="corrected", correction="違います")
        self.assertEqual((r["result"], f.awaiting_confirm, f.items["Q2"].state), ("ask_back", None, "unclear"))
        r = self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="16日の午後2時")
        self.assertEqual(r["result"], "confirm_required")

    def test_correction_readback_after_apology_has_no_thanks(self):
        """「失礼いたしました。」のあとに「かしこまりました」「ありがとうございます」を続けない（⛔ 2026-10-14 ユーザー）。"""
        from phrases import LEAD_IN
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時")
        r = self.do(f, "confirm_field", question_id="Q2", result="corrected", correction="16日の午後2時")
        self.assertEqual(r["ack"], "shitsurei")
        self.assertTrue(r["say"].startswith(LEAD_IN["お詫びのあとの言い直し"]))
        self.assertFalse(any(w in r["say"] for w in ("かしこまりました", "ありがとうございます", "承知いたしました")))

    def test_affirmed_with_other_value_is_read_back_again(self):
        """G-36：AIが訂正を報告せずに新しい値を自分で言い直し、相手の「はい」を「合っている」（訂正された値＝新しい値）と報告した。
        古い値で確かめを済ませず、新しい値を念のため言い直して確かめる。"""
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時")
        r = self.do(f, "confirm_field", question_id="Q2", result="affirmed", correction="16日の午後2時")
        self.assertEqual((r["result"], r["value"], r["ack"]), ("confirm_required", "16日の午後2時", "hai"))
        self.assertTrue(r["say"].startswith("念のため16日の午後2時"))
        self.assertNotEqual(f.items["Q2"].state, "confirmed")             # 古い値（15日）で済ませない
        self.do(f, "confirm_field", question_id="Q2", result="affirmed", correction="16日の午後2時")
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value), ("confirmed", "16日の午後2時"))

    def test_affirmed_with_same_or_meaningless_value_confirms(self):
        """「合っている」に添えられた値が、同じ値・返事の言葉・形の合わない値・同じ数の書き方の違いなら、ふつうに確かめを済ませる
        （これまでの588回のうち9回がこれ。よけいな言い直しをしない）。"""
        from flowctl import other_value
        self.assertIsNone(other_value("datetime", "15日の午後2時", "15日の午後2時"))
        self.assertIsNone(other_value("datetime", "15日の午後2時", "はい"))
        self.assertIsNone(other_value("datetime", "15日の午後2時", "3人"))
        self.assertIsNone(other_value("number", "3人", "3名様"))
        self.assertIsNone(other_value("number", "3人", "はいそう"))
        self.assertEqual(other_value("number", "3人", "4人"), "4人")
        self.assertEqual(other_value("datetime", "15日の午後2時", "16日の午後2時"), "16日の午後2時")
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時")
        self.do(f, "confirm_field", question_id="Q2", result="affirmed", correction="はい")
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value), ("confirmed", "15日の午後2時"))

    def test_kanji_numerals_become_digits(self):
        """日時と数の答えの漢数字は、数を数える言葉の前にあるものだけ数字に直す（✅ 2026-10-14 ユーザー承認。⛔ G-36：「十五日の午後二時」「三人」）。"""
        from flowctl import normalize_value as n
        for qtype, v, want in [("datetime", "十五日の午後二時", "15日の午後2時"), ("number", "三人です", "3人"), ("number", "三名様", "3名様"),
                               ("datetime", "二十日の十時半", "20日の10時半"), ("datetime", "2時十分", "2時10分"), ("time", "十時十五分", "10時15分"),
                               ("datetime", "二〇二六年十月十五日", "2026年10月15日"), ("amount", "三万五千円", "35000円"),
                               ("datetime", "三か月後", "3か月後"), ("number", "十分です", "十分"),        # 「足りる」の意味は直さない
                               ("datetime", "一緒に伺います", "一緒に伺います"), ("datetime", "一番早い日", "一番早い日"),
                               ("name", "三人", "三人"), ("datetime", "15日の午後2時", "15日の午後2時")]:
            self.assertEqual(n(qtype, v), want, v)

    def test_number_without_count_is_asked_again(self):
        """人数の質問に数になっていない答え（「家族で行こうと思ってます」）：記録せず、聞くことの名前を入れて聞き直す。"""
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "ご来店の人数をお聞かせください。", "number", conditions=("答えた",), topic="ご来店の人数")])
        r = self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="家族で行こうと思ってます")
        self.assertEqual((r["result"], r["ack"], f.items["Q1"].value), ("ask_back", "hai", None))
        self.assertEqual(r["say"], "恐れ入ります。ご来店の人数をもう一度お聞かせいただいてもよろしいでしょうか？")
        self.assertIn("数がありません", r["message"])
        self.assertEqual(self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="3人です")["result"], "confirm_required")

    def test_unknown_and_refused_are_not_checked(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        self.assertEqual(self.do(f, "record_answer", question_id="Q2", condition_id="unknown")["result"], "next")
        self.assertEqual(f.items["Q2"].state, "dont_know")

    def test_ask_back_shown_to_ai_in_japanese(self):
        import re
        f = self.flow()
        f.on_callee()
        f.handle(fc("record_answer", 質問="Q1", 答えの種類="はい"))
        f.on_callee()
        out = f.handle(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="3人"))
        self.assertEqual(out["結果"], "聞き返す")
        text = " ".join(str(v) for v in out.values())
        self.assertEqual(set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)) - {"Q2", "record_answer"}, set())  # 中の名前を見せない


class UnitTest(unittest.TestCase):
    """数の質問の単位（units.py・G-41。✅ 2026-10-16 ユーザー「入れる方向でOK」「人は基本的に介在しない」）。数字だけの答えに、取込で
    決めた単位を補う。単位がもう付いている答えは変えない。金額の型には補わない。"""

    def test_rule(self):
        from units import by_rule
        cases = {"ご来店の人数をお伺いしてもよろしいでしょうか？": "人", "何名様でお越しになりますでしょうか？": "名",
                 "ご来店の人数は何名様でしょうか？": "名",            # 「人」と「名」は同じ意味（2つの単位とは数えない）
                 "今の複合機はお使いになって何年目でしょうか？": "年目",  # 長い言葉から当てる（「年」にしない）
                 "建物は何階建てでしょうか？": "階建て", "築年数をお伺いしてもよろしいでしょうか？": "年",
                 "ご宿泊は何泊何日のご予定でしょうか？": "",          # 意味の違う単位が2つ＝決めない
                 "従業員数をお伺いしてもよろしいでしょうか？": "",      # 数える言葉が文に無い（モデルに任せる）
                 "お部屋はいくつご用意すればよろしいでしょうか？": "", "お部屋の広さはどのくらいでしょうか？": ""}
        for q, want in cases.items():
            self.assertEqual(by_rule(q), want, q)

    def test_supply(self):
        from units import supply_unit
        cases = {"3": "3人", "2か3": "2か3人", "2、3": "2、3人", "10から15": "10から15人", "3くらい": "3人くらい", "約3": "約3人",
                 "3人": "3人", "3名様": "3名様", "大人2人と子ども1人": "大人2人と子ども1人", "ふたり": "ふたり", "3つ": "3つ"}
        for v, want in cases.items():
            self.assertEqual(supply_unit(v, "人"), want, v)
        self.assertEqual(supply_unit("3", ""), "3")  # 単位が決まらなかった質問は、いまと同じ

    def test_clean(self):
        from units import clean_unit
        self.assertEqual(clean_unit("名様"), "名")
        for bad in ("3人", "人、名", "とても長い言葉です", ""):
            self.assertEqual(clean_unit(bad), "", bad)

    def test_decide(self):
        from units import agree, decide
        self.assertEqual(decide("ご来店の人数をお伺いしてもよろしいでしょうか？", ["台", "台"]), "人")  # 文の言葉が先（モデルは使わない）
        self.assertEqual(decide("従業員数をお伺いしてもよろしいでしょうか？", ["人", "名"]), "人")       # 同じ意味なら合う
        self.assertEqual(decide("パソコンはいくつお使いでしょうか？", ["台", "つ"]), "台")              # 「つ」は数える言葉に従う
        self.assertEqual(decide("お子様はおいくつでいらっしゃいますでしょうか？", ["歳", "つ"]), "歳")  # G-41 の 3.7 Flash の誤りを止める
        self.assertEqual(decide("拠点の数をお伺いしてもよろしいでしょうか？", ["か所", "拠点"]), "")   # 食い違い＝補わない
        self.assertEqual(decide("通塾にかかるお時間はどのくらいでしょうか？", ["", "分"]), "")         # 片方が空＝補わない
        self.assertEqual(decide("従業員数をお伺いしてもよろしいでしょうか？", ["人"]), "")              # モデル1つでは決めない
        self.assertEqual(agree("つ", "つ"), "つ")

    def test_flow_supplies_unit_to_readback_and_value(self):
        from flowctl import Flow, Question, normalize_value
        f = Flow([Question("Q1", "ご来店の人数をお伺いしてもよろしいでしょうか？", "number", conditions=("答えた",), unit="人")])
        f.on_callee()
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "3 です"})
        self.assertIn("3人で", r["say"])                    # 言い直し：「3人で大丈夫でしょうか？」（前は「3で…」）
        self.assertEqual(f.items["Q1"].value, "3人")         # 依頼主へ渡す値
        self.assertEqual(normalize_value("number", "三", "人"), "3人")  # 補ってから漢数字を直す
        self.assertEqual(normalize_value("amount", "50", "円"), "50")   # 金額には補わない（「50万」の聞き落としでありうる）
        self.assertEqual(normalize_value("number", "3"), "3")           # 単位の無い質問は、いまと同じ


class TopicTest(unittest.TestCase):
    """質問の「聞くこと」（topics.py・G-43。✅ 2026-10-16 ユーザー「人を介さない形で」）。決まりは質問の文の言葉だけを使い、
    言葉を作り出さない。聞く質問でない形（「資料をお送りしても」「ご確認いただけますか」「聞いたことは」）には付けない。"""

    def test_rule(self):
        from topics import by_rule
        cases = {"ご希望の日時をお聞かせいただけますでしょうか？": "ご希望の日時",
                 "差し支えなければご年齢をお伺いしてもよろしいでしょうか？": "ご年齢",          # 「ば」のあとから
                 "恐れ入りますがご住所をお伺いしてもよろしいでしょうか？": "ご住所",            # 「が」のあとから
                 "ご連絡のつきやすい時間帯をお知らせいただけますでしょうか？": "ご連絡のつきやすい時間帯",  # 名詞にかかる動詞は中に入る
                 "お名前をフルネームでお伺いしてもよろしいでしょうか？": "お名前",
                 "担当の方のお名前と部署をお伺いしてもよろしいでしょうか？": "担当の方のお名前と部署",
                 "お名前を頂戴してもよろしいでしょうか？": "お名前", "ご住所を確認させていただけますでしょうか？": "ご住所",
                 "お名前をお聞きしてもよろしいでしょうか？": "お名前",
                 "資料をお送りしてもよろしいでしょうか？": "", "ご契約の内容をご確認いただけますでしょうか？": "",
                 "このサービスを聞いたことはございますでしょうか？": "", "何名様でお越しになりますでしょうか？": "",
                 "お使いになっている回線を教えていただけますでしょうか？": ""}                 # 形が崩れる（「いる回線」）＝空
        for q, want in cases.items():
            self.assertEqual(by_rule(q), want, q)

    def test_check_and_decide(self):
        from topics import check, decide
        self.assertTrue(check("ご来店の人数"))
        for bad in ("", "人数をお伺い", "ご来店の人数ですか？", "いる回線", "とても長い長い長い長い長い長い長い長い言い方の名前"):
            self.assertFalse(check(bad), bad)
        self.assertEqual(decide("ご希望の日時をお聞かせいただけますでしょうか？", ["日時", "日時"]), "ご希望の日時")  # 決まりが先
        self.assertEqual(decide("何名様でお越しになりますでしょうか？", ["ご来店の人数", "ご来店の人数"]), "ご来店の人数")
        self.assertEqual(decide("何名様でお越しになりますでしょうか？", ["ご来店の人数", "お越しになる人数"]), "")  # 食い違い＝空
        self.assertEqual(decide("何名様でお越しになりますでしょうか？", ["ご来店の人数"]), "")                       # 1つでは決めない


class MeaningTest(unittest.TestCase):
    """選択肢ごとの意味の言葉（meanings.py・G-48）。⚠️ 一番こわいのは向きの取り違え（「はい」に「されていない」）＝相手が認めると逆の答えが渡る。
    わざと取り違えた言葉を混ぜて、決まらないことを確かめる（Intent.md 16-1「わざと壊す」）。"""
    Q = "ご予約はされていますでしょうか？"

    def test_check(self):
        from meanings import check
        self.assertTrue(check("ご予約はされているということ", self.Q, "はい"))
        self.assertTrue(check("お支払いはカード", "お支払いはカードと振込のどちらがよろしいでしょうか？", "カード"))
        for bad in ("", "はい", "ご予約、されているということ", "ご予約はされていますか", "ご予約はされているでしょうか",
                    "とても長い長い長い長い長い長い長い長い長い長い言い方", "ご来店は初めてということ"):  # 最後＝質問に無い言葉だけ
            self.assertFalse(check(bad, self.Q, "はい"), bad)

    def test_decide(self):
        from meanings import decide
        a = {"はい": "ご予約はされているということ", "いいえ": "ご予約はされていないということ"}
        b = {"はい": "ご予約はお済みということ", "いいえ": "ご予約はまだされていないということ"}
        self.assertEqual(decide(self.Q, ("はい", "いいえ"), [a, b]), a)            # 言い方が違っても、向きがそろえば最初のモデルの言葉
        flipped = {"はい": "ご予約はされていないということ", "いいえ": "ご予約はされているということ"}
        self.assertEqual(decide(self.Q, ("はい", "いいえ"), [a, flipped]), {})     # わざと取り違えた：向きが食い違えば決めない
        self.assertEqual(decide(self.Q, ("はい", "いいえ"), [a]), {})              # 1つでは決めない
        self.assertEqual(decide(self.Q, ("はい", "いいえ"), [a, {"はい": a["はい"]}]), {"はい": a["はい"]})  # 片方に無い選択肢は決めない

    def test_negated(self):
        from meanings import negated
        for w in ("ご予約はされていないということ", "お電話は差し上げなくてよろしいということ", "お電話は不要ということ",
                  "ご予約の日時に変更はないということ", "ご予約の日時に変更はなしということ"):
            self.assertTrue(negated(w), w)
        for w in ("ご予約はされているということ", "ご予約の日時に変更があるということ", "お支払いはカード"):
            self.assertFalse(negated(w), w)


class SpokenNameTest(unittest.TestCase):
    """決まった読みは、声に出す文の名前をひらがなにして渡す（instruction.quote_in_text・Business。G-44）。決まらない名前は表記のまま。"""

    def test_quote_with_readings(self):
        from instruction import quote_in_text
        names = [{"surface": "日本橋", "reading": "にっぽんばし"}, {"surface": "中島", "reading": ""}, "上野原"]
        self.assertEqual(quote_in_text("大阪の日本橋店と、上野原の土地と、中島の件です。", names),
                         "大阪の「にっぽんばし」店と、「上野原」の土地と、「中島」の件です。")
        self.assertEqual(quote_in_text("上野の件です。", ["上野"]), "「上野」の件です。")  # 文字だけの名前は、いままでどおり

    def test_business_question_texts_use_readings(self):
        """⛔ 見直しで見つけた漏れ：読みの置き換えが名乗りにしか効かず、質問の文の名前は漢字のまま、読みがなは2回読まれた。"""
        from flowctl import Question
        from instruction import Business
        names = [{"surface": "大和ハウス工業", "reading": "だいわはうすこうぎょう"}, {"surface": "東海林", "reading": "しょうじ"},
                 {"surface": "中島", "reading": ""}]
        q = [Question("Q1", "「大和ハウス工業」様と同じ形で、東海林（しょうじ）が中島の件で伺ってもよろしいでしょうか？", "yes_no",
                      conditions=("はい", "いいえ"))]
        b = Business(company="アオバ物流", caller="佐藤", purpose="", questions=q, names=names, purpose_line="東海林（しょうじ）の件です。")
        self.assertEqual(b.questions[0].text, "「だいわはうすこうぎょう」様と同じ形で、「しょうじ」が「中島」の件で伺ってもよろしいでしょうか？")
        self.assertEqual(b.purpose_line, "「しょうじ」の件です。")
        b = Business(company="アオバ物流", caller="佐藤", purpose="東海林（しょうじ）が担当する体験授業の案内", questions=q, names=names)
        self.assertEqual(b.purpose, "「しょうじ」が担当する体験授業の案内")   # AIに渡す目的の中の名前も、同じ1か所を通す
        self.assertEqual(b.opening_parts()[-1], b.questions[0].text)  # 名乗りに続く最初の質問も、同じ文

    def test_lead_word_does_not_shift_with_reading_question(self):
        """⛔ 見直しで見つけた漏れ：読みの確かめを挟むと、次の質問のつなぎの言葉（録音の名前）が「それでは」から「続きまして」にずれた。"""
        from flowctl import Question, reading_question, spoken_question
        qs = [Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")), reading_question("N1", "中島", "なかじま"),
              Question("Q2", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))]
        self.assertEqual(spoken_question(qs, "Q2")[1], "それでは")
        self.assertEqual(spoken_question(qs, "N1"), (qs[1].text, ""))

    def test_opening_reads_from_names(self):
        """⛔ 見直し（2回目）：名乗りの社名の読みも、名前の一覧から取る。設定の読みがあれば、そちらが先。
        ✅ 担当者名は一覧から取らない（2026-10-16 ユーザー決定＝案C：担当者名の読みは設定に必ず入れる）。"""
        from flowctl import Question
        from instruction import Business
        q = [Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))]
        names = [{"surface": "アオバ物流", "reading": "あおばぶつりゅう"}, {"surface": "東海林", "reading": "しょうじ"}]
        b = Business(company="アオバ物流", caller="東海林", purpose="", questions=q, names=names, caller_reading="とうかいりん")
        self.assertEqual(b.opening(), "お忙しいところ恐れ入ります。株式会社「あおばぶつりゅう」の「とうかいりん」と申します。")
        b = Business(company="アオバ物流", caller="東海林", purpose="", questions=q, names=names, company_reading="あおば")
        self.assertEqual(b.opening(), "お忙しいところ恐れ入ります。株式会社「あおば」の「東海林」と申します。")

    def test_caller_cannot_be_unread(self):
        """✅ 2026-10-16 ユーザー決定（案C）：担当者名は名乗りで必ず言うので、「声に出さずに言い換える名前」には入れられない。"""
        from flowctl import Question
        from instruction import Business
        q = [Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))]
        with self.assertRaises(AssertionError):
            Business(company="アオバ物流", caller="中島", purpose="", questions=q, unread_names=["中島"])
        Business(company="アオバ物流", caller="佐藤", purpose="", questions=q, unread_names=["中島"])  # ほかの名前ならよい

    def test_business_uses_readings(self):
        from flowctl import Question
        from instruction import Business
        q = [Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))]
        b = Business(company="大和ハウス工業", caller="中島", purpose="", questions=q, company_reading="だいわはうすこうぎょう")
        self.assertEqual(b.opening(), "お忙しいところ恐れ入ります。株式会社「だいわはうすこうぎょう」の「中島」と申します。")


class KeyRingTest(unittest.TestCase):
    """⛔ 2026-10-16 ユーザー：「キーはどれを使ってもいいと言ってるよ」＝上限に当たったら次のキーで続ける。"""

    def test_moves_to_next_key_on_quota(self):
        import argparse
        import common

        class Quota(Exception):
            code = 429

        ring = common.KeyRing(argparse.Namespace(proxy_key=False, env="/nonexistent", key_name=None, key_index=0), min_interval_s=0,
                              state_path=False)
        ring.keys = [common.ApiKey("K0", "a" * 39), common.ApiKey("K1", "b" * 39), common.ApiKey("K2", "c" * 39)]
        ring._clients = {0: "c0", 1: "c1", 2: "c2"}
        calls = []

        def fn(client):
            calls.append(client)
            if client in ("c0", "c1"):
                raise Quota("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")   # 1日の上限
            return "ok"

        self.assertEqual(ring.call("m", fn), ("ok", 2))
        self.assertEqual(ring.call("m", fn), ("ok", 2))        # 上限に当たったキーは、そのモデルでは飛ばす
        self.assertEqual(calls, ["c0", "c1", "c2", "c2"])
        with self.assertRaises(RuntimeError):
            ring.call("m", lambda c: (_ for _ in ()).throw(Quota("429 PerDay")))

    def test_pinned_key_does_not_move(self):
        # キーごとに1本の流れ（g53_meaning.run_judges）：自分のキーだけを使い、使えなくなったら抜ける（ほかの流れのキーに重ならない）
        import argparse
        import common

        class Quota(Exception):
            code = 429

        ring = common.KeyRing(argparse.Namespace(proxy_key=False, env="/nonexistent", key_name=None, key_index=1), min_interval_s=0,
                              state_path=False)
        ring.keys = [common.ApiKey("K0", "a" * 39), common.ApiKey("K1", "b" * 39)]
        ring._clients = {0: "c0", 1: "c1"}
        self.assertEqual(ring.call("m", lambda c: c, rotate=False), ("c1", 1))
        with self.assertRaises(common.KeyUnusable):
            ring.call("m", lambda c: (_ for _ in ()).throw(Quota("429 PerDay")), rotate=False)

    def test_limit_without_per_day_is_not_a_day(self):
        # ⛔ 2026-10-17 G-53：待つ秒の書かれていない上限の知らせを1日の上限と決めつけ、使えるキーを全部「今日はもう使えない」にした
        import argparse
        import common
        from unittest import mock

        class Quota(Exception):
            code = 429

        ring = common.KeyRing(argparse.Namespace(proxy_key=False, env="/nonexistent", key_name=None, key_index=0), min_interval_s=0,
                              state_path=False)
        ring.keys = [common.ApiKey("K0", "a" * 39)]
        ring._clients = {0: "c0"}
        n = {"c0": 0}

        def fn(client):
            n["c0"] += 1
            if n["c0"] == 1:
                raise Quota("429 RESOURCE_EXHAUSTED. You exceeded your current quota")   # 秒も上限の名前も無い
            return "ok"
        with mock.patch("time.sleep") as sl:
            self.assertEqual(ring.call("m", fn), ("ok", 0))
        self.assertNotIn(0, ring.spent.get("m", set()))
        self.assertTrue(any(c.args[0] >= 30 for c in sl.call_args_list))       # 秒が無ければ30秒待つ
        self.assertTrue(common.is_daily(Exception("quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")))
        self.assertFalse(common.is_daily(Exception("quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier")))

    def test_denied_project_and_per_minute_limit(self):
        import argparse
        import common
        from unittest import mock

        class Denied(Exception):
            code = 403

        class Minute(Exception):
            code = 429

        ring = common.KeyRing(argparse.Namespace(proxy_key=False, env="/nonexistent", key_name=None, key_index=0), min_interval_s=0,
                              state_path=False)
        ring.keys = [common.ApiKey("K0", "a" * 39), common.ApiKey("K1", "b" * 39)]
        ring._clients = {0: "c0", 1: "c1"}
        n = {"c1": 0}

        def fn(client):
            if client == "c0":
                raise Denied("403 PERMISSION_DENIED. Your project has been denied access.")
            n["c1"] += 1
            if n["c1"] == 1:
                raise Minute("429 RESOURCE_EXHAUSTED. Please retry in 2.5s.")
            return "ok"

        with mock.patch("time.sleep") as sl:
            self.assertEqual(ring.call("m", fn), ("ok", 1))
        self.assertEqual(ring.denied, {0})                 # 止められたキーは、どのモデルでも飛ばす
        self.assertNotIn(1, ring.spent.get("m", set()))     # 1分の上限は、待って同じキーで続ける（1日の上限と取り違えない）
        self.assertTrue(any(c.args[0] >= 2.5 for c in sl.call_args_list))
        self.assertEqual(common.retry_after_s(Exception("Please retry in 11h8m7.1s")), 11 * 3600 + 8 * 60 + 7.1)

    def test_state_is_remembered_for_the_day(self):
        import argparse
        import tempfile
        from pathlib import Path
        import common
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            ns = argparse.Namespace(proxy_key=False, env="/nonexistent", key_name=None, key_index=0)
            keys = [common.ApiKey("K0", "a" * 39), common.ApiKey("K1", "b" * 39)]
            with unittest.mock.patch.object(common, "load_keys", return_value=keys):
                ring = common.KeyRing(ns, min_interval_s=0, state_path=path)
                ring.rest(0, "m"); ring.denied = {1}; ring._save()
                again = common.KeyRing(ns, min_interval_s=0, state_path=path)
                # 同じファイルに書くほかの KeyRing（同じ実行の会話のモデルと意味の判定）の記録を消さない（⛔ 後から書いたほうが消していた）
                other = common.KeyRing(ns, min_interval_s=0, state_path=path)
                other.spent, other.denied = {}, set()
                other.rest(1, "text")
                merged = common.KeyRing(ns, min_interval_s=0, state_path=path)
                # 1日の上限の休みは、期限まで（⛔ その日いっぱいにしたら、2時間後に戻ったキーまで止めていた）。前の形（その日いっぱい）は読まない
                import json as _json
                import time as _time
                with unittest.mock.patch("time.time", return_value=_time.time() + common.KeyRing.DAILY_REST_S + 1):
                    later = common.KeyRing(ns, min_interval_s=0, state_path=path)
                    self.assertFalse(later.resting(0, "m"))
                path.write_text(_json.dumps({"day": common.KeyRing._today(), "denied": [], "spent": {"m": [keys[0].fingerprint]}}))
                old_form = common.KeyRing(ns, min_interval_s=0, state_path=path)
            self.assertEqual(({m: set(v) for m, v in again.spent.items()}, again.denied), ({"m": {0}}, {1}))
            self.assertEqual(({m: set(v) for m, v in merged.spent.items()}, merged.denied), ({"m": {0}, "text": {1}}, {1}))
            self.assertTrue(merged.resting(0, "m") and merged.resting(1, "text") and not merged.resting(1, "m"))
            self.assertEqual(old_form.spent, {})
            self.assertNotIn("a" * 39, path.read_text(encoding="utf-8"))   # キーの値は書かない

    def test_live_calls_move_to_next_key(self):
        # ⛔ 2026-10-17：通話の試験は1つのキーだけでつなぎ、上限（会話のモデルは閉じる番号 1011・「exceeded your current quota」）に当たると
        # 止まっていた ⇒ 次のキーで初めからやり直す。止められたプロジェクトは使わない。ほかの誤りはそのまま上げる
        import argparse
        import asyncio
        import common

        class Live(Exception):
            def __init__(self, code, msg):
                super().__init__(f"{code} None. {msg}")
                self.code = code

        ring = common.KeyRing(argparse.Namespace(proxy_key=False, env="/nonexistent", key_name=None, key_index=0), min_interval_s=0,
                              state_path=False)
        ring.keys = [common.ApiKey(f"K{i}", chr(97 + i) * 39) for i in range(3)]
        ring._clients = {0: "c0", 1: "c1", 2: "c2"}
        calls = []

        async def fn(client):
            calls.append(client)
            if client == "c0":
                raise Live(1011, "You exceeded your current quota, please check your plan and billing details.")
            if client == "c1":
                raise Live(1008, "Your project has been denied access. Please contact support.")
            return "ok"

        self.assertEqual(asyncio.run(ring.acall("live", fn)), ("ok", 2))
        self.assertEqual(asyncio.run(ring.acall("live", fn)), ("ok", 2))      # 休み中のキー・止められたキーは飛ばす
        self.assertEqual(calls, ["c0", "c1", "c2", "c2"])
        self.assertEqual(ring.denied, {1})

        async def other(client):
            raise ValueError("ほかの誤り")
        with self.assertRaises(ValueError):
            asyncio.run(ring.acall("live", other))
        # 待つ秒が書いてある上限（文字のモデルの1分の上限）は、その秒だけ休ませる（⛔ 前は10分休ませ、通話の中の判定が間に合わなかった）
        import time

        class Minute(Exception):
            code = 429

        async def minute(client):
            if client == "c2":
                raise Minute("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerMinutePerProjectPerModel. Please retry in 3.0s.")
            return "ok"
        ring.denied, ring._cool = set(), {}
        ring.start = 2
        self.assertEqual(asyncio.run(ring.acall("text", minute)), ("ok", 0))
        self.assertLess(ring._cool[(2, "text")] - time.monotonic(), 10)

        # 待つ秒が無くても、1分の上限と書いてあれば30秒ほど（⛔ 2026-10-18：10分休ませ、通話の中の判定が届かなかった）
        async def minute_no_secs(client):
            if client == "c2":
                raise Minute("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
            return "ok"
        ring._cool = {}
        self.assertEqual(asyncio.run(ring.acall("text", minute_no_secs)), ("ok", 0))
        self.assertLess(ring._cool[(2, "text")] - time.monotonic(), 40)

        # 1日の上限と書いてある（文字のモデル）：DAILY_REST_S 秒休ませて記録する（⛔ 2026-10-19：10分休ませるだけで、休み明けのたびに
        # 上限のキーへ順に問い合わせ直し、1回の判定に7.9秒かかった）
        async def daily(client):
            calls.append(client)
            if client == "c2":
                raise Minute("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            return "ok"
        ring._cool, calls[:] = {}, []
        self.assertEqual(asyncio.run(ring.acall("text", daily)), ("ok", 0))
        self.assertEqual((set(ring.spent.get("text", {})), (2, "text") in ring._cool), ({2}, False))
        self.assertEqual(asyncio.run(ring.acall("text", daily)), ("ok", 0))
        self.assertEqual(calls, ["c2", "c0", "c0"])

        # 通話の中の判定は、どのキーも休み中なら待たない（max_wait_s=0。JudgePanel が代わりへ回す）
        ring._cool = {(i, "text"): time.monotonic() + 600 for i in range(3)}
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError):
            asyncio.run(ring.acall("text", daily, max_wait_s=0.0))
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_session_timeout_is_not_a_denied_project(self):
        # ⛔ つなぎの時間切れも閉じる番号 1008 で返る（2026-10-17 G-15）＝番号だけで「止められたプロジェクト」と決めない
        import common

        class Live(Exception):
            code = 1008
        goaway = Live("1008 None. Connection aborted because the client failed to close the connection after receiving a GoAway signal "
                      "once the session duration limit was reached.")
        self.assertFalse(common.is_denied(goaway))
        self.assertTrue(common.is_denied(Live("1008 None. Your project has been denied access. Please contact support.")))
        self.assertTrue(common.is_quota(Exception("1011 None. You exceeded your current quota, please check your plan and billing details.")))
        self.assertFalse(common.is_quota(goaway))


class ScriptRecordingTest(unittest.TestCase):
    """✅ 2026-10-16 ユーザー：「こちら側の担当者名は、依頼者ごとの録音時に録音しておくといいでしょう」。名乗り（自社名・担当者名）と
    用件も依頼主の単位で録音し、読みが決まらないこちら側の名前は、読みの候補ごとに録音を作って、人が聞いて選ぶ。"""

    def business(self):
        from flowctl import Question
        from instruction import Business
        names = [{"surface": "新垣", "reading": "", "candidates": ["あらがき", "しんがき"], "likely": "あらがき"},
                 {"surface": "東海林", "reading": "しょうじ"}]
        q = [Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
             Question("Q2", "当日は講師の新垣がご案内いたしますがよろしいでしょうか？", "yes_no", conditions=("はい", "いいえ"))]
        return Business(company="アオバ物流", caller="東海林", caller_reading="しょうじ", purpose="", questions=q, names=names,
                        purpose_line="ご予約の確認でお電話いたしました。")

    def test_table_has_intro_and_variants(self):
        from make_ack_clips import script_table
        table, readings = script_table(self.business())
        self.assertEqual(table["intro"][0], "株式会社「アオバ物流」の「しょうじ」と申します。")   # 担当者名は設定の読み（案C）
        self.assertIn("purpose", table)
        v = {k: t[0] for k, t in table.items() if k.startswith("Q2")}
        self.assertEqual(v, {"Q2_lead_soredeha_v1": "それでは当日は講師の「あらがき」がご案内いたしますがよろしいでしょうか？",   # まず言ってみる読みが先
                             "Q2_lead_soredeha_v2": "それでは当日は講師の「しんがき」がご案内いたしますがよろしいでしょうか？"})
        self.assertEqual(sorted(readings[k]["新垣"] for k in v), ["あらがき", "しんがき"])

    def test_chosen_reading_is_kept_and_conflicts_stop(self):
        import tempfile
        from pathlib import Path
        from make_ack_clips import record_readings, script_table
        _, readings = script_table(self.business())
        ids = {r["新垣"]: k for k, r in readings.items()}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "name_readings.json"
            book = record_readings([{"id": ids["しんがき"], "chosen_from": "S4_x_1"}], readings, path)
            self.assertEqual(book["新垣"], {"reading": "しんがき", "source": "録音で選んだ", "chosen_in": "S4_x_1"})
            with self.assertRaises(AssertionError):
                record_readings([{"id": ids["あらがき"], "chosen_from": "S4_x_2"}], readings, path)

    def test_ear_check_skips_names(self):
        """⛔ 見直し：別の耳は「しょうじ」を「庄司」、「アオバ物流」を「青葉物流」と書いた（名前の読みは別の耳では確かめられない）。"""
        from make_ack_clips import heard_ok
        text = "株式会社「アオバ物流」の「しょうじ」と申します。"
        self.assertTrue(heard_ok(text, {"0.0": "株式会社青葉物流の庄司と申します。", "1.0": "株式会社アオバ物流の東海林と申します。"}))
        self.assertFalse(heard_ok(text, {"0.0": "株式会社青葉物流の庄司ともうしまう。", "1.0": "株式会社青葉物流の庄司と申します。"}))
        self.assertFalse(heard_ok(text, {"0.0": "株式会社の庄司と申します。", "1.0": "株式会社青葉物流の庄司と申します。"}))  # 名前が抜けた
        self.assertTrue(heard_ok("ご予約はされていますでしょうか？", {"0.0": "ご予約はされていますでしょうか。", "1.0": "ご予約はされていますでしょうか"}))

    def test_contract_table_has_greeting(self):
        from make_ack_clips import words
        table, _ = words(False)
        self.assertEqual(table["greeting"][0], "お忙しいところ恐れ入ります。")


class CalleeQuestionTest(unittest.TestCase):
    """⛔ G-47：相手が答えと一緒に質問をしたのに、すぐ次の質問の録音を流し、相手の質問を聞き流した（10本中10本）。"""

    def flow(self, play=True):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
                     Question("Q2", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))],
                    play_recordings=play)

    def test_question_holds_the_recording(self):
        f = self.flow()
        f.on_callee(); f.on_heard("はい、予約しています。当日はどなたが担当されるんですか？")
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})
        self.assertNotIn("play", r)                                    # 録音は流さない
        self.assertIn("当日はどなたが担当されるんですか？", r["message"])   # まず答える
        self.assertIn("「それでは」から話し始めて", r["message"])          # つなぎの言葉は録音と同じ並び

    def test_plain_answer_plays(self):
        f = self.flow()
        f.on_callee(); f.on_heard("はい、予約しています。")
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})
        self.assertEqual(r["play"][0]["id"], "Q2")

    def test_question_endings(self):
        from flowctl import _CALLEE_ASKS
        for t in ("料金はおいくらですか", "どなたが担当されるんですか？", "来ていただけるんでしょうか", "それって無料ですか。"):
            self.assertTrue(_CALLEE_ASKS.search(t), t)
        for t in ("はい、予約しています。", "15日の午後2時でお願いします。", "いいえ、まだしていません", "はい、そうです。"):
            self.assertFalse(_CALLEE_ASKS.search(t), t)


class EarsTest(unittest.TestCase):
    """通話の中の別の耳（ears.py）：入口は1つ（open_ear）、どの耳も同じ形（feed で流す・wait で確定した文字を待つ）。つながずに確かめる。"""

    def test_amivoice_events(self):
        from ears import amivoice_final
        self.assertEqual(amivoice_final('A {"text": "してないです。", "results": []}'), "してないです。")
        # 言いよどみの印は外して言葉は残す（⛔ 初めの設定では「いいえ」「ええ」を言いよどみとして消していた。ears.AMIVOICE_KEEP_FILLER）
        self.assertEqual(amivoice_final('A {"text": "%い%%え%まだです。"}'), "いえまだです。")
        self.assertEqual(amivoice_final('A {"text": "%えー%してます。"}'), "えーしてます。")
        for ev in ('U {"text": "して"}', "S 1200", "E 2300", "C", 'A {"text": ""}', "A not-json", 'A {"text": "%%"}'):
            self.assertIsNone(amivoice_final(ev), ev)

    def test_soniox_tokens(self):
        from ears import soniox_update
        pending = []
        self.assertEqual(soniox_update(pending, {"tokens": [{"text": "して", "is_final": True}, {"text": "ない", "is_final": False}]}), [])
        self.assertEqual(soniox_update(pending, {"tokens": [{"text": "ないです。", "is_final": True}, {"text": "<end>", "is_final": True}]}),
                         ["してないです。"])
        self.assertEqual(pending, [])                                           # 話し終わりで、次の発話のために空にする
        # こちらから頼んだ確定（{"type": "finalize"}）の終わりの印「<fin>」でも、1つの発話として渡す
        self.assertEqual(soniox_update(pending, {"tokens": [{"text": "はい、", "is_final": True}, {"text": "先週しました。", "is_final": True},
                                                            {"text": "<fin>", "is_final": True}]}), ["はい、先週しました。"])
        with self.assertRaises(RuntimeError):
            soniox_update(pending, {"error_code": 401, "error_message": "unauthorized"})

    def test_pieces_during_the_voice_are_joined(self):
        # ⛔ 2026-10-17：耳が発話の途中の間で区切ったとき、最初のかけら（「はい。」）で渡していた ⇒ 声を流し終えたあとに届いた分で、
        # ためた分と合わせて渡す。声を流し終えたあとに何も届かなければ、かけらだけでは比べない（None）
        import asyncio
        from ears import Ear

        class SplitEar(Ear):
            def __init__(self, after: bool):
                super().__init__()
                self.after, self.n = after, 0

            async def _send(self, pcm, voice):
                if voice:
                    self.n += 1
                    if self.n == 2:
                        self._final("はい。")                                    # 声を流している間のかけら

            async def _fed(self):
                if self.after:
                    self._final("先週しました。")

        async def go(after: bool):
            async with SplitEar(after) as ear:
                await ear.feed(b"\0\0" * 8000, 16000, 0.0)
                return await ear.wait(0.3)
        self.assertEqual(asyncio.run(go(True)), "はい。先週しました。")
        self.assertIsNone(asyncio.run(go(False)))                               # かけらだけなら比べない（返さない）

    def test_missing_key_and_model_stop_with_instructions(self):
        import os
        import tempfile
        from ears import open_ear, reazon_files
        from pathlib import Path
        os.environ.pop("SONIOX_API_KEY", None)
        with self.assertRaises(SystemExit) as e:
            open_ear("soniox", env_path=os.path.join(tempfile.gettempdir(), "no-such.env"))
        self.assertIn("SONIOX_API_KEY", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            reazon_files(Path(tempfile.mkdtemp()))
        self.assertIn("get_reazon_model.py", str(e.exception))
        with self.assertRaises(SystemExit):
            open_ear("no-such-ear")

    def test_key_is_hidden(self):
        import os
        from common import load_secret, redact
        os.environ["AMIVOICE_APPKEY"] = "test-value-123456"
        try:
            self.assertEqual(load_secret("AMIVOICE_APPKEY", env_path="/no/such.env"), "test-value-123456")
            self.assertNotIn("test-value-123456", redact("s LSB16K -a-general authorization=test-value-123456"))
        finally:
            os.environ.pop("AMIVOICE_APPKEY")

    def test_feed_and_wait(self):
        # 共通の形：流している間は無音を止め、流し終えたら（この耳は、そこで）確定した文字を渡す。最初に届いた時刻を残す
        import asyncio
        from ears import Ear

        class FakeEar(Ear):
            def __init__(self):
                super().__init__()
                self.sent = []

            async def _send(self, pcm, voice):
                self.sent.append(voice)

            async def _fed(self):
                self._final("はい、予約しています。")

        async def go():
            async with FakeEar() as ear:
                await ear.feed(b"\0\0" * 8000, 16000, 0.0)                    # 0.5秒の声
                return ear, await ear.wait(1.0), await asyncio.wait_for(ear.wait(0.1), 1.0)
        ear, text, again = asyncio.run(go())
        self.assertEqual((text, again), ("はい、予約しています。", "はい、予約しています。"))
        self.assertIsNotNone(ear.first_at)
        self.assertGreaterEqual(ear.sent.count(True), 5)                        # 0.1秒ずつ5つ

    def test_cut_after_the_voice(self):
        # 区切りを伝えるのは、声の終わりから CUT_S 秒の所（本物の通話の、話し終わりの見張りと同じ）。⛔ 2026-10-17：ファイルを流し終えた所で
        # 区切っていたため、声のあとの無音が無いファイルで、Soniox が最後の「す」を落とした。ファイルの無音が長ければ、そこまでは流さない
        import asyncio
        from ears import CUT_S, Ear

        class CountEar(Ear):
            def __init__(self):
                super().__init__()
                self.sent, self.at_fed = 0, None

            async def _send(self, pcm, voice):
                self.sent += len(pcm) if voice else 0

            async def _fed(self):
                self.at_fed = self.sent

        async def go(seconds: float, tail_s: float):
            async with CountEar() as ear:
                await ear.feed(b"\0\0" * int(16000 * seconds), 16000, tail_s)
                return ear.at_fed / 2 / 16000
        self.assertAlmostEqual(asyncio.run(go(0.5, 0.0)), 0.5 + CUT_S, places=2)     # 無音の無いファイル：無音を足す
        self.assertAlmostEqual(asyncio.run(go(1.5, 0.9)), 0.6 + CUT_S, places=2)     # 無音の長いファイル：声の終わりから CUT_S 秒まで

    def test_reazon_cuts_from_before_the_voice(self):
        # ⛔ 2026-10-17：声の頭から切り出して渡すと、頭の言葉を落とした（「はい、予約しています。」⇒「予約しています」）。
        # ⇒ 発話の前の PREROLL_S 秒（その前に流れていた音）から切り出す。つないだ直後の発話は、無音から（モデルを読まずに確かめる）
        import asyncio
        from ears import RATE, ReazonEar
        pre = int(ReazonEar.PREROLL_S * RATE) * 2

        async def go():
            ear = ReazonEar()
            await ear._send(b"\1\0" * 10, voice=True)
            first = bytes(ear._buf)
            ear._buf = bytearray()
            await ear._send(b"\2\0" * RATE, voice=False)                        # 1秒の回線の音
            await ear._send(b"\3\0" * 10, voice=True)
            await ear._send(b"\4\0" * 10, voice=True)
            return first, bytes(ear._buf)
        first, second = asyncio.run(go())
        self.assertEqual(first, bytes(pre) + b"\1\0" * 10)
        self.assertEqual(second, b"\2\0" * (pre // 2) + b"\3\0" * 10 + b"\4\0" * 10)  # 直前の音は PREROLL_S 秒だけ・頭に1回だけ


class EarsTryTest(unittest.TestCase):
    """独立した別の耳を1つの命令で試す道具（ears_try.py）と、ユーザーが貼る文字（docs/EARS_SETUP.md）。つながずに確かめる。"""

    DOC = Path(__file__).resolve().parent.parent / "docs" / "EARS_SETUP.md"

    def blocks(self) -> list[str]:
        """手順書の枠（``` で囲んだ所）の中の行。"""
        lines, inside = [], False
        for line in self.DOC.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                inside = not inside
            elif inside and line.strip():
                lines.append(line.strip())
        return lines

    def test_doc_has_every_host_and_key_name(self):
        # 手順書の枠に、コードがつなぐ接続先（ワイルドカードを含む）と、コードが読むキーの名前が、すべてある（食い違うと、貼っても動かない）
        from ears import SECRETS
        from ears_try import HOSTS
        lines = self.blocks()

        def covered(host: str) -> bool:
            return any(d == host or (d.startswith("*.") and host.endswith(d[1:])) for d in lines)
        hosts = {h for hs in HOSTS.values() for h in hs} | {"generativelanguage.googleapis.com", "release-assets.githubusercontent.com"}
        for h in sorted(hosts):
            self.assertTrue(covered(h), h)
        for name in SECRETS.values():
            self.assertTrue(any(l.startswith(f"{name}=") for l in lines), name)

    def test_predict_counts_like_the_call(self):
        # 通話の中と同じ決まり：別の耳の文字が hold_s 秒までに届いて食い違えば確かめる。数えるのは AI が選んだ答えで
        # （⛔ 1回目は会話AIの文字が台本と違うかで数え、文字が違ってもAIが正しく選んだ通話の確かめを「拾える誤り」にしていた）
        # （⛔ 2回目は、いまの頭の言葉の確かめでもう拾える通話も「拾える誤り」に数えていた ⇒「いまの確かめで拾える」に分ける）
        # （⛔ 3回目は、言い切っていない答えの確かめを見落とした ⇒ 決まりを書き写さず、流れの制御そのものに渡す）
        from ears_try import predict
        from flowctl import Question
        qs = {"Q1": Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))}
        rows = [{"wav": "a.wav", "text": "してないです。", "ear": "してないです", "first_s": 0.3},
                {"wav": "c.wav", "text": "はい、先週しました。", "ear": "はい、先週しました", "first_s": 0.5},
                {"wav": "d.wav", "text": "予約済みです。", "ear": "要約済みです", "first_s": 2.5},
                {"wav": "e.wav", "text": "まだです。", "ear": "まだです", "first_s": 0.4},
                {"wav": "g.wav", "text": "いいえ、まだです。", "ear": "いえ、まだです", "first_s": 0.4},
                {"wav": "f.wav", "text": "大丈夫です。", "ear": "大丈夫", "first_s": 0.4},
                {"wav": "h.wav", "text": "家族がしたかもしれないです。", "ear": "家族が来たかもしれないです", "first_s": 0.4}]
        calls = [{"wav": "a.wav", "heard": "捨てないです。", "condition": "はい", "want": "いいえ"},      # AIが誤った・別の耳と食い違う ⇒ 拾える
                 {"wav": "a.wav", "heard": "してないです。", "condition": "いいえ", "want": "いいえ"},
                 {"wav": "c.wav", "heard": "はい、清掃しました。", "condition": "はい", "want": "はい"},  # 文字は違うがAIは正しい ⇒ 要らない確かめ
                 {"wav": "d.wav", "heard": "予約済みです。", "condition": "はい", "want": "はい"},       # 間に合わない
                 {"wav": "e.wav", "heard": "まだです。", "condition": "はい", "want": "いいえ"},          # 同じに聞いたがAIが誤った ⇒ 拾えない
                 # 頭の言葉の確かめ（2026-10-18 に外した）が拾っていた ⇒ 別の耳だけでは拾えない（意味の判定が拾う＝MeaningJudgeTest）
                 {"wav": "g.wav", "heard": "いいえ、まだです。", "condition": "はい", "want": "いいえ", "question": "Q1"},
                 {"wav": "h.wav", "heard": "家族がしたかもしれないです。", "condition": "はい", "want": "いいえ"},  # 言い切っていない答えの確かめが拾う
                 {"wav": "f.wav", "heard": "大丈夫です。", "condition": "はい", "want": "ambiguous"},    # 曖昧な答えは数えない
                 {"wav": "a.wav", "heard": "してないです。", "condition": "いいえ", "want": None},       # 正解の無い記録は数えない
                 {"wav": "z.wav", "heard": "はい。", "condition": "はい", "want": "はい"}]               # この耳が聞いていない声
        calls = [{"op": "record_answer", **c} for c in calls]
        # AIの最初の報告が答えの報告でない（締めた・報告が無い）：別の耳が効く所ではない（⛔ 4回目は「いまの確かめで拾える」に数えていた）
        calls += [{"wav": "e.wav", "heard": "結構です。", "op": "request_end", "condition": "callee_refused_call", "want": "いいえ"},
                  {"wav": "e.wav", "heard": "まだです。", "op": "なし", "condition": None, "want": "いいえ"}]
        p = predict(rows, calls, questions=qs)
        self.assertEqual((p["通話"], p["AIが正しく選んだ"], p["拾える誤り"], p["要らない確かめ"], p["間に合わない"], p["拾えない誤り"],
                          p["いまの確かめで拾える"], p["答えの報告ではない"]), (7, 3, 1, 1, 1, 2, 1, 2))

    def test_carried_over(self):
        # 前の発話の終わりの言葉が、この発話の別の耳の文字の頭に入ったか（⛔ 2026-10-17 の見直しまで「混ざりは0回」と書いていた）
        from g15_ear import carried_over
        self.assertTrue(carried_over("してないです。", "いや、これからなんです。", "です。 いや、これからなんです。"))   # Soniox：確定し損ねた語尾
        self.assertTrue(carried_over("はい。", "予約済みです。", "はい。予約済みです。"))                              # Gemini の耳：遅れて届いた
        self.assertFalse(carried_over("はい。", "はい、予約しています。", "はい、予約しています。"))                    # この発話の頭が同じ言葉
        self.assertFalse(carried_over("してないです。", "予約済みです。", "予約済みで。"))
        self.assertFalse(carried_over("はい。", "予約済みです。", None))

    def test_in_call_counts(self):
        from ears_try import in_call
        rows = [{"verdict": "正しい", "want": "はい", "condition": "はい", "ear_confirm": False, "ear_check": {"text": "はい"}},
                {"verdict": "意味を確かめた（はっきりした答えに）", "want": "いいえ", "condition": "いいえ", "ear_confirm": True, "ear_check": {"text": "x"}},
                {"verdict": "意味を確かめた（はっきりした答えに）", "want": "いいえ", "condition": "はい", "ear_confirm": True, "ear_check": {"text": "x"}},
                {"verdict": "意味を確かめた（はっきりした答えに）", "want": "ambiguous", "condition": "はい", "ear_confirm": True, "ear_check": {"text": "x"}},
                {"verdict": "⛔違う条件", "want": "いいえ", "condition": "はい", "ear_confirm": False, "ear_check": None}]
        c = in_call(rows)
        self.assertEqual((c["通話"], c["要らない確かめ（AIは正しかった）"], c["拾えた（AIが誤っていた）"], c["拾えなかった誤り"], c["別の耳の文字が届かなかった"]),
                         (5, 1, 1, 1, 1))

    def test_missing_reports_key_by_name_only(self):
        import os
        from ears_try import missing
        os.environ.pop("AMIVOICE_APPKEY", None)
        with unittest.mock.patch("ears_try.reachable", return_value=None):
            got = missing("amivoice", "/no/such.env")
        self.assertTrue(any("AMIVOICE_APPKEY" in m for m in got), got)
        os.environ["AMIVOICE_APPKEY"] = "test-value-987654"
        try:
            with unittest.mock.patch("ears_try.reachable", return_value="接続先 x が止められています"):
                got = missing("amivoice", "/no/such.env")
            self.assertEqual(len(got), 1)
            self.assertFalse(any("test-value-987654" in m for m in got))
            self.assertTrue(common.has_secret("AMIVOICE_APPKEY", "/no/such.env"))
        finally:
            os.environ.pop("AMIVOICE_APPKEY")

    def test_model_archive_names(self):
        # 外から来たまとめたファイルは、置き場の外を指す名前・使わないもの（試しの声・説明書き）を取り出さない
        from get_reazon_model import wanted
        d = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"
        self.assertEqual(wanted(f"{d}/encoder-epoch-99-avg-1.onnx"), "encoder-epoch-99-avg-1.onnx")
        self.assertEqual(wanted(f"{d}/joiner-epoch-99-avg-1.int8.onnx"), "joiner-epoch-99-avg-1.int8.onnx")
        self.assertEqual(wanted(f"{d}/tokens.txt"), "tokens.txt")
        for bad in (f"{d}/test_wavs/1.wav", f"{d}/README.md", "../encoder.onnx", "/tmp/encoder.onnx", f"{d}/sub/encoder.onnx", "encoder.onnx",
                    f"{d}/../encoder.onnx"):
            self.assertIsNone(wanted(bad), bad)


class EarCheckTest(unittest.TestCase):
    """DESIGN.md 6-10 の (5)・G-15：確かめない型の答えは、会話AIの聞き取りと別の耳の文字を比べ、食い違えば意味を確かめる。
    ⛔ G-14 の残りの誤り：「してないです」を会話AIが「捨てないです」と聞いてAIが「はい」を選んだ・「いや、これから」を「や、これから」と聞いて
    「している」を選んだ（どちらも、別の耳は正しく聞いた）。"""

    def flow(self, required=True):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"), required=required,
                              meanings=AmbiguousAnswerTest.M),
                     Question("Q2", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))])

    def answer(self, f, heard, ear, cond):
        f.on_callee(); f.on_heard(heard)
        self.assertTrue(f.wants_checks("record_answer", {"question_id": "Q1", "condition_id": cond}))
        f.on_check("ear", ear)
        return f.apply("record_answer", {"question_id": "Q1", "condition_id": cond, "value": heard})

    def test_alike(self):
        from flowctl import heard_alike
        for a, b in (("家まだしていません。", "いえ、まだしていません。"), ("いいえ、まだです。", "いえ、まだです。"), ("はい、予約しています。", "はい予約しています"),
                     ("いや、これからなんです。", "いいえ、これからなんです。"),
                     # 頭の返事の言葉の伸ばし方の違い（2026-10-17：AmiVoice は「えー」「いやあ」と書く）
                     ("ええ、してます。", "えーしてます。"), ("いや、これからなんです。", "いやあ、これからなんです。"), ("いいえ、まだです。", "いえまだです。"),
                     # 答えの前の言いよどみ（AmiVoice は息の音を「あ」と書く）
                     ("はい、予約しています。", "あはい。予約しています。"), ("まだです。", "えーと、まだです。"), ("してないです。", "あのー、してないです。")):
            self.assertTrue(heard_alike(a, b), (a, b))
        for a, b in (("捨てないです。", "してないです。"), ("や、これからなんです。", "いや、これからなんです。"), ("はい、清掃しました。", "はい、先週しました。"),
                     ("や、これからなんです。", "いやあ、これからなんです。"), ("いいえ、まだです。", "はい、まだです。"), ("ええ、してます。", "いえ、してます。"),
                     ("いいえ、まだです。", "あ、はい、まだです。"), ("ありがとうございます。", "りがとうございます。")):
            self.assertFalse(heard_alike(a, b), (a, b))

    def test_disagreement_is_confirmed(self):
        f = self.flow()
        r = self.answer(f, "捨てないです。", "してないです。", "はい")
        self.assertEqual((r["result"], f.awaiting_confirm), ("confirm_required", "Q1"))
        self.assertIn("ご予約はお済みということ", r["say"])                  # 意味の言葉で確かめる（曖昧な答えと同じ道）
        self.assertTrue(any("別の耳" in why for _, _, why in f.items["Q1"].history))

    def test_agreement_or_no_ear_is_taken(self):
        for ear in ("いいえ、まだです。", None):                             # 同じこと・別の耳が間に合わなかった（比べない＝止まらない）
            f = self.flow()
            r = self.answer(f, "いえ、まだです。", ear, "いいえ")
            self.assertEqual((r["result"], f.items["Q1"].state), ("next", "accepted"), ear)

    def test_not_required_is_unresolved(self):
        f = self.flow(required=False)
        r = self.answer(f, "捨てないです。", "してないです。", "はい")
        self.assertEqual(f.items["Q1"].state, "unresolved")                 # 必須でない項目は確かめない（曖昧な答えと同じ）

    def test_which_reports_wait(self):
        # 返事を作る前に確かめを待つのは、答えをそのまま受け取りうる報告だけ（DESIGN.md 1章）
        f = self.flow()
        self.assertTrue(f.wants_checks("record_answer", {"質問": "Q1", "答えの種類": "はい"}))
        self.assertFalse(f.wants_checks("confirm_field", {"質問": "Q1"}))   # 言い直しの返事を待っていない
        f.current = "Q2"
        self.assertFalse(f.wants_checks("record_answer", {"質問": "Q2", "答えの種類": "答えた"}))  # 値を聞く型は、言い直して確かめる（その返事で待つ）
        self.assertTrue(f.wants_checks("record_answer", {"質問": "Q2", "答えの種類": "わからない"}))  # 「わからない」はそのまま受け取る
        f.awaiting_confirm = "Q2"
        self.assertTrue(f.wants_checks("confirm_field", {"質問": "Q2"}))   # 言い直しへの返事
        self.assertTrue(f.wants_checks("replay_opening", {"流し直す部分": "代わった方へ"}))  # 取り次ぎを待つ間に流さない
        self.assertFalse(f.wants_checks("replay_opening", {"流し直す部分": "名乗り"}))
        f.on_check("ear", "はい")
        f.on_callee()
        self.assertIsNone(f.ear_said)                                       # 相手の発話ごとに消える


class MeaningJudgeTest(unittest.TestCase):
    """DESIGN.md 6-10 の (6)・G-53：意味の2つ目の判定が、会話のAIの選んだ答えと食い違えば、そのまま受け取らずに確かめる。
    ⛔ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」＝記録の通話で、
    意味を取り違えた答え12回を、流れの制御が確かめずに受け取っていた。"""
    M = {"はい": "ご予約はお済みということ", "いいえ": "ご予約はまだということ"}

    def flow(self):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"), meanings=self.M),
                     Question("Q2", "お支払いはカードと振込のどちらがよろしいでしょうか？", "choice", conditions=("カード", "振込"))])

    @staticmethod
    def report(f, qid, heard, cond, meaning):
        f.on_callee(); f.on_heard(heard); f.on_check("judge", meaning)
        return f.apply("record_answer", {"question_id": qid, "condition_id": cond, "value": heard})

    @staticmethod
    def reply(f, qid, heard, result, meaning, correction=None):
        f.on_callee(); f.on_heard(heard); f.on_check("judge", meaning)
        return f.apply("confirm_field", {"question_id": qid, "result": result, **({"correction": correction} if correction else {})})

    def to_q2(self, f):
        self.report(f, "Q1", "はい、予約しています。", "はい", "はい")
        self.assertEqual(f.current, "Q2")

    def test_dont_know_but_judge_cannot_decide_asks_again(self):
        # ⛔ 2026-10-19 G-55：「10名未満ではないです」（3択）をAIは「わからない」、判定は「決められない」＝答えの一部を言っている ⇒ 聞き直す
        f = self.flow()
        self.to_q2(f)
        r = self.report(f, "Q2", "振込じゃないほうがいいかなとは思いますけど", "unknown", "決められない")
        self.assertEqual(r["result"], "ask_back")
        self.assertEqual(f.items["Q2"].state, "unclear")
        f = self.flow()
        self.to_q2(f)
        self.report(f, "Q2", "ちょっと分からないです。", "unknown", "unknown")       # 判定もわからない ⇒ そのまま受け取る
        self.assertEqual(f.items["Q2"].state, "dont_know")
        f = self.flow()
        self.to_q2(f)
        self.report(f, "Q2", "それはちょっと言えないです。", "refused", "決められない")  # 答えたくない ⇒ 二度と聞かない（当てない）
        self.assertEqual(f.items["Q2"].state, "refused")

    def test_agree_is_taken(self):
        f = self.flow()
        self.report(f, "Q1", "はい、予約しています。", "はい", "はい")
        self.assertEqual(f.items["Q1"].state, "accepted")

    def test_unknown_with_an_answer_is_checked(self):
        # 「いや、これからなんです」をAIが「わからない」：判定は「いいえ」⇒ 判定の意味で言い直す（⛔ 前は確かめずに「-」）
        f = self.flow()
        r = self.report(f, "Q1", "いや、これからなんです。", "unknown", "いいえ")
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("ご予約はまだということ", r["say"])
        self.assertEqual(f.items["Q1"].condition, "いいえ")

    def test_any_for_not_decided_is_checked(self):
        # 「まだ決めてないです」をAIが「どちらでも」：判定は「わからない」⇒ AIの答えで言い直す（言い直す選択肢が判定に無い）
        f = self.flow()
        self.to_q2(f)
        r = self.report(f, "Q2", "まだ決めてないです。", "any", "unknown")
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("どちらでも構わない", r["say"])

    def test_choice_for_either_is_checked(self):
        # 「どちらでも大丈夫です」をAIが「カード」：判定は「どちらでも」⇒ 判定の意味で言い直す
        from flowctl import ANY
        f = self.flow()
        self.to_q2(f)
        r = self.report(f, "Q2", "どちらでも大丈夫です。", "カード", ANY)
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("どちらでも構わない", r["say"])

    def test_undecided_against_a_choice_is_checked(self):
        from flowctl import UNDECIDED
        f = self.flow()
        r = self.report(f, "Q1", "そうですね。", "はい", UNDECIDED)
        self.assertEqual(r["result"], "confirm_required")
        self.assertIn("ご予約はお済みということ", r["say"])

    def test_no_choice_on_both_sides_is_not_a_clash(self):
        # どちらも選択肢を選んでいない（わからない と 決められない）：言い直す答えは無いが、⛔ 2026-10-19 G-55 から、最初の答えだけは
        # 質問から聞き直す（「10名未満ではないです」＝答えの一部を言っている）。2回目の「わからない」は受け取る（何度も聞かない）
        from flowctl import UNDECIDED
        f = self.flow()
        self.assertEqual(self.report(f, "Q1", "ちょっとわからないです。", "unknown", UNDECIDED)["result"], "ask_back")
        self.report(f, "Q1", "ちょっとわからないです。", "unknown", UNDECIDED)
        self.assertEqual(f.items["Q1"].state, "dont_know")

    def test_no_judgment_keeps_the_old_way(self):
        f = self.flow()
        self.report(f, "Q1", "はい、予約しています。", "はい", None)
        self.assertEqual(f.items["Q1"].state, "accepted")

    def test_late_clash_is_not_handed_over_and_is_asked_again(self):
        # 判定が、受け取ったあとに届いて食い違った ⇒ 依頼主へ渡さない（「-」）・締めの前の念のための確認で、もう一度聞く
        f = self.flow()
        f.use_checks("judge")
        self.report(f, "Q1", "これからなんです。", "はい", None)      # 判定がまだ届いていない
        self.assertEqual(f.items["Q1"].state, "accepted")
        self.assertEqual(f.unchecked(), {"Q1": ["judge"]})              # 届かなければ通話後の検証へ
        f.on_check("judge", "いいえ")                                   # 同じ相手の発話の判定が、受け取ったあとに届いた
        self.assertEqual(f.unchecked(), {})
        self.assertEqual(f.items["Q1"].state, "unresolved")
        self.assertEqual(f.export()["Q1"], "-")
        r = self.report(f, "Q2", "カードでお願いします。", "カード", "カード")
        self.assertEqual((r["result"], r["question_id"]), ("recheck", "Q1"))

    def test_late_agreement_changes_nothing(self):
        f = self.flow()
        f.use_checks("judge")
        self.report(f, "Q1", "はい、予約しています。", "はい", None)
        f.on_check("judge", "はい")
        self.assertEqual((f.items["Q1"].state, f.unchecked()), ("accepted", {}))

    def test_late_check_belongs_to_its_utterance(self):
        # 遅れて届いた確かめは、その相手の発話への報告で受け取った項目にだけ当てる（あとの発話・聞き直したあとの答えには当てない）
        f = self.flow()
        self.report(f, "Q1", "はい、予約しています。", "はい", None)
        old = f.callee_turns
        self.report(f, "Q2", "カードでお願いします。", "カード", "カード")
        f.on_check("judge", "いいえ", turn=old)                       # Q1 の発話の判定（遅れた）
        self.assertEqual((f.items["Q1"].state, f.items["Q2"].state), ("unresolved", "accepted"))
        f.on_check("judge", "振込", turn=old)                         # Q2 は別の発話で受け取った ⇒ 当てない
        self.assertEqual(f.items["Q2"].state, "accepted")
        self.assertEqual(f.meaning_said, "カード")                      # いまの発話の判定は、古い発話の判定で書き換えない

    def test_late_ear_clash_is_not_handed_over(self):
        # 別の耳の文字が遅れて届いて食い違った ⇒ 判定と同じ扱い（どの確かめも、遅れたら同じ決まりで当てる）
        f = self.flow()
        self.report(f, "Q1", "捨てないです。", "はい", "はい")
        f.on_check("ear", "してないです")
        self.assertEqual(f.items["Q1"].state, "unresolved")

    def test_late_clash_during_recheck_is_asked_again(self):
        f = self.flow()
        self.report(f, "Q1", "はい、予約しています。", "はい", None)
        f.phase, f.current = "rechecking", "Q2"                          # 締めの前の念のための確認の最中に、遅れて届いて食い違った
        f.on_check("judge", "いいえ")
        self.assertEqual((f.items["Q1"].state, f.recheck_queue), ("unresolved", ["Q1"]))   # ⇒ 順番に足す

    def test_confirm_reply_is_checked_like_an_answer(self):
        # 言い直しへの返事も、最初の答えと同じ確かめに当てる：AIは「合っている」、判定は別の選択肢 ⇒ 判定の意味で、もう1回だけ言い直す
        f = self.flow()
        self.report(f, "Q1", "大丈夫です。", "はい", "はい")                # 曖昧な答え ⇒ 言い直して確かめる
        self.assertEqual(f.awaiting_confirm, "Q1")
        r = self.reply(f, "Q1", "あ、いえ、まだなんです。", "affirmed", "いいえ")
        self.assertEqual((r["result"], f.items["Q1"].condition), ("confirm_required", "いいえ"))
        self.assertIn("ご予約はまだということ", r["say"])
        r = self.reply(f, "Q1", "えっと、どうだったかな。", "affirmed", "はい")  # また食い違う（AIは「いいえ」で合っている、判定は「はい」）
        self.assertEqual((f.items["Q1"].state, f.export()["Q1"]), ("unresolved", "-"))   # 言い直しは1回まで ⇒「-」

    def test_value_confirm_reply_is_checked(self):
        # 値を聞く型：AIは「合っている」、判定は「決められない」⇒ もう1回だけ言い直す。判定が「合っている」なら確かめ済み
        from flowctl import Flow, Question, UNDECIDED
        f = Flow([Question("Q1", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))])
        f.on_callee(); f.on_heard("16日の午後2時で。")
        f.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "16日の午後2時"})
        r = self.reply(f, "Q1", "うーん、たぶん。", "affirmed", UNDECIDED)
        self.assertEqual((r["result"], f.items["Q1"].state), ("confirm_required", "checking"))
        r = self.reply(f, "Q1", "はい、それで大丈夫です。", "affirmed", "affirmed")
        self.assertEqual(f.items["Q1"].state, "confirmed")
        g = Flow([Question("Q1", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))])
        g.on_callee(); g.on_heard("16日の午後2時で。")
        g.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "16日の午後2時"})
        r = self.reply(g, "Q1", "いや、17日です。", "corrected", "affirmed", correction="17日の午後2時")
        self.assertEqual(g.items["Q1"].value, "17日の午後2時")             # 直された値は、もう一度言い直して確かめる（食い違っても誤りは渡らない）

    def test_hold_is_not_an_answer(self):
        # 取り次ぎを待つように言われた：答えとして受け取らず、AIは黙って待つ（差し戻しではない＝張り替えない・相づちも流さない）
        from flowctl import HANDOVER, HOLD
        f = self.flow()
        r = self.report(f, "Q1", "担当に代わりますので少々お待ちください。", "はい", HOLD)
        self.assertEqual((r["result"], r["ack"], f.items["Q1"].state, f.current), ("hold", None, "empty", "Q1"))
        self.assertTrue(f.holding)                                        # 電話接続側は、黙っている秒の見張りを止める
        r = self.report(f, "Q1", "お電話代わりました、田中です。", "はい", HANDOVER)
        self.assertFalse(f.holding)                                       # 次の相手の発話で解く
        self.assertEqual((r["result"], f.items["Q1"].state), ("rejected", "empty"))
        self.assertIn("代わった方が話し始めた", r["message"])
        r = self.report(f, "Q1", "はい、予約しています。", "はい", "はい")
        self.assertEqual(f.items["Q1"].state, "accepted")

    def test_handover_replay_waits_for_the_new_person(self):
        # ⛔ G-50：代わった方への名乗りを、相手の「少々お待ちください」の直後に流していた ⇒ 判定が「取り次ぎを待つ」の間は流さない
        from flowctl import Flow, HANDOVER, HOLD, Question
        f = Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"), meanings=self.M)],
                 opening={"greeting": "お世話になっております。", "intro": "青葉物産の佐藤です。", "purpose": "ご予約の確認です。"})
        f.on_callee(); f.on_heard("少々お待ちください。"); f.on_check("judge", HOLD)
        r = f.apply("replay_opening", {"part": "handover"})
        self.assertEqual((r["result"], f.replays), ("hold", 0))
        f.on_callee(); f.on_heard("お電話代わりました。"); f.on_check("judge", HANDOVER)
        r = f.apply("replay_opening", {"part": "handover"})
        self.assertEqual((r["result"], f.replays), ("replayed", 1))

    def test_system_answer_with_an_ear_clash_is_asked_again(self):
        # 「わからない」も、聞き取りが食い違えば、そのまま受け取らずに聞き直す（言い直す選択肢が無いので、質問から）
        f = self.flow()
        f.on_callee(); f.on_heard("わからないです。"); f.on_check("ear", "わかりました")
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "unknown"})
        self.assertEqual((r["result"], f.items["Q1"].state), ("ask_back", "unclear"))

    def test_each_utterance_starts_without_a_judgment(self):
        f = self.flow()
        f.on_check("judge", "はい")
        f.on_callee()
        self.assertIsNone(f.meaning_said)


class MeaningJudgeOptionsTest(unittest.TestCase):
    """意味の判定に見せる選べるもの（meaning_judge.options）：選択肢は意味の言葉を名前にする。⛔ G-53 の2回目：名前の「いいえ」と相手の
    「違います」を言葉で結びつけ、言い直しを認めていないのに認めたと読んだ（2回）。"""

    def test_choices_are_named_by_meaning(self):
        import json
        from flowctl import ANY, HOLD, Question
        from meaning_judge import options, prompt, read
        q = Question("Q1", "ご予約の日時に変更はございませんでしょうか？", "yes_no", conditions=("はい", "いいえ"),
                     meanings={"はい": "ご予約の日時に変更はないということ", "いいえ": "ご予約の日時に変更がおありということ"})
        names = [n for n, _, _ in options(q)]
        self.assertEqual(names[:2], ["ご予約の日時に変更はないということ", "ご予約の日時に変更がおありということ"])
        self.assertNotIn("- はい：", prompt(q, "違います。", "念のためご予約の日時に変更がおありということでよろしいでしょうか？"))
        self.assertEqual(read(q, json.dumps({"choice": "ご予約の日時に変更はないということ"})), "はい")
        self.assertEqual(read(q, json.dumps({"choice": HOLD})), HOLD)
        c = Question("Q2", "お支払いはどちらになさいますか？", "choice", conditions=("カード", "振込"), meanings={"カード": "わからない"})
        self.assertEqual([n for n, _, _ in options(c)][:3], ["カード", "振込", "どちらでも"])  # ほかの名前とかぶる意味の言葉は使わない
        self.assertEqual(read(c, json.dumps({"choice": "どちらでも"})), ANY)
        v = Question("Q3", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))
        self.assertEqual(read(v, json.dumps({"choice": "合っている"}), "念のため16日でよろしいでしょうか？"), "affirmed")
        self.assertIsNone(read(v, json.dumps({"choice": "はい"}), "念のため16日でよろしいでしょうか？"))


class UnreportedNoteTest(unittest.TestCase):
    """AIが声は出したのに、報告なしでターンを終えたときの、報告を促す知らせ（Flow.unreported_note）。
    ⛔ 2026-10-17：記録の通話で、「はい。」などのあとに報告が抜けた回が34回。プログラムは気づく手立てを持っていなかった。
    ⛔ 1回目は判定を見ずに出し、取り次ぎのあとの知らせで会話が乱れた ⇒ 意味の判定が「答え」と決めたときだけ。"""

    def flow(self):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"),
                              meanings={"はい": "ご予約はお済みということ", "いいえ": "ご予約はまだということ"})])

    def test_only_for_an_answer_once_per_utterance(self):
        f = self.flow()
        f.on_callee(); f.on_heard("はい。"); f.on_check("judge", "はい")
        note = f.unreported_note()
        self.assertIn("「はい。」", note)
        self.assertIn("record_answer", note)
        self.assertNotIn("ご予約はされていますでしょうか", note)   # 質問の文は入れない（AIが自分で質問し直さないように）
        self.assertIsNone(f.unreported_note())                      # 同じ発話には1回まで
        f.on_callee(); f.on_heard("ちょっとわからないです。"); f.on_check("judge", "unknown")
        self.assertIsNotNone(f.unreported_note())                   # 「わからない」も答え

    def test_not_for_words_that_are_not_an_answer(self):
        from flowctl import HOLD, UNDECIDED
        f = self.flow()
        for kind in (HOLD, UNDECIDED):
            f.on_callee(); f.on_heard("担当の者に代わりますので少々お待ちください。"); f.on_check("judge", kind)
            self.assertIsNone(f.unreported_note())                  # 取り次ぎ・考えている途中
        f.on_callee(); f.on_heard("はい。")
        self.assertIsNone(f.unreported_note())                      # 判定が無い

    def test_not_after_the_end_or_while_confirming(self):
        f = self.flow()
        f.on_callee(); f.on_heard("大丈夫です。")
        f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "大丈夫です。"})
        self.assertEqual(f.awaiting_confirm, "Q1")
        f.on_callee(); f.on_heard("はい、そうです。"); f.on_check("judge", "はい")
        self.assertIsNone(f.unreported_note())                      # 言い直しへの返事を待っている（報告は confirm_field）
        f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})
        f.on_callee(); f.on_heard("はい。"); f.on_check("judge", "はい")
        self.assertEqual(f.phase, "closing")
        self.assertIsNone(f.unreported_note())


class ChoicesNoteTest(unittest.TestCase):
    """会話のAIにも、選択肢の意味（取込で決めた意味の言葉）を知らせる（flowctl.choices_note の1か所）。⛔ 2026-10-17：選択肢の名前しか
    見せておらず、打ち消しの問いの「はい」の向きをAIが自分で決めていた。"""

    def test_note_on_every_question_announcement(self):
        from flowctl import Flow, Question, choices_note
        from instruction import Business, build
        q1 = Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))
        q2 = Question("Q2", "ご不明な点はございませんでしょうか？", "yes_no", conditions=("はい", "いいえ"),
                      meanings={"はい": "ご不明な点はないということ", "いいえ": "ご不明な点がおありということ"})
        self.assertEqual(choices_note(q1), "")                                  # 意味の言葉が無ければ足さない
        self.assertIn("「はい」＝ご不明な点はないということ", choices_note(q2))
        for play in (False, True):
            f = Flow([q1, q2], play_recordings=play)
            f.on_callee(); f.on_heard("はい、しています。")
            r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "はい、しています。"})
            self.assertIn("ご不明な点はないということ", r["message"])               # 次の質問の知らせ（録音で流す形も）
        b = Business(company="青葉物産", caller="佐藤", purpose="確認", questions=[q2, q1])
        for mode in ("ai", "recorded"):
            _, text = build(b, "S4", opening_mode=mode)
            self.assertIn("ご不明な点はないということ", text)                       # 最初の質問


class ResendTest(unittest.TestCase):
    def test_resend_keeps_the_count_and_clears_the_words(self):
        # 張り替えて同じ発話を送り直す（6-7）：相手は話していないので数えず、文字だけを受け取り直す（⛔ 前は同じ言葉が2回たまった）
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))])
        f.on_callee(); f.on_heard("はい。"); f.on_check("ear", "はい"); f.on_check("judge", "はい")
        n = f.callee_turns
        f.on_resend(); f.on_heard("はい。")
        self.assertEqual((f.callee_turns, f.callee_said, f.ear_said, f.meaning_said), (n, "はい。", None, None))


class AmbiguousAnswerTest(unittest.TestCase):
    """DESIGN.md 6-10 の (3)：受け入れにも断りにも取れる答え（「大丈夫です」「結構です」）は、AIが選んだ選択肢のまま渡さず、意味を確かめる。
    ⛔ G-14：AIは24本中21本で、曖昧な答えを「はい」「いいえ」に決めた。⛔ 2026-10-17 に見つけた穴：選択肢の型の言い直しが
    「はいとおっしゃいましたでしょうか？」になり、言い直しを打ち消されても選択肢は「はい」のままだった（依頼主へ「はい」が渡る）。"""
    M = {"はい": "ご予約はお済みということ", "いいえ": "ご予約はまだということ"}

    def flow(self, meanings=None, required=True, play=False):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"), required=required,
                              meanings=self.M if meanings is None else meanings),
                     Question("Q2", "お支払いはカードと振込のどちらがよろしいでしょうか？", "choice", conditions=("カード", "振込"))],
                    play_recordings=play)

    @staticmethod
    def say(f, heard, op, **args):
        f.on_callee(); f.on_heard(heard)
        return f.apply(op, args)

    def test_list(self):
        # 曖昧な答えの決まりは、流れの制御の比べ（Flow._compare）の1か所で当てる
        from flowctl import Flow, Question
        q = Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))

        def why(t, reported="はい", policy=True, confirming=False):
            return Flow._compare(q, t or "", "affirmed" if confirming else reported, {}, policy=policy, confirming=confirming)[0]
        from flowctl import HEDGE_WHY
        for t in ("大丈夫です。", "結構です", "いいです。", "あ、大丈夫です。", "えーと、結構です。", "大丈夫です、大丈夫です。", "いいですよ。"):
            self.assertEqual(why(t), "曖昧な答え", t)
        for t in ("家族がしたかもしれないです。", "たぶんカードです。", "多分してます", "おそらくまだです", "したかも。"):  # 言い切っていない答え
            self.assertEqual(why(t), HEDGE_WHY, t)
        # 認める・打ち消す言葉が付いていれば、その言葉で決まる。「いいえ」は打ち消し。文字が無ければ決めない。「と思います」は言い切りの柔らかい形
        for t in ("はい、大丈夫です。", "いいえ。", "いえ、結構です。", "どちらでも大丈夫です。", "はい、予約しています。", "", None,
                  "カードがいいと思います。", "かもめ便でお願いします", "鴨川に住んでいます"):
            self.assertIsNone(why(t), t)
        # 言い直しへの返事：「大丈夫です」は認める返事（決まりを当てない）・「たぶんそうです」は言い切っていないが、聞き直さずに受け取る
        # （値に「曖昧」の印を付ける＝Flow._hedged。✅ 2026-10-18 ユーザー：「yes,but 曖昧」）
        self.assertIsNone(why("大丈夫です。", policy=False, confirming=True))
        self.assertIsNone(why("たぶんそうです。", policy=False, confirming=True))
        self.assertTrue(Flow._hedged("たぶんそうです。", {}))
        self.assertFalse(Flow._hedged("はい、そうです。", {}))
        # 「わからない」の報告には、言い切っていない決まりを当てない（「たぶん分からないです」は、そのまま「わからない」）
        self.assertIsNone(why("たぶん分からないです。", reported="unknown", policy=False))

    def test_ambiguous_is_confirmed_by_meaning(self):
        f = self.flow()
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="はい", value="大丈夫です")
        self.assertEqual(r["result"], "confirm_required")
        self.assertEqual(r["say"], "念のためご予約はお済みということでよろしいでしょうか？")
        self.assertEqual(r["ack"], "hai")
        self.assertNotIn("大丈夫でしょうか", r["say"])   # 相手の「大丈夫です」をそのまま聞き返さない
        r = self.say(f, "はい。", "confirm_field", question_id="Q1", result="affirmed")
        self.assertEqual((f.items["Q1"].state, f.export()["Q1"]), ("confirmed", "はい"))

    def test_hedged_reply_is_taken_with_a_mark(self):
        # ✅ 2026-10-18 ユーザー：「『していないはずですが…』『5万円くらいかなとは思いますが』なんかは、『yes,but 曖昧』のような返答にしておけば
        #    いいんじゃないですか？」⇒ 言い直しへの返事が言い切っていなければ、もう一度は聞かず、値に「曖昧」の印を付けて受け取る
        from meaning_judge import Reading
        f = self.flow()
        f.use_checks("judge")
        f.on_callee(); f.on_heard("大丈夫です。"); f.on_check("judge", Reading("はい"))
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "大丈夫です"})
        self.assertEqual(r["result"], "confirm_required")
        # 言い切っていない言葉（たぶん・はず…）が無くても、意味の判定が言い切っていないと読めば印を付ける
        f.on_callee(); f.on_heard("予約したと言えばしたんですが。"); f.on_check("judge", Reading("はい", firm=False))
        r = f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})
        self.assertEqual(f.items["Q1"].state, "confirmed")
        # 依頼主へは、値はそのまま・別の欄にお客様の言葉（✅ 2026-10-18 ユーザー：案C）
        self.assertEqual((f.export()["Q1"], f.hedged_words()), ("はい", {"Q1": "予約したと言えばしたんですが。"}))

    def test_offer_restatement_echoes_the_word(self):
        # ✅ 2026-10-18 ユーザー：「『はい、念のためパンフレットをお送りして大丈夫だということでよろしかったでしょうか？』語尾に『よろしい』が
        #    2回は違和感。相手が言ったことを聞き返した方が確か」⇒ 意味の言葉の「よろしい」を、相手の言葉に替える（語尾と同じ言葉を重ねない）
        from flowctl import Flow, Question
        from phrases import confirm, restate_words
        q = Question("Q8", "パンフレットをお送りしてもよろしいでしょうか？", "yes_no", conditions=("はい", "いいえ"),
                     meanings={"はい": "パンフレットをお送りしてよろしいということ", "いいえ": "パンフレットはお送りしなくてよろしいということ"})
        f = Flow([q])
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q8", condition_id="はい", value="大丈夫です")
        self.assertEqual(r["say"], "念のためパンフレットをお送りして大丈夫だということでよろしいでしょうか？")
        self.assertEqual(restate_words("パンフレットはお送りしなくてよろしいということ", "結構です。"), "パンフレットはお送りしなくて結構だということ")
        self.assertEqual(restate_words("パンフレットをお送りしてよろしいということ", "いいです"), "パンフレットをお送りしていいということ")
        self.assertEqual(restate_words("ご予約はお済みということ", "大丈夫です。"), "ご予約はお済みということ")  # 「よろしい」が無ければそのまま
        # 語尾は、言い直す言葉と同じ言葉を使わない形だけから選ぶ
        for n in range(4):
            said = confirm("パンフレットをお送りして大丈夫だということ", "ふつう", n, meaning=True)
            self.assertEqual(said.count("大丈夫"), 1, said)

    def test_hedged_first_answer_is_asked_softly(self):
        # ✅ 2026-10-18 ユーザー：言い切っていない最初の答えは「はい、念のためご予約はお済みということでよろしかったでしょうか？」が自然
        f = self.flow()
        r = self.say(f, "たぶんしてます。", "record_answer", question_id="Q1", condition_id="はい", value="たぶんしてます")
        self.assertEqual(r["result"], "confirm_required")
        self.assertEqual((r["say"], r["ack"]), ("念のためご予約はお済みということでよろしかったでしょうか？", "hai"))

    def test_late_unsure_judge_marks_the_reply(self):
        # 言い直しへの返事を受け取ったあとに、2つ目の判定（遅い）が「言い切っていない」と届いた：値は変えず、お客様の言葉を別の欄に残す
        # （通話の終わりのあとに届いても同じ＝convo.run_call は、走っている確かめを届くまで待って当てる）
        from meaning_judge import Reading
        f = self.flow()
        f.use_checks("judge", "judge2")
        f.on_callee(); f.on_heard("大丈夫です。"); f.on_check("judge", Reading("はい"))
        f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "大丈夫です"})
        f.on_callee(); f.on_heard("ええ、そうだと思うんですけど。"); f.on_check("judge", Reading("はい"))
        f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})
        self.assertEqual((f.items["Q1"].state, f.hedged_words()), ("confirmed", {}))
        f.on_check("judge2", Reading("はい", firm=False))
        self.assertEqual((f.export()["Q1"], f.hedged_words()), ("はい", {"Q1": "ええ、そうだと思うんですけど。"}))

    def test_hedged_answer_is_asked_softly_whatever_the_trigger(self):
        # AIは「わからない」、判定は「していない」（言い切っていない答え）：食い違いがきっかけでも、言い方は言い切っていない答えのもの
        from meaning_judge import Reading
        f = self.flow()
        f.use_checks("judge")
        f.on_callee(); f.on_heard("たぶんしてないと思います。"); f.on_check("judge", Reading("いいえ", firm=False))
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "unknown", "value": "たぶんしてない"})
        self.assertEqual(r["result"], "confirm_required")
        self.assertTrue(r["say"].endswith("よろしかったでしょうか？"), r["say"])

    def test_unsure_first_answer_is_confirmed(self):
        # 最初の答えは、意味の判定が言い切っていないと読めば、言い直して確かめる（言い切っていない答えの決まりと同じ）
        from meaning_judge import Reading
        f = self.flow()
        f.use_checks("judge")
        f.on_callee(); f.on_heard("予約したと言えばしたんですが。"); f.on_check("judge", Reading("はい", firm=False))
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "予約したと言えばした"})
        self.assertEqual(r["result"], "confirm_required")
        self.assertTrue(any("言い切っていない" in why for _, _, why in f.items["Q1"].history))

    def test_denied_meaning_takes_the_named_choice(self):
        f = self.flow()
        self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="はい", value="大丈夫です")
        r = self.say(f, "いえ、まだです。", "confirm_field", question_id="Q1", result="corrected", correction="いいえ")
        self.assertEqual(r["say"], "改めましてご予約はまだということで大丈夫でしょうか？")
        self.say(f, "はい。", "confirm_field", question_id="Q1", result="affirmed")
        self.assertEqual(f.export()["Q1"], "いいえ")

    def test_correction_must_be_a_choice(self):
        f = self.flow()
        self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="はい", value="大丈夫です")
        r = self.say(f, "いえ、まだです。", "confirm_field", question_id="Q1", result="corrected", correction="まだです")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("はい、いいえ、わからない、答えたくない", r["message"])
        self.assertEqual(f.items["Q1"].condition, "はい")  # 記録は書き換えない
        r = f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "わからない"})  # 同じ発話への報告し直し
        self.assertEqual((f.items["Q1"].state, f.export()["Q1"]), ("dont_know", "-"))

    def test_clear_answer_is_not_confirmed(self):
        f = self.flow()
        r = self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        self.assertEqual(r["result"], "next")
        self.assertEqual(f.items["Q1"].state, "accepted")

    def pamphlet(self):
        # 前の質問から意味が決まる質問（G-49：2つのモデルが「結構」＝「いいえ」と決めた）
        from flowctl import Flow, Question
        return Flow([Question("Q3", "パンフレットをお送りしてもよろしいでしょうか？", "yes_no", conditions=("はい", "いいえ"),
                              meanings={"はい": "パンフレットをお送りしてもよろしいということ", "いいえ": "パンフレットはお送りしなくてよろしいということ"},
                              reply_meanings={"結構": "いいえ"})])

    def test_question_decides_the_word(self):
        # ✅ 2026-10-17 ユーザー：「人でも前の質問や会話から判断する言葉です」⇒ 前の質問で決まれば、確かめずにその意味（AIの選んだものと違っても）
        f = self.pamphlet()
        r = self.say(f, "結構です。", "record_answer", question_id="Q3", condition_id="はい", value="結構です")
        self.assertEqual((r["result"], f.items["Q3"].state, f.export()["Q3"]), ("end", "accepted", "いいえ"))
        f = self.pamphlet()
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q3", condition_id="はい", value="大丈夫です")
        self.assertEqual(r["result"], "confirm_required")  # 「大丈夫」は、この質問でも決まらない ⇒ 確かめる

    def test_word_alone_does_not_end_the_call(self):
        # ⛔ G-14：相手の「結構です。」だけで、AIが通話の断りとして締めた（30本中4本）
        f = self.flow()
        r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("record_answer", r["message"])
        self.assertFalse(f.hurried)
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "いいえ", "value": "結構です"})
        self.assertEqual(r["say"], "念のためご予約はまだということでよろしいでしょうか？")
        r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")  # 確かめたあとにまた言われた＝断りとして締める
        self.assertEqual((r["result"], f.hurried), ("end", True))
        f = self.pamphlet()  # 前の質問で意味が決まる：その答えで受け取って続ける
        r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")
        self.assertEqual((f.items["Q3"].state, f.export()["Q3"], f.hurried), ("accepted", "いいえ", False))
        f = self.flow()  # 断りの言葉が付いていれば、断り
        r = self.say(f, "もう結構です、かけてこないでください。", "request_end", reason="callee_refused_call")
        self.assertEqual(r["result"], "end")

    def test_value_questions_follow_the_same_rule(self):
        # ⛔ 2026-10-17 の見直しで見つけた漏れ：(1) 名前・電話番号の質問に「結構です」「はい」だけ ⇒「結構ということでよろしいでしょうか？」と
        #    言い直していた (2) 値を聞く質問では、「結構です」だけで AIが断りと報告すると締めていた ⇒ どの型も同じ決まり（_ambiguous_only）
        from flowctl import Flow, Question
        for qt in ("datetime", "number", "name", "phone", "email", "address"):
            for said in ("結構です。", "はい。", "大丈夫です。"):
                f = Flow([Question("Q1", "質問の文", qt, conditions=("答えた",), topic="ご希望")])
                r = self.say(f, said, "record_answer", question_id="Q1", condition_id="答えた", value=said.rstrip("。"))
                self.assertEqual(r["result"], "ask_back", (qt, said))  # 答えの値ではない ⇒ 聞き返す（言い直さない）
            f = Flow([Question("Q1", "質問の文", qt, conditions=("答えた",), topic="ご希望")])
            r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")
            self.assertEqual((r["result"], f.hurried), ("ask_back", False), qt)    # その言葉だけでは締めない
            r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")
            self.assertEqual((r["result"], f.hurried), ("end", True), qt)          # 聞き返したあとにまた言われたら、断りとして締める
        f = Flow([Question("Q1", "ご要望をお聞かせいただけますでしょうか？", "free_text", conditions=("答えた",))])
        r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")
        self.assertEqual(r["result"], "rejected")
        r = self.say(f, "結構です。", "request_end", reason="callee_refused_call")  # 差し戻しは1回だけ（止まらない）
        self.assertEqual(r["result"], "end")
        g = Flow([Question("Q1", "ご担当者様のお名前をお伺いしてもよろしいでしょうか？", "name", conditions=("答えた",))])
        r = self.say(g, "山田です。", "record_answer", question_id="Q1", condition_id="答えた", value="山田")
        self.assertEqual(r["result"], "confirm_required")  # ふつうの名前は、これまでどおり言い直す

    def test_system_answer_on_the_word_alone(self):
        # ⛔ G-14 の続き：日時の質問への「大丈夫です」を、AIが「わからない」にした（聞き返さずに「-」）⇒ その言葉だけでは決めない
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",), topic="ご希望の日時")])
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="unknown")
        self.assertEqual(r["result"], "ask_back")
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="unknown")  # 聞き返したあとは、AIの報告のまま
        self.assertEqual(f.items["Q1"].state, "dont_know")
        f = self.flow()
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="unknown")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("選択肢（はい、いいえ）から近いもの", r["message"])
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "大丈夫です"})
        self.assertEqual(r["say"], "念のためご予約はお済みということでよろしいでしょうか？")
        f = self.flow()  # 言い切っていない答え・はっきりした「わからない」は、これまでどおり「わからない」
        for said in ("家族がしたかもしれないです。", "ちょっとわからないです。"):
            f = self.flow()
            self.say(f, said, "record_answer", question_id="Q1", condition_id="unknown")
            self.assertEqual(f.items["Q1"].state, "dont_know", said)

    def test_rejection_and_resume_tell_how_to_report_a_choice(self):
        # ⛔ 2026-10-17 の見直しで見つけた漏れ：(1) 差し戻しの「いま求めていること」が、選択肢でも「違う値…訂正された値で」とだけ言っていた
        #    (2) 張り替えの「いまの位置」が、確かめの言い方ではなく相手の言葉（「大丈夫です」）・中の名前（any）を見せていた
        from instruction import Business, position_text
        f = self.flow()
        self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="はい", value="大丈夫です")
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})  # 確かめの返事待ちに、もう一度答えを報告した
        self.assertEqual(r["result"], "rejected")
        self.assertIn("選択肢の名前（はい、いいえ、わからない、答えたくない）", r["message"])
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", purpose_line="ご予約の確認でお電話いたしました。",
                     questions=list(f.questions.values()))
        text = position_text(b, f)
        self.assertIn("「念のためご予約はお済みということでよろしいでしょうか？」と確かめて", text)
        self.assertNotIn("値「大丈夫です」", text)
        g = self.flow()
        self.say(g, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        self.say(g, "どちらでもいいです。", "record_answer", question_id="Q2", condition_id="any")
        text = position_text(b, g)
        self.assertIn("⇒ はい（済み）", text)
        self.assertIn("⇒ どちらでも（済み）", text)
        self.assertNotIn("any", text)

    def test_lead_word_is_left_to_the_judge(self):
        # ⛔ 2026-10-18 に頭の言葉の確かめ（「いや」で始まるのにAIが「はい」）を外した：打ち消しの問いでは、頭の言葉が正しい答えと逆を指す
        #    （G-52 の B で 82個中21個）。意味の判定が受け持つ ⇒ 判定が届けば確かめる・頭の言葉だけでは確かめない
        import flowctl
        self.assertFalse(hasattr(flowctl, "reply_lead"))
        f = self.flow()
        f.on_callee(); f.on_heard("いや、これからなんです。"); f.on_check("judge", "いいえ")
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "これから"})
        self.assertEqual((r["result"], r["say"]), ("confirm_required", "念のためご予約はまだということでよろしいでしょうか？"))
        q = flowctl.Question("Q1", "ご不明な点はございませんでしょうか？", "yes_no", conditions=("はい", "いいえ"),
                             meanings={"はい": "ご不明な点はないということ", "いいえ": "ご不明な点がおありということ"})
        g = flowctl.Flow([q])
        g.on_callee(); g.on_heard("いえ、特にありません。"); g.on_check("judge", "はい")
        r = g.apply("record_answer", {"question_id": "Q1", "condition_id": "はい", "value": "特にありません"})
        self.assertEqual(g.items["Q1"].state, "accepted")              # 頭の「いえ」でも、意味は「はい」（不明な点はない）＝確かめない

    def test_any_is_an_answer_for_choice(self):
        # ✅ 2026-10-17 ユーザー：選択肢の質問の「どちらでも大丈夫です」は答え（「いつでも」と同じ）。はい／いいえの質問には無い
        from flowctl import Flow
        f = self.flow()
        self.assertNotIn("どちらでも", f.questions["Q1"].labels)
        self.assertEqual(f.questions["Q2"].labels, ("カード", "振込", "どちらでも", "わからない", "答えたくない"))
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい")
        r = f.handle(type("FC", (), {"name": "record_answer", "args": {"質問": "Q2", "答えの種類": "どちらでも", "値": "どちらでも大丈夫です"}})())
        self.assertEqual((f.items["Q2"].state, f.export()["Q2"]), ("accepted", "どちらでも"))
        self.assertNotIn("any", str(r))  # AIへの返事に中の名前を出さない
        g = Flow([f.questions["Q1"]])
        r = self.say(g, "どちらでもいいです。", "record_answer", question_id="Q1", condition_id="any")
        self.assertEqual(r["result"], "rejected")  # はい／いいえの質問の選択肢には無い

    def test_any_after_ask_back_is_restated(self):
        f = self.flow()
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい")
        self.say(f, "え？", "report_unclear", question_id="Q2")
        r = self.say(f, "どちらでも大丈夫です。", "record_answer", question_id="Q2", condition_id="any")
        self.assertTrue(r["say"].endswith("どちらでも構わないということでよろしかったでしょうか？"), r["say"])
        r = self.say(f, "いえ、カードでお願いします。", "confirm_field", question_id="Q2", result="corrected", correction="カード")
        self.assertEqual(f.items["Q2"].condition, "カード")

    def test_transcript_missing_uses_reported_words(self):
        f = self.flow()
        r = self.say(f, "", "record_answer", question_id="Q1", condition_id="いいえ", value="結構です")
        self.assertEqual(r["say"], "念のためご予約はまだということでよろしいでしょうか？")

    def test_without_meaning_words_asks_again(self):
        # 意味の言葉が取込で決まらず、選択肢が「はい」「いいえ」だけ：言い直せないので聞き返す（止まらない）
        f = self.flow(meanings={})
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="はい", value="大丈夫です")
        self.assertEqual(r["result"], "ask_back")

    def test_not_required_becomes_dash(self):
        f = self.flow(required=False)
        r = self.say(f, "大丈夫です。", "record_answer", question_id="Q1", condition_id="はい", value="大丈夫です")
        self.assertEqual((r["result"], f.items["Q1"].state), ("next", "unresolved"))

    def test_choice_label_is_its_own_words(self):
        # 選択肢の名前が言葉になっている（「カード」）ときは、名前のまま言い直せる。聞き返したあとの言い直し（6-11）
        f = self.flow()
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい")
        self.say(f, "え？", "report_unclear", question_id="Q2")
        r = self.say(f, "カードで。", "record_answer", question_id="Q2", condition_id="カード", value="カードで")
        self.assertTrue(r["say"].endswith("カードでよろしかったでしょうか？"), r["say"])
        self.assertNotIn("おっしゃいましたでしょうか", r["say"])  # 意味の言い直しに、言葉を聞き返す形は使わない

    def test_yes_no_after_ask_back_without_words_is_accepted(self):
        # ⛔ 直す前：「はいとおっしゃいましたでしょうか？」と言い直していた。言い直す言葉が無ければ、聞き取れた答えのまま受け付ける
        f = self.flow(meanings={})
        self.say(f, "え？", "report_unclear", question_id="Q1")
        r = self.say(f, "はい。", "record_answer", question_id="Q1", condition_id="はい", value="はい")
        self.assertEqual((r["result"], f.items["Q1"].state), ("next", "accepted"))

    def test_meanings_only_for_declared_choices(self):
        from flowctl import Question
        with self.assertRaises(AssertionError):
            Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"), meanings={"たぶん": "x"})
        with self.assertRaises(AssertionError):
            Question("Q2", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",), meanings={"答えた": "x"})


class ReplayOpeningTest(unittest.TestCase):
    """名乗りを録音で流し直す（DESIGN.md 6-13。✅ 2026-10-13 ユーザー：名乗り直しも録音で流す）。⛔ G-22・G-29：AIが自分で名乗り直すと、
    社名が正しく聞こえない（24本中3本）。AIは部分を選ぶだけで、流すのは流れの制御が渡す録音。"""

    def flow(self, play=True, **kw):
        from flowctl import Flow
        from g22_opening import BUSINESS
        return Flow(BUSINESS.questions, play_recordings=play, opening=BUSINESS.replay_parts(), **kw), BUSINESS

    def test_parts(self):
        f, b = self.flow()
        intro, purpose, q1 = b.opening_parts()[1], b.purpose_line, b.questions[0].text
        for part, want, ack in (("intro", [intro, q1], "shitsurei"), ("purpose", [purpose, q1], "shitsurei"),
                                ("all", [intro, purpose, q1], "shitsurei")):
            f, _ = self.flow()
            f.on_callee()
            r = f.apply("replay_opening", {"part": part})
            self.assertEqual(([p["text"] for p in r["play"]], r["ack"]), (want, ack), part)
        f, _ = self.flow()
        r = f.apply("replay_opening", {"part": "handover"})  # 代わった方へ：あいさつから。お詫びの相づちは流さない
        self.assertEqual(([p.get("part", p["kind"]) for p in r["play"]], r["ack"]), (["greeting", "intro", "purpose", "question"], None))

    def test_silent_and_state(self):
        f, _ = self.flow()
        f.on_callee()
        reply, silent = f.handle_call(type("FC", (), {"name": "replay_opening", "args": {"流し直す部分": "名乗り"}})())
        self.assertTrue(silent)                            # 録音を流すので、AIは話さない
        self.assertEqual((f.current, f.items["Q1"].state), ("Q1", "empty"))  # 質問の位置は変わらない
        self.assertIn("録音で流しました", reply["次にすること"])

    def test_limit(self):
        f, _ = self.flow()
        for _ in range(2):
            self.assertEqual(f.apply("replay_opening", {"part": "all"})["result"], "replayed")
        r = f.apply("replay_opening", {"part": "all"})  # 3回目は流さない（使い切ったら、AIが短く答える＝止まらない）
        self.assertEqual(r["result"], "rejected")

    def test_while_waiting_for_a_restatement(self):
        # 言い直しの返事待ちのときは、録音のあとにAIが言い直しをもう一度言う（値で変わるので録音にできない）
        f, _ = self.flow()
        f.on_callee(); f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})
        f.on_callee(); f.on_heard("15日の午後2時でお願いします。")
        r = f.apply("record_answer", {"question_id": "Q2", "condition_id": "答えた", "value": "15日の午後2時"})
        f.on_callee(); f.on_heard("え、どちら様ですか？")
        reply, silent = f.handle_call(type("FC", (), {"name": "replay_opening", "args": {"流し直す部分": "名乗り"}})())
        self.assertFalse(silent)
        self.assertEqual([p["part"] for p in f.log[-1]["reply"]["play"]], ["intro"])
        self.assertIn(f.awaiting_say, reply["次にすること"])

    def test_without_recorded_questions_ai_asks(self):
        f, _ = self.flow(play=False)
        reply, silent = f.handle_call(type("FC", (), {"name": "replay_opening", "args": {"流し直す部分": "用件"}})())
        self.assertFalse(silent)
        self.assertIn("ご予約はされていますでしょうか？", reply["次にすること"])

    def test_unheard_parts_are_added(self):
        # 録音の途中で止めたときは、まだ届いていない名乗り・用件を、AIが選んだ部分に足して流す（⛔ G-50：用件が届く前の「どちら様」に、
        # AIは「名乗り」だけを選んだ＝用件が一度も届かない）。あいさつは足さない。一度流したら、次からは足さない
        f, b = self.flow()
        greeting, intro, purpose, q1 = b.opening_parts()
        for done, part, want in (([], "intro", [intro, purpose, q1]), ([greeting], "intro", [intro, purpose, q1]),
                                 ([greeting, intro], "purpose", [purpose, q1]), ([greeting, intro], "intro", [intro, purpose, q1]),
                                 ([greeting, intro, purpose], "intro", [intro, q1])):
            f, _ = self.flow()
            f.on_opening_cut(done)
            f.on_callee()
            self.assertEqual([p["text"] for p in f.apply("replay_opening", {"part": part})["play"]], want, (done, part))
        f, _ = self.flow()
        f.on_opening_cut([greeting])
        f.apply("replay_opening", {"part": "intro"})
        self.assertEqual([p["text"] for p in f.apply("replay_opening", {"part": "intro"})["play"]], [intro, q1])

    def test_rule_after_session_swap(self):
        # 張り替えのあとの「いまの位置」にも、流し直しの決まりを渡す（⛔ 見直しで見つけた漏れ：最初のセッションの文にだけあった）
        from flowctl import Flow
        from instruction import position_text
        f, b = self.flow()
        self.assertIn("replay_opening", position_text(b, f))
        self.assertNotIn("replay_opening", position_text(b, Flow(b.questions)))

    def test_no_question_left(self):
        # 質問が全部済んだあと（締めの最中など）：質問の録音を足さず、答えの報告も求めない（⛔ 見直しで見つけた漏れ）
        f, _ = self.flow()
        f.current, f.phase = None, "closing"
        reply, silent = f.handle_call(type("FC", (), {"name": "replay_opening", "args": {"流し直す部分": "名乗り"}})())
        self.assertTrue(silent)
        self.assertEqual([p.get("part", p["kind"]) for p in f.log[-1]["reply"]["play"]], ["intro"])
        self.assertNotIn("record_answer", reply["次にすること"])

    def test_no_opening_recording(self):
        from flowctl import Flow
        from g22_opening import BUSINESS
        r = Flow(BUSINESS.questions).apply("replay_opening", {"part": "intro"})
        self.assertEqual(r["result"], "rejected")


class RequestTailTest(unittest.TestCase):
    """⛔ G-46：AIが「15日の午後2時でお願いします」を値にして報告し、言い直しが「…でお願いしますということでよろしいでしょうか？」になった。"""

    def test_request_tail(self):
        from flowctl import normalize_value
        self.assertEqual(normalize_value("datetime", "15日の午後2時でお願いします。"), "15日の午後2時")
        self.assertEqual(normalize_value("number", "3人でお願いいたします", "人"), "3人")
        self.assertEqual(normalize_value("datetime", "いつでも大丈夫です"), "いつでも大丈夫")   # 聞いていただいた形は変えない
        self.assertEqual(normalize_value("name", "なかしまでお願いします"), "なかしまでお願いします")  # 名前の型には当てない


class ReadingConfirmTest(unittest.TestCase):
    """読みが決まらない名前を、会話の中で確かめる（flowctl.reading_question）。✅ 2026-10-16 ユーザー：「お名前はやまざき様でよろしかった
    でしょうか」「お名前を確認させていただきたいのですが、ヤマザキ様とお呼びしたらよろしいでしょうか？」。ほかの値と同じ言い直しの流れ。"""

    def flow(self):
        from flowctl import Flow, Question, reading_question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
                     reading_question("N1", "山崎", "やまざき"),
                     Question("Q2", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",))])

    @staticmethod
    def do(f, name, **args):
        f.on_callee()
        return f.apply(name, args)

    def test_affirmed(self):
        f = self.flow()
        r = self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        self.assertEqual(r["result"], "confirm_required")  # 次の質問の代わりに、読みの確かめ（相手に聞かずに言い直しの形で始まる）
        self.assertEqual(r["say"], "お名前の読みの方は「やまざき」様でよろしかったでしょうか？")
        r = self.do(f, "confirm_field", question_id="N1", result="affirmed")
        self.assertEqual((r["result"], r["question_id"]), ("next", "Q2"))
        self.assertIn("「山崎」様は「やまざき」様とお呼びします", r["message"])
        self.assertEqual(f.readings(), {"山崎": "やまざき"})

    def test_reading_like_a_reply_word(self):
        # 返事の言葉と同じ音の読み（「井伊」＝いい）も、読みの確かめでは値の形の確かめに当てない（2026-10-17：返事の言葉だけの値を名前の型で
        # 止めるようにしたため）
        from flowctl import Flow, Question, reading_question
        f = Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
                  reading_question("N1", "井伊", "いのい", ["いのい", "いい"])])
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="いい")
        self.assertEqual(r["say"], "改めましてお名前の読みの方は「いい」様でよろしかったでしょうか？")

    def test_corrected_then_affirmed(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="ヤマサキです")
        self.assertEqual((r["result"], r["ack"]), ("confirm_required", "shitsurei"))  # こちらの読み違え＝お詫び「失礼いたしました。」
        self.assertEqual(r["say"], "改めましてお名前の読みの方は「やまさき」様でよろしかったでしょうか？")
        r = self.do(f, "confirm_field", question_id="N1", result="affirmed")
        self.assertEqual(f.readings(), {"山崎": "やまさき"})
        self.assertEqual(r["question_id"], "Q2")

    def test_kanji_correction_is_rejected(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="山崎")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("ひらがなだけ", r["message"])
        self.assertEqual(f.items["N1"].value, "やまざき")  # 記録は書き換えない

    def test_unconfirmed_means_no_name(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="はい")
        r = self.do(f, "confirm_field", question_id="N1", result="no_answer")
        self.assertIn("お名前を使わずに", r["message"])
        self.assertEqual(f.readings(), {})

    def test_first_question(self):
        from flowctl import Flow, reading_question
        f = Flow([reading_question("N1", "中島", "なかじま")])   # 最初の質問なら、始めから返事待ち（名乗りのあとに言われる）
        self.assertEqual(f.awaiting_confirm, "N1")
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="なかしま")
        self.assertEqual(r["say"], "改めましてお名前の読みの方は「なかしま」様でよろしかったでしょうか？")

    def test_next_reading(self):
        """次に確かめる読みを決める1か所（flowctl.next_reading）。⛔ G-46：「いや、カドタです。」を、AIは「かどだ」と報告した。"""
        from flowctl import next_reading
        c3 = ["つのだ", "かくた", "かどた"]
        self.assertEqual(next_reading("かどだ", c3, ["つのだ"]), "かどた")     # 濁りだけ違う ⇒ 候補の読み
        self.assertEqual(next_reading("かくた", c3, ["つのだ"]), "かくた")     # 候補そのもの
        self.assertEqual(next_reading("かぐた", c3, ["つのだ"]), "かくた")
        self.assertEqual(next_reading("つのだ", c3, ["つのだ"]), "かくた")     # 言った読みと同じ ⇒ まだ言っていない候補の最初
        self.assertEqual(next_reading(None, c3, ["つのだ"]), "かくた")
        self.assertEqual(next_reading("つのた", c3, ["つのだ"]), "かくた")     # 言った読みを濁りだけ違えて聞いた形 ⇒ 使わない
        self.assertEqual(next_reading("すみた", c3, ["つのだ"]), "すみた")     # 候補に無い読みを相手が言った
        self.assertEqual(next_reading("かどだ", c3, ["つのだ", "かくた", "かどた"]), None)  # 候補が尽きた（言った読みと濁りだけ違う）…
        self.assertEqual(next_reading("やまさき", ["やまざき"], ["やまざき"]), "やまさき")  # ほかに候補が無ければ、濁りだけ違う訂正も使う
        self.assertEqual(next_reading("なかしま", ["なかじま", "なかしま"], ["なかじま"]), "なかしま")
        self.assertEqual(next_reading("なかじま", ["なかじま", "なかしま"], ["なかじま"]), "なかしま")

    def test_three_candidates_are_tried_in_turn(self):
        """⛔ G-46：名前だけで「カドタです。」と直されると、AIの報告は使えないことが多い（候補を並べて選ばせても、並びの先を取る）。
        ⇒ 相手に順に確かめていただく（言う読みは3つまで）。2度目の聞き違いは重いお詫び。"""
        from flowctl import Flow, reading_question
        f = Flow([reading_question("N1", "角田", "つのだ", ["つのだ", "かくた", "かどた"])])
        f.on_callee(); f.on_heard("角田です。")
        r = f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})   # 名前だけの言い直し＝直された（AIは「合っている」）
        self.assertEqual((r["say"], r["ack"]), ("改めましてお名前の読みの方は「かくた」様でよろしかったでしょうか？", "shitsurei"))
        f.on_callee(); f.on_heard("いや、角田です。")
        r = f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})
        self.assertEqual((r["say"], r["ack"]), ("改めましてお名前の読みの方は「かどた」様でよろしかったでしょうか？", "moushiwake_heavy"))
        f.on_callee(); f.on_heard("はい、そうです。")
        f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})
        self.assertEqual(f.readings(), {"角田": "かどた"})
        # 3つとも違うと言われたら、名前を使わずにお呼びする（4つ目は聞かない）
        f = Flow([reading_question("N1", "角田", "つのだ", ["つのだ", "かくた", "かどた"])])
        for _ in range(3):
            f.on_callee(); f.on_heard("いや、違います。")
            r = f.apply("confirm_field", {"question_id": "N1", "result": "corrected", "correction": "すみた"})
        self.assertEqual(f.items["N1"].state, "unresolved")
        self.assertIn("お名前を使わずに", r["message"])

    def test_reminder_does_not_ask_again_after_callee_replied(self):
        """⛔ G-46：報告の種類の名前を書き違えて差し戻されたとき、「相手がまだ答えていなければ、もう一度聞いて」としか言わず、
        相手はもう答えていたのに、AIが同じ確かめをもう一度声に出した。"""
        from flowctl import Flow, reading_question
        f = Flow([reading_question("N1", "角田", "つのだ", ["つのだ", "かくた", "かどた"])])
        f.on_callee(); f.on_heard("角田です。")
        r = f.apply("confirm_field", {"question_id": "N1", "result": "回答された値", "correction": "かどだ"})
        self.assertEqual(r["result"], "rejected")
        self.assertIn("もう一度聞かずに", r["message"])
        self.assertNotIn("もう一度聞いてください", r["message"])

    def test_voicing_misheard_correction_uses_candidate(self):
        """⛔ G-46：相手は「カドタ」と直したのに、AIは「かどだ」と報告し、プログラムは「かどだ」様で確かめていた。"""
        from flowctl import Flow, reading_question
        f = Flow([reading_question("N1", "角田", "つのだ", ["つのだ", "かくた", "かどた"])])
        f.on_callee(); f.on_heard("いや、角田です。")
        r = f.apply("confirm_field", {"question_id": "N1", "result": "corrected", "correction": "かどだ"})
        self.assertEqual(r["say"], "改めましてお名前の読みの方は「かどた」様でよろしかったでしょうか？")

    def test_same_reading_reported_as_correction_uses_other_candidate(self):
        """⛔ G-45：相手が「いや、ナカシマです。」と直したのに、AIは「なかじま」（直す前と同じ読み）で訂正を報告した。"""
        from flowctl import Flow, reading_question
        f = Flow([reading_question("N1", "中島", "なかじま", ["なかじま", "なかしま"])])
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="なかじま")
        self.assertEqual(r["say"], "改めましてお名前の読みの方は「なかしま」様でよろしかったでしょうか？")   # まだ言っていない読みで確かめる
        r = self.do(f, "confirm_field", question_id="N1", result="affirmed")
        self.assertEqual(f.readings(), {"中島": "なかしま"})
        f = Flow([reading_question("N1", "中島", "なかじま", ["なかじま", "なかしま"])])
        self.do(f, "confirm_field", question_id="N1", result="corrected", correction="なかじま")
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="なかしま")  # 2つとも違う＝名前を使わない
        self.assertIn("お名前を使わずに", r["message"])
        self.assertEqual(f.readings(), {})

    def test_denial_words_decide_over_ai_affirmed(self):
        """⛔ G-45：相手の「いや、中島です。」を、AIが「合っている」と報告した。認めたか直されたかは、相手の言葉で決める。"""
        from flowctl import Flow, reading_question
        f = Flow([reading_question("N1", "中島", "なかじま", ["なかじま", "なかしま"])])
        f.on_callee()
        f.on_heard("いや、中島です。")
        r = f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})
        self.assertEqual(r["say"], "改めましてお名前の読みの方は「なかしま」様でよろしかったでしょうか？")
        f.on_callee()
        f.on_heard("いえいえ、そうです。")                     # 「いえいえ」は打ち消しではない
        f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})
        self.assertEqual(f.readings(), {"中島": "なかしま"})

    def test_denial_words(self):
        from flowctl import DENIAL
        for said in ("いや、中島です。", "いえ、なかしまです", "違います", "ちがいます、なかしまです", "違う違う"):
            self.assertTrue(DENIAL.match(said), said)
        for said in ("いえいえ、そうです", "違いません", "違いないです", "はい、そうです", "中島です"):
            self.assertFalse(DENIAL.match(said), said)

    def test_name_only_reply_is_a_correction(self):
        """相手が認める言葉を言わずに名前だけを言い直した（「ナカシマです。」）＝直された。「はい、中島です。」は認めた。"""
        from flowctl import Flow, reading_question
        for heard, want in (("中島です。", "なかしま"), ("ナカシマです。", "なかしま"), ("はい、中島です。", "なかじま"), ("", "なかじま")):
            f = Flow([reading_question("N1", "中島", "なかじま", ["なかじま", "なかしま"])])
            f.on_callee()
            f.on_heard(heard)
            r = f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})
            if r["result"] == "confirm_required":
                f.on_callee()
                f.on_heard("はい、そうです。")
                f.apply("confirm_field", {"question_id": "N1", "result": "affirmed"})
            self.assertEqual(f.readings(), {"中島": want}, heard)

    def test_kinds_and_target_call(self):
        """✅ 2026-10-16 ユーザー：法人「（部署、役職）の（相手の名前）様はいらっしゃいますでしょうか？」「お名前の読みの方は…様で
        よろしかったでしょうか？」／個人「（相手の名前）様のお電話でお間違えないでしょうか？」「お名前の方は…様でよろしかったでしょうか？」"""
        from flowctl import Flow, reading_question
        from phrases import target_call
        self.assertEqual(target_call("法人", "「なかじま」", "営業部部長"), "営業部部長の「なかじま」様はいらっしゃいますでしょうか？")
        self.assertEqual(target_call("法人", "「なかじま」"), "「なかじま」様はいらっしゃいますでしょうか？")
        self.assertEqual(target_call("個人", "「なかじま」", "営業部部長"), "「なかじま」様のお電話でお間違えないでしょうか？")
        f = Flow([reading_question("N1", "中島", "なかじま", ["なかじま", "なかしま"], kind="個人")])
        self.assertEqual(f.questions["N1"].text, "お名前の方は「なかじま」様でよろしかったでしょうか？")
        r = self.do(f, "confirm_field", question_id="N1", result="corrected", correction="なかしま")
        self.assertEqual(r["say"], "改めましてお名前の方は「なかしま」様でよろしかったでしょうか？")

    def test_unread_rule(self):
        from flowctl import Question
        from instruction import Business, build
        q = [Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))]
        _, text = build(Business(company="アオバ物流", caller="佐藤", purpose="", questions=q, unread_names=["角田"]), "S4")
        self.assertIn("こちら側の人の名前のうち、「角田」は読み方が決まっていません。この名前は声に出さずに", text)
        self.assertNotIn("お客様側", text)  # お客様側の名前は、まず言ってみる読みで呼んで確かめる（使わない、とは言わない）
        _, text = build(Business(company="アオバ物流", caller="佐藤", purpose="", questions=q), "S4")
        self.assertNotIn("読み方が決まっていません", text)


class NameReadingRuleTest(unittest.TestCase):
    """台本に書いてある読みがなを、プログラムが読む（names.given_reading・G-42）。"""

    def test_given_reading(self):
        from names import given_reading
        self.assertEqual(given_reading("講師の東海林（しょうじ）が担当いたします。", "東海林"), "しょうじ")
        self.assertEqual(given_reading("担当の角田(ツノダ)です。", "角田"), "つのだ")
        self.assertEqual(given_reading("担当の角田です。", "角田"), "")


class AskLadderTest(unittest.TestCase):
    """聞き返しの回数と言い方（DESIGN.md 6-11）。✅ 2026-10-14 ユーザー：必須の項目を聞く回数は、最初の質問のほかに最大3回
    （その場2回＋締めの前の念のための確認1回）。依頼主ごとに変えられる。最後の1回は「申し訳ございません、」（相づちの録音）＋
    「お声が聞き取れませんでしたので、（聞くこと）を、もう一度だけ、お聞かせいただいてもよろしいでしょうか？」。"""

    LAST = "お声が聞き取れませんでしたのでご希望の日時をもう一度だけお聞かせいただいてもよろしいでしょうか？"

    def flow(self, **limits):
        from flowctl import Flow, Limits, Question
        return Flow([Question("Q1", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",), topic="ご希望の日時"),
                     Question("Q2", "ご来店の人数をお聞かせください。", "number", conditions=("答えた",), topic="ご来店の人数")],
                    limits=Limits(**limits))

    @staticmethod
    def do(f, name, **args):
        f.on_callee()
        return f.apply(name, args)

    def test_default_two_in_place_then_last_ask_at_recheck(self):
        from phrases import ASK_BACK
        f = self.flow()
        said = [self.do(f, "report_unclear", question_id="Q1") for _ in range(2)]
        self.assertEqual([(r["say"], r["ack"]) for r in said], [(ASK_BACK, "moushiwake"), ("もう一回よろしいでしょうか？", "moushiwake_heavy")])
        r = self.do(f, "report_unclear", question_id="Q1")
        self.assertEqual((r["result"], r["question_id"], f.items["Q1"].state), ("next", "Q2", "unresolved"))  # その場は2回まで
        self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")
        r = self.do(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((r["result"], r["question_id"], r["say"], r["ack"]), ("recheck", "Q1", self.LAST, "moushiwake_heavy"))  # 最後の1回＝重いお詫び
        self.assertIn(self.LAST, r["message"])
        self.assertEqual(self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の午後2時")["result"], "confirm_required")
        self.assertEqual(self.do(f, "confirm_field", question_id="Q1", result="affirmed")["result"], "end")
        self.assertEqual(f.export()["Q1"], "15日の午後2時")

    def test_last_ask_is_said_once_and_then_dash(self):
        """念のための確認でも聞き取れなければ「-」で締めへ（聞く回数は、最初の質問のほかに3回まで）。"""
        f = self.flow()
        for _ in range(3):
            self.do(f, "report_unclear", question_id="Q1")
        self.do(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")
        self.do(f, "confirm_field", question_id="Q2", result="affirmed")
        r = self.do(f, "report_unclear", question_id="Q1")
        self.assertEqual((r["result"], f.export()["Q1"]), ("end", "-"))
        self.assertEqual(sum(1 for e in f.log if e["reply"]["result"] in ("ask_back", "recheck")), 3)

    def test_client_settings(self):
        """依頼主ごとの設定：念のための確認を0回にしたら、その場の最後の聞き返しが最後の1回の言い方になる。
        その場を3回にしても、「もう一度だけ」は最後の1回（念のための確認）にしか使わない。"""
        f = self.flow(recheck=0)
        self.do(f, "report_unclear", question_id="Q1")
        self.assertEqual(self.do(f, "report_unclear", question_id="Q1")["say"], self.LAST)
        g = self.flow(ask_back=3)
        says = [self.do(g, "report_unclear", question_id="Q1")["say"] for _ in range(3)]
        self.assertFalse(any("もう一度だけ" in s for s in says), says)
        h = self.flow(ask_back=1, recheck=0)  # 1回きりの聞き直しが、答えが質問に合わないとき：言い方も相づちも、最後の1回のもの
        r = self.do(h, "record_answer", question_id="Q1", condition_id="答えた", value="3人")
        self.assertEqual((r["say"], r["ack"]), (self.LAST, "moushiwake_heavy"))

    def heard(self, f, said):
        """相手が話した（会話AIの聞き取りの文字が届いた）。"""
        f.on_callee()
        if said:
            f.on_heard(said)

    def test_misheard_and_restated_read_back_differently(self):
        """値を直されたあと（✅ 2026-10-14 ユーザー）：こちらの聞き違い＝「失礼いたしました。」＋「改めまして、…大丈夫でしょうか？」、
        相手の言い間違い＝「承知しました。」＋「それでは、…よろしいでしょうか？」。どちらかは、プログラムが相手の言葉から決める
        （✅ 2026-10-14 ユーザー決定。⛔ G-36：AIに選ばせると12本中12本「相手が言い直した」）。言葉の文字がまだ届いていなければお詫び。"""
        cases = {"あ、すみません、間違えました。16日の午後2時でお願いします。": "shouchi", "やっぱり16日にしてください。": "shouchi",
                 "16日に変更できますか。": "shouchi", "いや、16日って言ったんですけど。": "shitsurei", "いえ、16日の午後2時です。": "shitsurei",
                 "": "shitsurei"}
        says = {"shitsurei": "改めまして16日の午後2時で大丈夫でしょうか？", "shouchi": "それでは16日の午後2時でよろしいでしょうか？"}
        for said, ack in cases.items():
            f = self.flow()
            self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の午後2時")
            self.heard(f, said)
            r = f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "16日の午後2時"})
            self.assertEqual((r["ack"], r["say"]), (ack, says[ack]), said)
            self.assertEqual((f.items["Q1"].value, f.awaiting_confirm), ("16日の午後2時", "Q1"))

    def test_heard_words_belong_to_the_latest_callee_turn(self):
        """前の相手の言葉に「間違え」があっても、いまの相手の言葉で決める（相手が話すたびに、文字を新しくする）。"""
        f = self.flow()
        self.heard(f, "あ、間違えました、15日です。")
        f.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "15日の午後2時"})
        self.heard(f, "いや、16日って言ったんですけど。")
        self.assertEqual(f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "16日の午後2時"})["ack"],
                         "shitsurei")

    def test_correction_right_after_affirmed_in_same_turn_wins(self):
        """⛔ G-36 の4回目：相手の「あ、すみません、間違えました。16日の…」の「あ」で、AIが「合っている」と報告し、すぐに
        「違う値を言われた」と報告し直した。同じ相手の発話への報告なら、あとの報告を採る（先の確かめを取り消して、流れの位置も戻す）。"""
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の午後2時")
        self.heard(f, "あ")
        self.assertEqual(f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})["result"], "next")
        f.on_heard("、すみません、間違えました。16日の午後2時でお願いします。")
        r = f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "16日の午後2時"})
        self.assertEqual((r["result"], r["ack"], r["say"]), ("confirm_required", "shouchi", "それでは16日の午後2時でよろしいでしょうか？"))
        self.assertEqual((f.items["Q1"].state, f.items["Q1"].value, f.awaiting_confirm), ("checking", "16日の午後2時", "Q1"))
        self.heard(f, "はい、そうです。")
        r = f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})
        self.assertEqual((r["result"], r["question_id"]), ("next", "Q2"))
        self.assertEqual(f.export()["Q1"], "16日の午後2時")

    def test_correction_after_callee_spoke_again_is_not_reopened(self):
        """相手がもう次の言葉を話したあとの「違う値を言われた」は、済んだ確かめを取り消さない（差し戻す）。"""
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の午後2時")
        self.do(f, "confirm_field", question_id="Q1", result="affirmed")
        self.heard(f, "3人です。")
        r = f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "16日の午後2時"})
        self.assertEqual((r["result"], f.items["Q1"].state, f.items["Q1"].value), ("rejected", "confirmed", "15日の午後2時"))

    def test_reopen_after_last_question_goes_back_from_closing(self):
        """最後の質問の確かめが済んで締めに進んだあとでも、同じ相手の発話への訂正なら、締めから戻して言い直す。"""
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("答えた",), topic="ご希望の日時")])
        f.on_callee()
        f.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "15日の午後2時"})
        self.heard(f, "あ")
        self.assertEqual(f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})["result"], "end")
        f.on_heard("、いや、16日って言ったんですけど。")
        r = f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "16日の午後2時"})
        self.assertEqual((r["result"], r["ack"], f.phase), ("confirm_required", "shitsurei", "asking"))
        self.heard(f, "はい。")
        self.assertEqual(f.apply("confirm_field", {"question_id": "Q1", "result": "affirmed"})["result"], "end")
        self.assertEqual(f.export()["Q1"], "16日の午後2時")

    def test_unreported_restatement_keeps_its_kind(self):
        """AIが答えを報告せずに言い直し、相手が自分の言い間違いを直した：差し戻したあとの答えの報告でも、言い直しを受けた言い方にする。"""
        f = self.flow()
        self.heard(f, "あ、すみません、間違えました。16日です。")
        self.assertEqual(f.apply("confirm_field", {"question_id": "Q1", "result": "corrected", "correction": "16日の午後2時"})["result"],
                         "rejected")
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "16日の午後2時"})
        self.assertEqual((r["ack"], r["say"]), ("shouchi", "それでは16日の午後2時でよろしいでしょうか？"))

    def test_instruction_leaves_the_fit_to_the_program(self):
        """答えが質問に合うかは、プログラムの1か所で決める（✅ 2026-10-14 ユーザー承認）。AIには、聞いたとおりの値で報告させる。"""
        from flowctl import CONFIRM_LABEL, Question
        from instruction import Business, build
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=[Question("Q1", "日時は。", "datetime", conditions=("答えた",))])
        text = build(b, "S2")[1]
        self.assertIn("合うかどうかはプログラムが決めて", text)
        self.assertIn("聞き取れない（何と言ったか分からない）ときだけ report_unclear", text)
        self.assertIn(f"こちらの聞き違いでも、相手の言い間違いでも）「{CONFIRM_LABEL['corrected']}」", text)  # 見分けはプログラム
        self.assertIn("直していない部分も足します", text)  # ⛔ G-36 の3回目：「16日」だけが報告され、「午後2時」が落ちた
        self.assertIn("その言い方から話し始めます", text)    # ⛔ G-36 の3回目：AIが自分で「失礼いたしました。」を足した


class SpokenTextTest(unittest.TestCase):
    """お客様に聞こえる決まった言い方・録音の文には、言葉の途中に読点を入れない（✅ 2026-10-15 ユーザー：「句読点のような間が入ると、
    少し違和感がある。すらっと読めると自然（人っぽい）」）。録音の相づち・つなぎの言葉の終わりの読点（続きがある調子）だけは許す。"""

    def test_no_comma_inside(self):
        import phrases
        from g22_opening import BUSINESS
        texts = [phrases.ASK_BACK, phrases.BUSY_ASK, phrases.CLOSE_DECLINED, phrases.AMPM_ASK, phrases.WAIT, phrases.CLOSING,
                 *phrases.SILENCE_PROMPT, *[q.text for q in BUSINESS.questions], *BUSINESS.opening_parts()]
        texts += [phrases.ask_again(n, r, "ご希望の日時", last) for n in (1, 2, 3) for r in ("unclear", "form") for last in (False, True)]
        texts += [phrases.confirm(v, sit, n) for v in ("15日の午後2時", "いつでも大丈夫") for sit in phrases.CONFIRM for n in range(2)]
        texts += [t for forms in phrases.LEAD_IN.values() for t in forms] + list(phrases.RECORDED.values())
        for t in texts:
            self.assertNotIn("、", t.rstrip("、"), t)


class AmPmTest(unittest.TestCase):
    """時刻の午前・午後（✅ 2026-10-15 ユーザー：AIからプログラムへ移す。依頼主ごとの営業時間で決め、営業時間の外になる側は聞かない。
    会社は 9:00〜19:00 が初めの値）。"""

    def test_fill_from_business_hours(self):
        from flowctl import fill_ampm
        for v, hours, want in [("15日の1時", (9, 19), ("15日の午後1時", False)), ("15日の10時", (9, 19), ("15日の午前10時", False)),
                               ("15日の10時", (9, 23), ("15日の10時", True)),       # 午前も午後も営業時間 ⇒ 聞く
                               ("15日の8時", (9, 19), ("15日の8時", True)),         # どちらも営業時間の外 ⇒ 聞く
                               ("1時", (18, 3), ("午前1時", False)), ("8時", (18, 3), ("午後8時", False)),  # 日をまたぐ深夜営業
                               ("15日の12時", (9, 19), ("15日の12時", False)),      # 昼の12時（「午後12時」とは言わない）
                               ("2時から4時", (9, 19), ("午後2時から4時", False)),
                               ("15日の14時", (9, 19), ("15日の14時", False)), ("夕方5時", (9, 19), ("夕方5時", False)),
                               ("15日の午前11時", (9, 19), ("15日の午前11時", False)), ("いつでも大丈夫", (9, 19), ("いつでも大丈夫", False))]:
            self.assertEqual(fill_ampm(v, hours), want, (v, hours))

    def flow(self, hours=(9, 19)):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご希望の日時を、お聞かせいただけますでしょうか。", "datetime", conditions=("答えた",), topic="ご希望の日時")],
                    business_hours=hours)

    @staticmethod
    def do(f, name, **args):
        f.on_callee()
        return f.apply(name, args)

    def test_filled_value_is_read_back_without_asking(self):
        f = self.flow()
        r = self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の1時")
        self.assertEqual((r["result"], f.items["Q1"].value), ("confirm_required", "15日の午後1時"))
        self.assertIn("15日の午後1時", r["say"])

    def test_ask_once_then_merge_answer(self):
        from phrases import AMPM_ASK
        f = self.flow((9, 23))
        r = self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の10時")
        self.assertEqual((r["result"], r["say"], r["ack"]), ("ask_back", AMPM_ASK, "hai"))
        self.assertEqual((f.items["Q1"].value, f.items["Q1"].ask_backs), (None, 0))   # 聞き返しの回数には数えない
        r = self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="午後です")
        self.assertEqual((r["result"], f.items["Q1"].value), ("confirm_required", "15日の午後10時"))
        self.do(f, "confirm_field", question_id="Q1", result="affirmed")
        self.assertEqual(f.export()["Q1"], "15日の午後10時")

    def test_asked_only_once(self):
        f = self.flow((9, 23))
        self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の10時")
        r = self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="16日の10時")  # 答えがまた決まらない
        self.assertEqual((r["result"], f.items["Q1"].value), ("confirm_required", "16日の10時"))   # 二度は聞かない

    def test_correction_is_filled_too(self):
        f = self.flow()
        self.do(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日の午後2時")
        r = self.do(f, "confirm_field", question_id="Q1", result="corrected", correction="16日の3時")
        self.assertEqual(f.items["Q1"].value, "16日の午後3時")
        self.assertIn("16日の午後3時", r["say"])

    def test_ai_does_not_ask_by_itself(self):
        from g22_opening import BUSINESS
        from instruction import build
        text = build(BUSINESS, "S2")[1]
        self.assertIn("自分で聞いたり、自分で決めたりしてはいけません", text)
        self.assertNotIn("と聞きます。自分で決めてはいけません", text)


class PlayRecordingsTest(unittest.TestCase):
    """次の質問と締めを、電話接続側が録音で流し、AIには話させない形（DESIGN.md 6-13。✅ 2026-10-15 ユーザー：よく使う言葉は契約者の単位、
    トークスクリプトは依頼主の単位で録音する。G-37）。言い直し（値が変わる）は、これまでどおりAIが話す。"""

    def flow(self):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか。", "yes_no", conditions=("はい", "いいえ")),
                     Question("Q2", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",), topic="ご希望の日時")],
                    play_recordings=True)

    def test_next_question_and_closing_are_played_silently(self):
        from phrases import CLOSING
        f = self.flow()
        f.on_callee()
        out, silent = f.handle_call(fc("record_answer", 質問="Q1", 答えの種類="はい"))
        r = f.log[-1]["reply"]
        self.assertTrue(silent)
        # つなぎの言葉と質問を1本の録音で流す（✅ 2026-10-15 ユーザー：1本のほうが、しいて言えば自然）
        self.assertEqual(r["play"], [{"kind": "question", "id": "Q2", "text": "それではご希望の日時をお聞かせいただけますでしょうか？",
                                      "lead": "それでは"}])
        self.assertEqual(r["ack"], "shouchi")
        self.assertIn("録音で流しました", out["次にすること"])
        self.assertNotIn("play", out)                                          # 中の項目はAIに見せない
        f.on_callee()
        out, silent = f.handle_call(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="15日の午後2時"))
        self.assertFalse(silent)                                               # 言い直しは、AIが話す
        f.on_callee()
        out, silent = f.handle_call(fc("confirm_field", 質問="Q2", 返事の種類="合っている"))
        self.assertTrue(silent)
        self.assertEqual((f.log[-1]["reply"]["result"], f.log[-1]["reply"]["play"]),
                         ("end", [{"kind": "closing", "id": "closing", "text": CLOSING}]))

    def test_lead_is_fixed_by_question_order(self):
        """つなぎの言葉は質問の順番で決まる（依頼主ごとの録音は、質問1つにつき1本のまま）。最初の質問は名乗りと一緒なので付けない。"""
        from flowctl import Question, spoken_question
        qs = [Question(f"Q{i}", f"質問{i}ですか？", "free_text") for i in range(1, 6)]
        self.assertEqual([spoken_question(qs, q.id)[1] for q in qs], ["", "それでは", "続きまして", "では", "それでは"])
        self.assertEqual(spoken_question(qs, "Q3")[0], "続きまして質問3ですか？")

    def test_declined_close_is_played_too(self):
        """断られたときの締めも、毎回同じ文なので録音で流す（6-13。flowctl.Flow._close）。⛔ 前は「これまでどおり」AIが言っていた
        （「お忙しいところ」はAIの声で崩れやすい）。"""
        from phrases import CLOSE_DECLINED, RECORDED
        for reason in ("通話を断られた", "相手が急いでいる"):
            f = self.flow()
            f.on_callee()
            out, silent = f.handle_call(fc("request_end", 理由=reason))
            self.assertTrue(silent, reason)
            self.assertEqual(f.log[-1]["reply"]["play"], [{"kind": "closing", "id": "closing_declined", "text": CLOSE_DECLINED}])
        self.assertEqual(RECORDED["closing_declined"], CLOSE_DECLINED)

    def test_close_after_callee_question_is_said(self):
        # 相手が最後に質問をしていたら、AIが短く答えてから、決まった締めの言い方のまま言う（録音を流すと、相手の質問にかぶせる）
        from phrases import CLOSING
        f = self.flow()
        f.on_callee(); f.handle_call(fc("record_answer", 質問="Q1", 答えの種類="はい"))
        f.on_callee(); f.handle_call(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="15日の午後2時"))
        f.on_callee(); f.on_heard("はい。当日はどなたが担当されるんですか？")
        out, silent = f.handle_call(fc("confirm_field", 質問="Q2", 返事の種類="合っている"))
        r = f.log[-1]["reply"]
        self.assertFalse(silent)
        self.assertEqual((r["result"], r["say"], r.get("play")), ("end", CLOSING, None))
        self.assertIn(CLOSING, out["次にすること"])

    def test_rule_in_instruction_only_when_playing(self):
        from g22_opening import BUSINESS
        from instruction import build
        self.assertIn("録音で流しました", build(BUSINESS, "S2", opening_mode="recorded", play_recordings=True)[1])
        self.assertNotIn("録音で流しました", build(BUSINESS, "S2", opening_mode="recorded")[1])


class NeverStuckTest(unittest.TestCase):
    """どんな報告がどんな順で届いても、流れの制御が止まらないこと（✅ 2026-10-14 ユーザー：「プログラムで判断できずに止まることがない
    ようにしてください」）：(1) 誤りで止まらない (2) 締めでない限り、どの返事にも次にすることが書いてある (3) 聞き返し・念のための確認は
    決まった回数で尽き、同じ報告をくり返されても、通話は必ず締めに着く。"""

    VALUES = ("15日の午後2時", "3人", "はい", "いつでも", "答えなかった", "違います", "", None, "16 日の午後 2 時", "x" * 3, "やまさき", "ヤマザキ様")

    def flow(self):
        from flowctl import Flow, Question, reading_question
        # 名前の読みの確かめ（プログラムが足す質問。2026-10-16）も混ぜる：これを足しても止まらないこと
        return Flow([Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ"), topic="ご予約の有無"),
                     reading_question("N1", "山崎", "やまざき"),
                     Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",), topic="ご希望の日時"),
                     Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("答えた",), required=False)])

    def random_report(self, rnd, f):
        """ふつうの報告を多めに、誤った報告（いまの質問でない・選択肢に無い・項目名の書き違え・無い操作）も混ぜる。"""
        from flowctl import CONFIRM_LABEL
        qid = f.current if (f.current and rnd.random() < 0.85) else rnd.choice(("Q1", "N1", "Q2", "Q3", "Q9"))
        x = rnd.random()
        if f.awaiting_confirm and x < 0.6:
            name, args = "confirm_field", {"question_id": qid, "result": rnd.choice(tuple(CONFIRM_LABEL) + ("bad",)),
                                           "correction": rnd.choice(self.VALUES)}
        elif x < 0.65:
            cond = rnd.choice(("はい", "いいえ")) if qid == "Q1" else "答えた"
            if rnd.random() < 0.25:
                cond = rnd.choice(("unknown", "refused", "ない"))
            name, args = "record_answer", {"question_id": qid, "condition_id": cond, "value": rnd.choice(self.VALUES)}
        elif x < 0.85:
            name, args = "report_unclear", {"question_id": qid}
        elif x < 0.92:
            name, args = "request_end", {"reason": rnd.choice(("done", "done", "callee_hurry", "callee_refused_call", "bad"))}
        else:
            name, args = "no_such", {}
        if rnd.random() < 0.05:
            args["typo"] = "x"
        return name, {k: v for k, v in args.items() if v is not None}

    def test_random_reports_never_leave_ai_without_next_step(self):
        import collections
        import random

        from flowctl import RESULT_LABEL
        rnd = random.Random(20261014)
        seen = collections.Counter()
        for _ in range(400):
            f = self.flow()
            for _ in range(60):
                if rnd.random() < 0.8:
                    f.on_callee()
                name, args = self.random_report(rnd, f)
                r = f.apply(name, args)  # (1) 誤りで止まらない
                seen[r["result"]] += 1
                self.assertIn(r["result"], RESULT_LABEL, r)
                if f.phase != "closing":
                    self.assertTrue(r.get("message"), r)  # (2) 次にすることが書いてある
                if r["result"] in ("ask_back", "confirm_required", "busy_ask", "recheck"):
                    self.assertTrue(r.get("say"), r)       # 言い方が決まっている
                if f.phase == "closing":
                    break
        for result in ("rejected", "ask_back", "confirm_required", "next", "recheck", "end"):  # どの場面も十分に通った
            self.assertGreater(seen[result], 30, (result, seen))

    def test_same_bad_report_repeated_always_reaches_closing(self):
        """AIや相手が同じことをくり返しても、回数の上限で必ず先へ進み、締めに着く（無限に聞き返さない）。"""
        for value in ("3人", "はい", "答えなかった"):
            f = self.flow()
            f.on_callee()
            f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})
            for step in range(50):
                f.on_callee()
                q = f.current
                if q is None:
                    break
                if f.awaiting_confirm:
                    f.apply("confirm_field", {"question_id": q, "result": "no_answer"})
                elif q == "Q2":
                    f.apply("record_answer", {"question_id": q, "condition_id": "答えた", "value": value})
                else:
                    f.apply("report_unclear", {"question_id": q})
            self.assertEqual(f.phase, "closing", value)
            self.assertLess(step, 20, value)  # 決まった回数で尽きる
        f = self.flow()
        for step in range(50):
            f.on_callee()
            if f.current is None:
                break
            f.apply("report_unclear", {"question_id": f.current})  # 何を聞いても聞き取れない相手
        self.assertEqual(f.phase, "closing")


class AckTimingTest(unittest.TestCase):
    """G-34 の数え方（報告の時刻に流す相づち）と、通話をつないで聞く声（stitch_gap.stitch_call）。作った記録で確かめる。"""

    @staticmethod
    def turn(first_audio, tool_at, ack="hai", said="ありがとうございます。15日の午後2時でよろしいでしょうか？"):
        return {"callee": {"wav": "Puck_datetime_pm.wav"}, "first_audio_after_s": first_audio, "intended": said,
                "tool_events": [{"at_s": tool_at, "name": "record_answer", "reply": "confirm_required", "ack": ack}]}

    def test_ack_played_only_before_ai_voice(self):
        from stitch_gap import callee_speech, played_ack
        tail = callee_speech("Puck_datetime_pm.wav")[2]
        a = played_ack(self.turn(1.0, 0.5))
        self.assertEqual(a["ack"], "hai")
        self.assertAlmostEqual(a["at_s"], 0.5 + tail, places=3)  # 声の終わりから数える
        self.assertIsNone(played_ack(self.turn(0.4, 0.5)))       # AIがもう話し始めていた：重ねない
        self.assertIsNone(played_ack(self.turn(1.0, 0.5, ack=None)))  # 差し戻し：流さない

    def test_measures_hold_ai_until_ack_ends(self):
        from g34_ack import JOIN_S, ack_seconds, turn_measures
        secs = ack_seconds("S2", "hai")
        self.assertIsNotNone(secs)
        call = {"turns": [self.turn(0.6, 0.3), {"callee": {"silence_s": 6.0}, "intended": "", "tool_events": []},
                          self.turn(1.2, 0.5, said="はい、ありがとうございます。")]}
        a, silent, b = turn_measures(call, "S2")
        self.assertEqual(a["first_heard_s"], a["ack_at_s"])
        self.assertAlmostEqual(a["ai_held_s"], max(0.0, a["ack_at_s"] + secs + JOIN_S - a["ai_at_s"]), places=2)
        self.assertGreaterEqual(a["ack_to_ai_s"], JOIN_S - 0.001)
        self.assertFalse(silent["spoke_in_silence"])
        self.assertTrue(b["ai_starts_with_hai"])  # AIが自分でも「はい」から話した（相づちと重なる）

    def test_followup_when_ai_waited_or_cut_in(self):
        from convo import followup_reason
        from live_util import TurnResult
        waited = TurnResult()                                   # 声も報告も無いターン＝相手の「えーとですね、」のあと黙って待った
        self.assertEqual(followup_reason(waited), "waited")
        cut = TurnResult(first_audio_after_s=0.01)              # 相手の言葉の途中で話し出していた
        self.assertEqual(followup_reason(cut), "cut_in")
        reported = TurnResult(tool_calls=[{"name": "record_answer"}])  # 報告だけ（声はまだ）：待たない（報告の返事のあとの声まで受け取る）
        self.assertIsNone(followup_reason(reported))
        self.assertIsNone(followup_reason(TurnResult(first_audio_after_s=1.0)))  # ふつうの返事
        self.assertIsNone(followup_reason(TurnResult(timed_out=True)))          # 時間切れ（黙っている間の確かめなど）

    def test_stitch_call_keeps_waited_turn(self):
        import numpy as np

        from common import write_wav
        from convo import AUDIO_DIR
        from stitch_gap import stitch_call
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        ai = AUDIO_DIR / "_test_stitch_ai2.wav"
        write_wav(ai, (np.ones(24000, dtype=np.int16) * 1000).tobytes(), 24000)
        try:
            waited = {"callee": {"wav": "Puck_filler_eto.wav"}, "first_audio_after_s": None, "tool_events": [], "intended": ""}
            silent_follow = {"callee": {"followup_of": {"wav": "Puck_filler_eto.wav"}, "reason": "waited"}, "first_audio_after_s": None,
                             "tool_events": [], "intended": ""}
            answer = {**self.turn(1.0, 0.5), "audio_file": ai.name}
            with tempfile.TemporaryDirectory() as d:
                plan = stitch_call({"turns": [waited, silent_follow, {"callee": {"silence_s": 1.0}, "tool_events": []}, answer]}, "S2",
                                   Path(d) / "call.wav")
                self.assertEqual([p["heard_first"] for p in plan], ["hai"])  # 黙って待ったターンには、AIの声も相づちも置かない
                spoke = {**silent_follow, "audio_file": ai.name}
                self.assertIsNone(stitch_call({"turns": [waited, spoke]}, "S2", Path(d) / "x.wav"))  # 待つ間に話した：時刻が分からない
        finally:
            ai.unlink()

    def test_stitch_call_orders_callee_ack_ai(self):
        import numpy as np

        from common import write_wav
        from convo import AUDIO_DIR
        from stitch_gap import stitch_call
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        ai = AUDIO_DIR / "_test_stitch_ai.wav"
        write_wav(ai, (np.ones(24000, dtype=np.int16) * 1000).tobytes(), 24000)
        try:
            t = {**self.turn(1.0, 0.5), "audio_file": ai.name}
            with tempfile.TemporaryDirectory() as d:
                plan = stitch_call({"turns": [t]}, "S2", Path(d) / "call.wav")
                self.assertEqual(plan[0]["heard_first"], "hai")
                self.assertTrue((Path(d) / "call.wav").exists())
                cut = {**self.turn(0.01, 0.5), "audio_file": ai.name}  # 割り込み：本当の時刻が分からないので作らない
                self.assertIsNone(stitch_call({"turns": [cut]}, "S2", Path(d) / "cut.wav"))
        finally:
            ai.unlink()


class RetryTest(unittest.TestCase):
    def test_retry_only_when_unavailable(self):
        from common import call_with_retry

        class Err(Exception):
            def __init__(self, code, status):
                super().__init__(status)
                self.code, self.status = code, status
        calls = []

        def busy_then_ok():
            calls.append(1)
            if len(calls) < 3:
                raise Err(503, "UNAVAILABLE")
            return "ok"
        self.assertEqual(call_with_retry(busy_then_ok, waits=(0, 0, 0)), ("ok", 2))  # 混んでいるときは、やり直す
        calls.clear()

        def quota():
            calls.append(1)
            raise Err(429, "RESOURCE_EXHAUSTED")
        with self.assertRaises(Err):
            call_with_retry(quota, waits=(0, 0, 0))
        self.assertEqual(len(calls), 1)  # ⛔ 上限（429）は、やり直さない
        calls.clear()

        def always_busy():
            calls.append(1)
            raise Err(503, "UNAVAILABLE")
        with self.assertRaises(Err):
            call_with_retry(always_busy, waits=(0, 0))
        self.assertEqual(len(calls), 3)  # やり直しを使い切ったら、誤りをそのまま上げる


class WordingTest(unittest.TestCase):
    """AIへの指示と録音の読み方に、ユーザーが外すよう言った言葉を入れない。"""

    BANNED = ("明るく", "はきはき", "ハキハキ")  # ✅ 2026-10-12 ユーザー：「営業には不向きな強引そうな詐欺師のような怪しいニュアンス」

    def test_instruction_and_reading_manner_have_no_banned_words(self):
        from instruction import system_rules
        from make_ack_clips import HOW, DEFAULT_HOW
        texts = {"AIへの指示": system_rules(False) + system_rules(True), "録音の既定の読み方": DEFAULT_HOW,
                 **{f"録音の読み方 {k}": v for k, v in HOW.items()}}
        for name, text in texts.items():
            for w in self.BANNED:
                self.assertNotIn(w, text, name)


class RecordingCheckTest(unittest.TestCase):
    """録音の確かめ（make_ack_clips）：声の途中で切れた録音・文の途中の長い無音を落とす。"""

    RATE = 24000

    def tone(self, *parts):
        """(秒, 声か) の並びから、作った音を作る。"""
        import numpy as np
        out = []
        for sec, on in parts:
            t = np.arange(int(self.RATE * sec)) / self.RATE
            out.append(np.sin(2 * np.pi * 160 * t) * (0.3 if on else 0.0))
        return (np.concatenate(out) * 32767).astype("<i2").tobytes()

    def test_cut_in_the_middle_of_the_voice_is_rejected(self):
        """⛔ 2026-10-15：S4 の「承知しました、」の候補3は声の途中で終わっていたのに、耳の確かめは通した（ユーザーが聞いて気づいた）。"""
        from make_ack_clips import ends_in_silence
        self.assertFalse(ends_in_silence(self.tone((0.2, False), (0.6, True)), self.RATE))
        self.assertFalse(ends_in_silence(self.tone((0.2, False), (0.6, True), (0.05, False)), self.RATE))
        self.assertTrue(ends_in_silence(self.tone((0.2, False), (0.6, True), (0.25, False)), self.RATE))

    def test_long_pause_only_at_sentence_end(self):
        from make_ack_clips import long_pauses_ok
        one_sentence = self.tone((0.1, False), (0.8, True), (0.35, False), (0.8, True), (0.2, False))
        self.assertFalse(long_pauses_ok("お忙しいところお時間をいただきましてありがとうございました。", one_sentence, self.RATE))
        self.assertTrue(long_pauses_ok("ありがとうございました。それでは失礼いたします。", one_sentence, self.RATE))
        lead = self.tone((0.1, False), (0.8, True), (0.30, False), (0.8, True), (0.2, False))  # つなぎの言葉のあとの間（上限 0.32秒の内側）
        self.assertTrue(long_pauses_ok("それではご希望の日時をお聞かせいただけますでしょうか？", lead, self.RATE))  # 頭のつなぎの言葉のあと

    def test_pause_after_lead_word_has_a_limit(self):
        """✅ 2026-10-16 ユーザー：「それではの後も少しだけ間が長い」（0.38秒）。「続きまして」のあとの 0.30秒は指摘なし。"""
        from make_ack_clips import long_pauses_ok
        text = "それではご希望の日時をお聞かせいただけますでしょうか？"
        long_lead = self.tone((0.1, False), (0.6, True), (0.38, False), (1.5, True), (0.2, False))
        ok_lead = self.tone((0.1, False), (0.6, True), (0.30, False), (1.5, True), (0.2, False))
        self.assertFalse(long_pauses_ok(text, long_lead, self.RATE))
        self.assertTrue(long_pauses_ok(text, ok_lead, self.RATE))

    def test_cut_after_context_keeps_only_the_second_sentence(self):
        """前置き「かしこまりました。」のあとの文の切れ目で切り、後ろだけを残す（声の頭の前に少し無音を残す）。切れ目が無ければ使わない。"""
        from make_ack_clips import CUT_KEEP_S, cut_after_context
        from barge import voice_runs, voiced
        pcm = self.tone((0.1, False), (0.7, True), (0.4, False), (1.5, True), (0.3, False))
        kept = cut_after_context(pcm, self.RATE)
        runs = voice_runs(voiced(kept, self.RATE), 0.05)
        self.assertEqual(len(runs), 1)                                    # 前置きの文は残らない
        self.assertAlmostEqual(runs[0][0], CUT_KEEP_S, delta=0.02)        # 声の頭は切らない
        self.assertAlmostEqual(runs[0][1] - runs[0][0], 1.5, delta=0.05)  # 後ろの文はそのまま
        self.assertIsNone(cut_after_context(self.tone((0.1, False), (2.0, True), (0.3, False)), self.RATE))

    def test_short_comma_like_pause_is_rejected(self):
        """⛔ 2026-10-15：締めの候補は、文の途中の 0.12〜0.24秒の無音で「、」が入った言い方に聞こえた（ユーザー）。0.08秒は聞こえなかった。"""
        from make_ack_clips import long_pauses_ok
        text = "お忙しいところお時間をいただきましてありがとうございました。"
        with_comma = self.tone((0.1, False), (0.8, True), (0.16, False), (0.8, True), (0.2, False))
        smooth = self.tone((0.1, False), (0.8, True), (0.08, False), (0.8, True), (0.2, False))
        self.assertFalse(long_pauses_ok(text, with_comma, self.RATE))
        self.assertTrue(long_pauses_ok(text, smooth, self.RATE))


class JudgeSetupTest(unittest.TestCase):
    """Google 以外の判定（meaning_judge.OTHER_JUDGES）と、ユーザーが貼る文字（docs/JUDGE_SETUP.md）。つながずに確かめる。"""

    DOC = Path(__file__).resolve().parent.parent / "docs" / "JUDGE_SETUP.md"

    def blocks(self) -> list[str]:
        lines, inside = [], False
        for line in self.DOC.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                inside = not inside
            elif inside and line.strip():
                lines.append(line.strip())
        return lines

    def q(self):
        from g53_meaning import g48_questions
        return next(q for q in g48_questions() if q.id == "G17")

    def test_doc_has_every_host_and_key_name(self):
        # 枠に、判定の接続先・キーの名前と、いま使えている接続先（貼り替えで消さない）が、すべてある
        from meaning_judge import OTHER_JUDGES
        lines = self.blocks()
        for j in OTHER_JUDGES.values():
            self.assertIn(j["host"], lines)
            self.assertTrue(any(l.startswith(f"{j['key']}=") for l in lines), j["key"])
        for keep in ("acp-api.amivoice.com", "stt-rt.soniox.com", "api.soniox.com", "*.googleapis.com", "github.com", "*.githubusercontent.com"):
            self.assertIn(keep, lines)
        # この作業の場所そのものが読む名前にしない
        self.assertFalse(any(l.startswith("ANTHROPIC_API_KEY=") for l in lines))
        self.assertNotIn("ANTHROPIC_API_KEY", {j["key"] for j in OTHER_JUDGES.values()})

    def test_requests_carry_the_same_prompt_and_choices(self):
        # 会社ごとに違うのは送り方だけ：同じ問い合わせの文と、同じ選べるものの名前（Gemini の _config と同じ並び）。
        # Jev は決まり・場面・選べるものを別々に受け取る：同じ決まりの文・同じ場面・同じ名前と説明（同じ並び）
        from meaning_judge import OTHER_JUDGES, RULES, options, other_request, prompt, scene
        q, asked, text = self.q(), "念のためご連絡はメールでよろしいでしょうか？", "メールのほうが助かりますが…"
        names = [n for n, _, _ in options(q, asked)]
        for name in OTHER_JUDGES:
            body = other_request(name, q, text, asked)
            sent = json.dumps(body, ensure_ascii=False)
            if OTHER_JUDGES[name]["maker"] == "typesafe":  # 問いは短く・選択肢は同じ名前で同じ並び（定義は意味で読む書き方）・同じ場面
                ask = body["questions"]["choice"]
                self.assertEqual((list(ask["criteria"]), body["state"]), (names, scene(q, text, asked)))
                self.assertLess(len(ask["instructions"]), 120)
                continue
            self.assertIn(json.dumps(prompt(q, text, asked), ensure_ascii=False), sent)
            self.assertIn(json.dumps(names, ensure_ascii=False), sent)
            # Claude Haiku 5.5 は温度などを送ると断る（2026-10-18 に400）
            self.assertFalse({"temperature", "top_p", "top_k"} & set(body), name)

    def test_replies_are_read_the_same_way(self):
        # 返事の形が違っても、read で同じ中の名前になる・読めない返事は None
        from meaning_judge import options, other_text, read
        q, asked = self.q(), "念のためご連絡はメールでよろしいでしょうか？"
        name, cond = options(q, asked)[0][0], options(q, asked)[0][2]
        claude = {"content": [{"type": "tool_use", "name": "answer", "input": {"choice": name}}]}
        gpt = {"output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"choice": name})}]}]}
        self.assertEqual(read(q, other_text("Claude Haiku 5.5", claude), asked), cond)
        self.assertEqual(read(q, other_text("GPT-6 Luna", gpt), asked), cond)
        self.assertIsNone(read(q, other_text("Claude Haiku 5.5", {"content": [{"type": "text", "text": "…"}]}), asked))
        self.assertIsNone(read(q, other_text("GPT-6 Luna", {"output": []}), asked))
        jev = {"answers": {"choice": {"type": "choice", "choice": name, "confidence": 0.99}}}
        self.assertEqual(read(q, other_text("Jev 1.14", jev), asked), cond)
        self.assertIsNone(read(q, other_text("Jev 1.14", {"error": {"code": 402}}), asked))
        # Jev の確からしさは答えに残す（線は JudgePanel が当てる）・言い切っているかは測って合った線（0.2）で読む（⛔ 半分で切っていた）
        got = read(q, other_text("Jev 1.14", jev | {"answers": jev["answers"] | {"firm": {"type": "noul", "noul": 0.3}}}), asked)
        self.assertEqual((got.sure, got.firm), (0.99, True))
        self.assertFalse(read(q, other_text("Jev 1.14", jev | {"answers": jev["answers"] | {"firm": {"noul": 0.1}}}), asked).firm)
        self.assertIsNone(read(q, other_text("Claude Haiku 5.5", claude), asked).sure)  # 確からしさを返さない判定は None

    def test_call_tests_choose_judges_in_one_place(self):
        # 判定の選び方は meaning_judge の1か所（add_judge_args・make_judges）。2つ目の判定の既定は JUDGE2（✅ 2026-10-18 ユーザー：当面は Sonnet だけ）
        import argparse
        from meaning_judge import JUDGE2, add_judge_args
        here = Path(__file__).resolve().parent
        for f in ("g14_listen.py", "g34_ack.py", "g50_replay.py"):
            src = (here / f).read_text(encoding="utf-8")
            self.assertIn("add_judge_args(p)", src, f)
            self.assertNotIn('"--judge"', src, f)
        p = argparse.ArgumentParser()
        add_judge_args(p)
        a = p.parse_args(["--judge", "--judge2"])
        self.assertEqual((a.judge, a.judge2), ("3.5 Flash Lite", JUDGE2))
        self.assertEqual(JUDGE2, "Claude Sonnet 5.5（考える）")

    def test_decisive_answers_go_first_and_survive_rejudge(self):
        # 1日の上限が少ないとき、いまの判定が言い直しへの返事を認めたと読み違えた答えを、先に判定させる（判定し直しで読みを捨てても印は残る）
        from g53_meaning import BASE, first, misread_as_affirmed
        bad = {"set": "D", "qid": "G17", "text": "メールのほうが助かりますが…", "truth": "決められない", "asked_choice": "メール", BASE: "メール"}
        ok = {"set": "D", "qid": "G17", "text": "はい、メールで。", "truth": "メール", "asked_choice": "メール", BASE: "メール"}
        self.assertTrue(misread_as_affirmed(bad, BASE) and first(bad))
        self.assertFalse(first(ok))
        stripped = {k: v for k, v in bad.items() if k != BASE} | {"前の読み違え": True}
        self.assertTrue(first(stripped))


class JudgePanelTest(unittest.TestCase):
    """通話の中の意味の判定の受け持ち（meaning_judge.JudgePanel）：会社の違う2つを受け持ちに置き、止まった判定の代わりに Jev が入る。
    ✅ 2026-10-19 ユーザー：「同じ会社のモデルだけだと、通信の問題が影響するでしょ」「どれかがダウンしていたら、Jevも使ったらいいんじゃない」"""

    LITE, SONNET, JEV = "3.5 Flash Lite", "Claude Sonnet 5.5（考える）", "Jev 1.14"

    def panel(self, fail=None, sure=0.99, standin=True, took=None):
        """fail：判定の名前 ⇒ 投げる失敗（呼ばれた順は calls に残る）。took：判定の名前 ⇒ かかる秒。"""
        import asyncio

        from meaning_judge import JudgePanel, Reading
        calls, now = [], [0.0]
        fail, took = fail or {}, took or {}

        def judge(name, answer):
            async def run(q, text, asked=None, before=None):
                calls.append(name)
                if name in took:
                    await asyncio.sleep(took[name])
                if name in fail:
                    raise fail[name]
                return Reading(answer, sure=sure if name == self.JEV else None)
            return run
        panel = JudgePanel({"judge": (self.LITE, judge(self.LITE, "はい")), "judge2": (self.SONNET, judge(self.SONNET, "はい"))},
                           standin=(self.JEV, judge(self.JEV, "はい")) if standin else None, clock=lambda: now[0])
        return panel, calls, now

    @staticmethod
    def ask(panel, kind):
        import asyncio

        from meaning_judge import NoJudgment
        try:
            return asyncio.run(panel.ask(kind, None, "はい。"))
        except NoJudgment as exc:
            return exc

    def test_each_slot_asks_its_own_judge(self):
        panel, calls, _ = self.panel()
        self.assertEqual([self.ask(panel, k).by for k in ("judge", "judge2")], [self.LITE, self.SONNET])
        self.assertEqual(calls, [self.LITE, self.SONNET])

    def test_stopped_judge_is_replaced_and_skipped_for_a_while(self):
        # 止まっている（混雑の 503）：その発話は Jev が答え、DOWN_S 秒は問い合わせずに Jev へ。過ぎたら、また問い合わせる
        from meaning_judge import DOWN_S, OtherJudgeError
        panel, calls, now = self.panel(fail={self.LITE: OtherJudgeError(503, "混雑")})
        got = self.ask(panel, "judge")
        self.assertEqual((got.by, [t["model"] for t in got.tried]), (self.JEV, [self.LITE]))
        now[0] = DOWN_S - 1
        self.assertEqual(self.ask(panel, "judge").tried[0]["error"], "止まっている（前の問い合わせ）")
        self.assertEqual(calls, [self.LITE, self.JEV, self.JEV])
        now[0] = DOWN_S + 1
        self.ask(panel, "judge")
        self.assertEqual(calls[-2:], [self.LITE, self.JEV])

    def test_refused_request_is_replaced_only_for_that_utterance(self):
        # 問い合わせの中身が断られた（400）：その発話だけ Jev。止まっているとはみなさない（次の発話は、また問い合わせる）
        from meaning_judge import OtherJudgeError, outage
        panel, calls, _ = self.panel(fail={self.LITE: OtherJudgeError(400, "形が違う")})
        self.assertEqual(self.ask(panel, "judge").by, self.JEV)
        self.ask(panel, "judge")
        self.assertEqual(calls, [self.LITE, self.JEV, self.LITE, self.JEV])
        self.assertTrue(all(outage(e) for e in (OtherJudgeError(429, ""), OtherJudgeError(403, ""), OtherJudgeError(None, ""),
                                                 TimeoutError(), RuntimeError("どのキーでも使えません"))))

    def test_slow_judge_is_cut_and_replaced(self):
        # DOWN_AFTER_S 秒で打ち切り、止まっているとみなす（報告を待たせるのは2秒まで＝それより遅い判定は、その場の役に立たない）
        import unittest.mock as m
        panel, calls, _ = self.panel(took={self.LITE: 0.3})
        with m.patch("meaning_judge.DOWN_AFTER_S", 0.05):
            got = self.ask(panel, "judge")
        self.assertEqual(got.by, self.JEV)
        self.assertIn(self.LITE, panel.down_until)

    def test_standin_serves_one_slot_only(self):
        # 2社とも止まった：代わりは先に入った受け持ちだけ（同じ判定を2つの確かめに数えない）。入った受け持ちの判定が戻ったら返す
        from meaning_judge import NoJudgment, OtherJudgeError
        down = OtherJudgeError(503, "混雑")
        panel, calls, now = self.panel(fail={self.LITE: down, self.SONNET: down})
        self.assertEqual(self.ask(panel, "judge").by, self.JEV)
        self.assertIsInstance(self.ask(panel, "judge2"), NoJudgment)
        self.assertEqual(panel.holder, "judge")
        panel.judges["judge"] = (self.LITE, panel.standin[1])  # 3.5 Flash Lite が戻った（答えを返す判定に差し替え）
        now[0] = 1000
        self.ask(panel, "judge")
        self.assertIsNone(panel.holder)
        self.assertEqual(self.ask(panel, "judge2").by, self.JEV)

    def test_unsure_standin_is_not_an_answer(self):
        # Jev の確からしさが線（0.95）より下：答えにしない（もう1つの判定で決め、通話後の検証へ）
        from meaning_judge import NoJudgment, OtherJudgeError
        panel, _, _ = self.panel(fail={self.LITE: OtherJudgeError(503, "混雑")}, sure=0.9)
        got = self.ask(panel, "judge")
        self.assertIsInstance(got, NoJudgment)
        self.assertEqual({k: v for k, v in got.tried[-1].items() if k != "s"}, {"model": self.JEV, "unsure": "はい", "sure": 0.9})
        # 受け持ちに置いたとき（検証）も同じ線で読む
        from meaning_judge import JudgePanel
        alone = JudgePanel({"judge": (self.JEV, panel.standin[1])}, clock=panel.clock)
        self.assertIsInstance(self.ask(alone, "judge"), NoJudgment)

    def test_standin_of_the_same_maker_is_not_used(self):
        # 代わりが、ほかの受け持ちと同じ会社なら使わない（止まるのも遅れるのも一緒）
        from meaning_judge import JudgePanel, maker
        noop = None
        panel = JudgePanel({"judge": (self.LITE, noop), "judge2": (self.JEV, noop)}, standin=("Claude Haiku 5.5", noop))
        self.assertIsNotNone(panel.standin)
        panel = JudgePanel({"judge": (self.LITE, noop), "judge2": (self.SONNET, noop)}, standin=("Claude Haiku 5.5", noop))
        self.assertIsNone(panel.standin)
        self.assertEqual({maker(n) for n in (self.LITE, self.SONNET, self.JEV)}, {"google", "anthropic", "typesafe"})

    def test_forced_down_goes_the_same_way(self):
        # 試験の「止まっているものとして流す」（--judge-down）は、本物の失敗と同じ道を通る
        from meaning_judge import JudgePanel
        panel, calls, _ = self.panel()
        forced = JudgePanel(panel.judges, standin=panel.standin, down=[self.SONNET], clock=panel.clock)
        self.assertEqual(self.ask(forced, "judge2").by, self.JEV)
        self.assertIn(self.SONNET, forced.down_until)
        self.assertNotIn(self.SONNET, calls)

    def test_args_turn_on_the_standin(self):
        import argparse

        from meaning_judge import STANDIN, add_judge_args
        p = argparse.ArgumentParser()
        add_judge_args(p)
        a = p.parse_args(["--judge", "--judge2", "--judge-down", self.LITE])
        self.assertEqual((a.no_standin, a.judge_down, STANDIN), (False, [self.LITE], self.JEV))
        self.assertTrue(p.parse_args(["--no-standin"]).no_standin)


class FirstJudgmentTest(unittest.TestCase):
    """会社の違う2つの判定のうち、早く届いたほうで決める（convo.check_handler の hold・Flow.meaning_said）。遅れて届いたほうも同じ決まりで当てる。
    ⛔ 2026-10-19 G-57 の9回目：その場の判定（3.5 Flash Lite）だけを待ち、2秒超が328回中58回。G-61：違う会社の2つで早いほうは、遅いほうから1割 1.53秒"""

    def flow(self):
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "ご予約は。", "yes_no", conditions=("はい", "いいえ"))])
        f.use_checks("judge", "judge2")
        f.on_callee()
        return f

    def test_meaning_is_what_the_arrived_judgments_agree_on(self):
        from flowctl import UNDECIDED
        f = self.flow()
        f.on_heard("はい、しています。")
        self.assertIsNone(f.meaning_said)
        f.on_check("judge2", "はい", said="はい、しています。")
        self.assertEqual(f.meaning_said, "はい")      # 先に届いたほう（2つ目の受け持ちでも）で決める
        f.on_check("judge", "はい", said="はい、しています。")
        self.assertEqual(f.meaning_said, "はい")
        f.check["judge"] = "いいえ"
        self.assertEqual(f.meaning_said, UNDECIDED)   # 届いた判定どうしが食い違う ⇒ どちらにも決めない
        self.assertTrue(f.judging)

    def drive(self, took: dict, fail=(), binding=False):
        """2つの判定（took＝受け持ち ⇒ かかる秒・fail＝答えずに失敗する受け持ち）で、出揃った言葉への報告を受ける。"""
        import asyncio
        import unittest.mock as m
        from types import SimpleNamespace

        from convo import check_handler
        f, out = self.flow(), {}

        def judge(kind):
            async def run(q, text, asked=None, before=None):
                await asyncio.sleep(took[kind])
                if kind in fail:
                    raise RuntimeError("止まっている")
                return "はい"
            return run

        async def main():
            pending = []
            handle, _, info, _ = check_handler(f, judge=judge("judge"), judge2=judge("judge2"), pcm=b"\x00\x00", rate=16000, wav="x.wav",
                                               tasks=pending, audio_at=1.0)
            f.on_heard("はい、しています。"); f.on_activity(False, 2.0)
            f.wants_checks = lambda name, args=None: True
            f.binding = lambda name, args=None: binding
            handle_call = f.handle_call
            t0 = asyncio.get_running_loop().time()
            f.handle_call = lambda fc, midway=False: (out.update(check=dict(f.check), at=asyncio.get_running_loop().time() - t0)
                                                      or handle_call(fc, midway))
            await handle(SimpleNamespace(name="record_answer", args={"質問": "Q1", "答えの種類": "はい"}))
            await asyncio.wait(pending, timeout=3)
            out["info"], out["after"] = info, dict(f.check)
        with m.patch("stitch_gap.callee_speech", return_value=(b"", 16000, 0.0)):
            asyncio.run(main())
        return f, out

    def test_report_is_decided_by_the_first_judgment(self):
        # その場の判定が遅い（1.0秒）：先に届いた2つ目（0.1秒）で決め、遅いほうは待たない（あとで届いて、同じ決まりで当てる）
        f, out = self.drive({"judge": 1.0, "judge2": 0.1})
        self.assertEqual(set(out["check"]), {"judge2"})
        self.assertLess(out["at"], 0.6)
        self.assertEqual(out["info"]["waits"][-1]["judged"], ["judge2"])
        self.assertEqual(set(out["after"]), {"judge", "judge2"})
        self.assertEqual(f.items["Q1"].state, "accepted")

    def test_binding_report_waits_for_every_judgment(self):
        # 取り消せない報告（目的の人に繋ぐ＝Flow.binding）：待つ長さの中で、判定を全部待つ（早く届いたほうだけで繋がない）
        f, out = self.drive({"judge": 0.05, "judge2": 0.4}, binding=True)
        self.assertEqual(set(out["check"]), {"judge", "judge2"})
        self.assertTrue(out["info"]["waits"][-1]["every"])
        f, out = self.drive({"judge": 0.05, "judge2": 0.4})
        self.assertEqual(set(out["check"]), {"judge"})

    def test_failed_judgment_does_not_end_the_wait(self):
        # 先に終わったほうが答えずに失敗した：もう1つを待つ（失敗を「届いた」と数えない）
        f, out = self.drive({"judge": 0.05, "judge2": 0.3}, fail=("judge",))
        self.assertEqual(set(out["check"]), {"judge2"})
        self.assertIn("judge_error", out["info"])


class CallFactsTest(unittest.TestCase):
    """通話の事実（業務の判定＝DESIGN.md 7-1 への入力のうち、会話の側が決めるもの）を、根拠の発話つきで1か所（Flow._note）に残す。
    Intent.md 原則14：「もうかけないで」「営業はお断り」は、その場で発信禁止の申告。9-4：怒り・苦情は、弁明や約束をせず終え、人へ知らせる印。"""

    def flow(self):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
                     Question("Q2", "ご希望の日時をお聞かせいただいてもよろしいでしょうか？", "datetime", conditions=("答えた",))])

    @staticmethod
    def say(f, heard, op, **args):
        f.on_callee(); f.on_heard(heard)
        return f.apply(op, args)

    def test_refused_call_is_a_do_not_call_report(self):
        from phrases import CLOSE_DECLINED
        f = self.flow()
        r = self.say(f, "営業の電話ですよね。もうかけてこないでください。", "request_end", reason="callee_refused_call")
        self.assertEqual((r["result"], r["say"], r["ack"], f.phase), ("end", CLOSE_DECLINED, "shouchi", "closing"))
        self.assertEqual([x["turn"] for x in f.facts["do_not_call"]], [1])                       # 発話の位置
        self.assertIn("もうかけてこないで", f.facts["do_not_call"][0]["said"])                      # 根拠の発話
        self.assertEqual(f.facts["end"], [{"turn": 1, "said": f.facts["do_not_call"][0]["said"], "reason": "callee_refused_call"}])
        self.assertTrue(f.hurried)
        self.assertNotIn("complaint", f.facts)

    def test_complaint_is_marked_for_a_person_and_closed_without_promise(self):
        from phrases import CLOSE_DECLINED
        f = self.flow()
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        r = self.say(f, "何回かけてくるんですか。迷惑なんですけど。", "request_end", reason="callee_complaint")
        self.assertEqual((r["result"], r["say"], f.phase), ("end", CLOSE_DECLINED, "closing"))
        self.assertIsNone(r["ack"])                    # 締めの言い方そのものがお詫び（頭に「承知しました」を流さない）
        self.assertEqual(f.facts["complaint"][0]["turn"], 2)
        self.assertNotIn("do_not_call", f.facts)       # 苦情だけでは発信禁止にしない（7-1：人へ知らせる印は別に持つ）
        self.assertEqual(f.export(), {"Q1": "はい", "Q2": "-"})  # 取れた答えは残る

    def test_complaint_is_not_held_back_by_the_ambiguous_word_rule(self):
        # 「結構です」だけで断りにしない決まり（_ambiguous_only）は、急いでいる・断りの報告にだけ当てる（怒りは言葉が違う）
        f = self.flow()
        r = self.say(f, "結構です。", "request_end", reason="callee_complaint")
        self.assertEqual(r["result"], "end")

    def test_hurry_and_done_are_not_reports(self):
        f = self.flow()
        self.say(f, "すみません、今ちょっと忙しいんですけど。", "request_end", reason="callee_hurry")
        self.assertEqual((set(f.facts), f.facts["end"][0]["reason"], f.hurried), ({"end"}, "callee_hurry", True))
        f = self.flow()
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        self.say(f, "15日の午後2時でお願いします。", "record_answer", question_id="Q2", condition_id="答えた", value="15日の午後2時")
        r = self.say(f, "はい。", "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((r["result"], f.facts["end"][0]["reason"], f.hurried), ("end", "done", False))

    def test_word_alone_then_again_becomes_a_report(self):
        # 「結構です」だけ：1回目は答えとして確かめ、確かめたあとにまた言われたら断り＝発信禁止の申告（迷ったら原則14の側）
        f = self.flow()
        self.assertEqual(self.say(f, "結構です。", "request_end", reason="callee_refused_call")["result"], "rejected")
        self.assertNotIn("do_not_call", f.facts)
        f.apply("record_answer", {"question_id": "Q1", "condition_id": "いいえ", "value": "結構です"})
        self.say(f, "結構です。", "request_end", reason="callee_refused_call")
        self.assertEqual(len(f.facts["do_not_call"]), 1)

    def test_ai_sees_the_reasons_and_the_rule(self):
        from flowctl import END_LABEL, tool_declarations
        from instruction import Business, build
        decl = tool_declarations(list(self.flow().questions.values()))[0].function_declarations
        end = next(d for d in decl if d.name == "request_end")
        self.assertIn(END_LABEL["callee_complaint"], end.parameters.properties["理由"].enum)
        self.assertIn(END_LABEL["callee_complaint"], end.description)
        text = build(Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=list(self.flow().questions.values())), "S2")[1]
        self.assertIn(f"「{END_LABEL['callee_complaint']}」", text)
        self.assertIn("もうかけないで", text)

    def test_call_result_carries_the_facts(self):
        import inspect
        import convo
        self.assertIn('"facts": flow.facts', inspect.getsource(convo.run_call))


class PartyTest(unittest.TestCase):
    """相手の種別（DESIGN.md 4-3・6-2 の report_party）：人ではない相手（留守番電話・自動音声の案内・FAX）には話さずに終え、
    番号違いは決まった締めで終える。Intent.md 9-4：決まっていない場面は安全側（丁寧に終える）。留守番電話の伝言は業務設定（空なら残さない）。"""

    def flow(self, **kw):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))], **kw)

    @staticmethod
    def say(f, heard, op, **args):
        f.on_callee(); f.on_heard(heard)
        return f.apply(op, args)

    def test_not_a_person_hangs_up_without_a_word(self):
        from types import SimpleNamespace
        from flowctl import PARTY_LABEL
        for party, heard in (("voicemail", "ただいま電話に出ることができません。発信音のあとにお名前とご用件をお話しください。"),
                             ("ivr", "お電話ありがとうございます。ご用件の番号を押してください。"), ("fax", "（ピー、ピー）")):
            f = self.flow()
            f.on_callee(); f.on_heard(heard)
            reply, silent = f.handle_call(SimpleNamespace(name="report_party", args={"相手": PARTY_LABEL[party]}))
            last = f.log[-1]["reply"]
            self.assertEqual((last["result"], last.get("hang_up"), last.get("say"), last.get("play"), last["ack"]),
                             ("end", True, None, None, None), party)
            self.assertTrue(silent, party)                                   # AIに話させない
            self.assertNotIn("hang_up", reply)                               # 中で使う印（電話接続側が切る）。AIには見せない
            self.assertEqual((f.facts["party"][0]["party"], f.end_reason, f.phase, f.hurried), (party, party, "closing", False))
            self.assertEqual(f.export(), {"Q1": "-"})

    def test_fax_is_known_by_its_sound(self):
        # ファクスの音は、会話AIに聞かせる前にプログラムが見分けて切る（⛔ G-57：会話AIは3本とも何も報告せずに黙った）。人の声は当たらない
        import glob
        from audio import fax_tone_seconds, is_fax_tone
        from common import HERE, read_wav_pcm16_mono
        from make_callee_samples import tone_pcm
        self.assertTrue(is_fax_tone(tone_pcm("fax"), 16000))
        voices = [p for p in sorted(glob.glob(str(HERE / "results" / "callee" / "*.wav"))) if "fax_tone" not in p]
        self.assertGreater(len(voices), 100)
        self.assertEqual([p for p in voices if fax_tone_seconds(*read_wav_pcm16_mono(p)) >= 0.2], [])
        f = self.flow()
        f.on_callee()
        r = f.on_signal("fax")
        self.assertEqual((r["result"], r.get("hang_up"), f.facts["party"][0]["by"], f.end_reason), ("end", True, "signal", "fax"))
        import inspect
        import convo
        self.assertIn("is_fax_tone(pcm, prate)", inspect.getsource(convo.run_call))

    def test_voicemail_message_is_left_only_when_the_business_sets_one(self):
        msg = "株式会社アオバ物流の佐藤です。ご予約の確認でお電話いたしました。改めてお電話いたします。"
        f = self.flow(voicemail_message=msg)
        r = self.say(f, "ただいま電話に出ることができません。", "report_party", party="voicemail")
        self.assertEqual((r["result"], r["say"], r.get("hang_up")), ("end", msg, None))
        # 録音で流す形：設定の伝言は依頼主の単位の録音（名乗りの部分 voicemail。✅ 2026-10-19 ユーザー：「依頼者ごとに設定で選べる形にして、
        # 必要なら録音をしましょう」）を流し、AIは黙る
        from instruction import Business
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=list(self.flow().questions.values()),
                     voicemail_message=msg)
        self.assertEqual(b.replay_parts()["voicemail"], msg)
        self.assertNotIn("voicemail", Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認",
                                               questions=list(self.flow().questions.values())).replay_parts())  # 設定が無ければ録音も無い
        f = self.flow(voicemail_message=msg, play_recordings=True, opening=b.replay_parts())
        r = self.say(f, "ただいま電話に出ることができません。", "report_party", party="voicemail")
        self.assertEqual(r["play"], [{"kind": "opening", "part": "voicemail", "text": msg}])
        self.assertEqual(f.end_reason, "voicemail")

    def test_wrong_party_closes_politely(self):
        from phrases import CLOSE_DECLINED
        f = self.flow()
        r = self.say(f, "え、うちは山田ですけど、番号をお間違えではないですか？", "report_party", party="wrong_party")
        self.assertEqual((r["result"], r["say"], r["ack"], f.end_reason, f.hurried), ("end", CLOSE_DECLINED, None, "wrong_party", False))
        self.assertIn("番号をお間違え", f.facts["party"][0]["said"])

    def test_people_are_noted_and_the_flow_goes_on(self):
        # 受付・目的の人：いまは記録だけ（目的の人に繋がってから質問を始める流れは、目的の人のある業務で使う）
        f = self.flow()
        r = self.say(f, "はい、株式会社アオバでございます。", "report_party", party="reception")
        self.assertEqual((r["result"], f.current, f.phase), ("noted", "Q1", "asking"))
        self.assertIn("Q1", r["message"])

    def test_ai_sees_the_operation_and_the_rule(self):
        from flowctl import PARTY_LABEL, tool_declarations
        from instruction import Business, build
        qs = list(self.flow().questions.values())
        d = next(d for d in tool_declarations(qs)[0].function_declarations if d.name == "report_party")
        self.assertEqual(d.parameters.properties["相手"].enum,
                         [PARTY_LABEL[k] for k in ("voicemail", "ivr", "fax", "wrong_party")])  # 目的の人の無い業務では、受付・目的の人は見せない
        text = build(Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=qs), "S2")[1]
        self.assertIn("report_party", text)
        self.assertIn("何も言わずに", text)


class CallbackTest(unittest.TestCase):
    """かけ直しの申し出（DESIGN.md 6-2 の request_callback）：理由ごとにシステムが足す質問を、ふつうの質問と同じ流れ（言い直して確かめる）で聞き、
    取れたら締める。残りの業務の質問は聞かない（目的の人ではない・いまは話せない相手）。答えは依頼主へ渡す項目に入れず、通話の事実に残す。"""

    def flow(self, play=False):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
                     Question("Q2", "ご希望の日時をお聞かせいただいてもよろしいでしょうか？", "datetime", conditions=("答えた",))],
                    play_recordings=play)

    @staticmethod
    def say(f, heard, op, **args):
        f.on_callee(); f.on_heard(heard)
        return f.apply(op, args)

    def test_absent_asks_when_back_and_closes(self):
        from phrases import CALLBACK_ASK, CLOSING
        f = self.flow()
        r = self.say(f, "あいにく本人は出かけておりまして。", "request_callback", why="absent")
        self.assertEqual((r["result"], r["question_id"], r["question"], r["ack"]), ("next", "C1", CALLBACK_ASK["back_time"], "shouchi"))
        r = self.say(f, "3時ごろには戻ると思います。", "record_answer", question_id="C1", condition_id="答えた", value="3時ごろ")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "午後3時ごろ"))   # 時刻はほかの値と同じく言い直して確かめる
        r = self.say(f, "はい。", "confirm_field", question_id="C1", result="affirmed")
        self.assertEqual((r["result"], r["say"], f.end_reason), ("end", CLOSING, "callback"))  # 残りの Q2 は聞かない
        self.assertEqual(f.facts["callback"], [{"turn": 1, "said": "あいにく本人は出かけておりまして。", "why": "absent",
                                                "time": "午後3時ごろ", "number": None}])
        self.assertEqual(f.export(), {"Q1": "-", "Q2": "-"})                                  # 依頼主へ渡す項目には入れない

    def test_other_number_asks_number_then_reachable_time(self):
        from phrases import CALLBACK_ASK
        f = self.flow()
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        r = self.say(f, "この番号じゃなくて、携帯のほうにかけてもらえますか。", "request_callback", why="other_number")
        self.assertEqual((r["question_id"], r["question"]), ("C2", CALLBACK_ASK["number"]))
        self.say(f, "090-1234-5678です。", "record_answer", question_id="C2", condition_id="答えた", value="090-1234-5678")
        r = self.say(f, "はい。", "confirm_field", question_id="C2", result="affirmed")
        self.assertEqual((r["result"], r["question_id"], r["question"]), ("next", "C1", CALLBACK_ASK["reachable_time"]))
        r = self.say(f, "ちょっとわからないです。", "record_answer", question_id="C1", condition_id="unknown")
        self.assertEqual((r["result"], f.end_reason), ("end", "callback"))                    # 出られる時間は分からなくてよい
        self.assertEqual((f.facts["callback"][0]["number"], f.facts["callback"][0]["time"]), ("090-1234-5678", None))
        self.assertEqual(f.export(), {"Q1": "はい", "Q2": "-"})

    def test_later_time_is_asked_back_when_unclear_but_not_rechecked(self):
        from flowctl import Limits
        f = self.flow()
        self.say(f, "今ちょっと手が離せないので、あとでかけてもらえますか。", "request_callback", why="later")
        for _ in range(Limits().ask_back):
            self.assertEqual(self.say(f, "えっと……", "report_unclear", question_id="C1")["result"], "ask_back")
        r = self.say(f, "えっと……", "report_unclear", question_id="C1")
        self.assertEqual((r["result"], f.end_reason, f.facts["callback"][0]["time"]), ("end", "callback", None))  # 念のための確認はしない

    def test_resume_during_callback(self):
        # 張り替えのときの「いまの位置」：足した質問がいまの質問でも書ける（⛔ 前は業務の質問の一覧から探していて、落ちる形だった）
        from instruction import Business, position_text
        from phrases import CALLBACK_ASK
        f = self.flow()
        self.say(f, "あいにく本人は出かけておりまして。", "request_callback", why="absent")
        b = Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=[f.questions["Q1"], f.questions["Q2"]])
        self.assertIn(CALLBACK_ASK["back_time"], position_text(b, f))

    def test_only_one_callback_request(self):
        f = self.flow()
        self.say(f, "あとでかけてもらえますか。", "request_callback", why="later")
        r = self.say(f, "あ、やっぱり携帯にかけてください。", "request_callback", why="other_number")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("C1", r["message"])

    def test_recorded_when_questions_are_recorded(self):
        # 足す質問も、録音で流す形では契約者の単位の録音を流す（つなぎの言葉は付けない）。⛔ G-57：AIに言わせると、3本中1本で
        # 「私の方では折り返しのお電話ができかねますので」と足した
        from phrases import CALLBACK_ASK, RECORDED
        f = self.flow(play=True)
        r = self.say(f, "あいにく本人は出かけておりまして。", "request_callback", why="absent")
        self.assertEqual(r["play"], [{"kind": "recorded", "id": "callback_back_time", "text": CALLBACK_ASK["back_time"]}])
        self.assertEqual(RECORDED["callback_back_time"], CALLBACK_ASK["back_time"])
        # 申し出の言葉が「〜もらえますか？」でも、相手からの質問として答えさせない（⛔ G-57 の2回目：AIに答えさせて、6本中2本で
        #    「私の方では折り返しのお電話はお約束できかねます」と言い、足す質問も録音でなくAIが言った）
        for why, heard in (("later", "今ちょっと手が離せないので、あとでかけてもらえますか？"), ("other_number", "携帯のほうにかけてもらえますか？")):
            f = self.flow(play=True)
            r = self.say(f, heard, "request_callback", why=why)
            self.assertEqual(r["play"][0]["kind"], "recorded", why)
            self.assertNotIn("相手が質問をしています", r["message"], why)
        f = self.flow(play=False)                                                       # AIが話す形では AIが言う
        r = self.say(f, "あいにく本人は出かけておりまして。", "request_callback", why="absent")
        self.assertNotIn("play", r)
        self.assertIn(CALLBACK_ASK["back_time"], r["message"])

    def test_ai_sees_the_operation_and_the_questions(self):
        from flowctl import CALLBACK_LABEL, tool_declarations
        from instruction import Business, build
        qs = list(self.flow().questions.values())
        decl = {d.name: d for d in tool_declarations(qs)[0].function_declarations}
        self.assertEqual(decl["request_callback"].parameters.properties["かけ直しの理由"].enum, list(CALLBACK_LABEL.values()))
        self.assertIn("C1", decl["record_answer"].parameters.properties["質問"].enum)
        self.assertIn("C2", decl["confirm_field"].parameters.properties["質問"].enum)
        text = build(Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認", questions=qs), "S2")[1]
        self.assertIn("request_callback", text)


class VoiceFeaturesTest(unittest.TestCase):
    """名前の録音を継ぐ位置と、名前の部分を選ぶ条件に使う声の数（voice_features。G-58 の4回目）。"""

    @staticmethod
    def pcm(x):
        import numpy as np
        return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()

    def test_unvoiced_span_is_found_between_voiced_parts(self):
        # 声（150Hz の音）・息の音（雑音）・声 ⇒ 息の音の区間だけが、声帯の震えの無い区間（「っし」の「シ」で継ぐため）
        import numpy as np
        from voice_features import unvoiced_spans
        rate = 24000
        t = np.arange(int(0.4 * rate)) / rate
        voiced = 0.5 * np.sin(2 * np.pi * 150 * t)
        noise = 0.2 * np.random.default_rng(0).standard_normal(int(0.15 * rate))
        spans = unvoiced_spans(self.pcm(np.concatenate([voiced, noise, voiced])), rate)
        self.assertEqual(len(spans), 1, spans)
        self.assertAlmostEqual(spans[0][0], 0.4, delta=0.04)
        self.assertAlmostEqual(spans[0][1], 0.55, delta=0.04)

    def test_seam_is_in_the_breath_after_the_particle(self):
        # 名前の録音の継ぎ目（name_clips.seam_cut）：「様」の次の言葉（で）より後ろの、最初の息の音（音の重心が高い＝「し」の息）。
        # その前の、かすれた声のような低い音（⛔ 2026-10-19：「様は｜いら」の間で、音の重心 1,400〜2,600Hz）は継ぎ目にしない
        import numpy as np
        from name_clips import join, seam_cut
        rate, rng = 24000, np.random.default_rng(1)

        def tone(s):
            t = np.arange(int(s * rate)) / rate
            return 0.5 * np.sin(2 * np.pi * 150 * t)

        def noise(s, high):
            x = rng.standard_normal(int(s * rate))
            return 0.2 * (np.diff(x, prepend=0) if high else np.convolve(x, np.ones(12) / 12, "same"))
        pcm = self.pcm(np.concatenate([tone(0.6), noise(0.08, False), tone(0.25), noise(0.12, True), tone(0.4)]))
        times = [("岡田", 0.0, 0.3), ("様", 0.3, 0.45), ("で", 0.45, 0.6), ("いら", 0.68, 0.93), ("っし", 0.93, 1.05), ("ゃいます", 1.05, 1.45)]
        cut = seam_cut(pcm, rate, times)
        self.assertIsNotNone(cut)
        self.assertTrue(0.93 <= cut / rate <= 1.05, cut / rate)
        self.assertIsNone(seam_cut(pcm, rate, [("岡田", 0.0, 0.3), ("さん", 0.3, 0.6)]))   # 「様」の無い文には継ぎ目が無い
        tail = {"pcm": pcm[cut * 2:]}
        self.assertEqual(join(pcm, cut, tail), pcm)                                    # 組み合わせは、そのまま並べるだけ

    def test_loudness_difference_in_db(self):
        # 同じ音の振幅が半分なら、約6dB 小さい（継ぎ目の前の大きさの差＝g58_name_splice.MAX_SEAM_DB で比べる）
        import numpy as np
        from voice_features import loudness_db
        t = np.arange(24000) / 24000
        a, b = loudness_db(self.pcm(0.5 * np.sin(2 * np.pi * 150 * t))), loudness_db(self.pcm(0.25 * np.sin(2 * np.pi * 150 * t)))
        self.assertAlmostEqual(a - b, 6.02, delta=0.05)
        self.assertEqual(loudness_db(b""), -999.0)


class ClipTablesTest(unittest.TestCase):
    """録音を作る表（make_ack_clips）に、どの録音にも読み方と長さの上限がある（⛔ 2026-10-19 の見直しで見つけた漏れ：かけ直しの質問を
    契約者の単位の録音に足したとき、読み方も上限も無く、作ると止まった）。依頼主の単位の録音には「AIですか」への言い方と伝言も入る。"""

    def test_every_recording_has_a_style_and_a_limit(self):
        import dataclasses
        from g22_opening import BUSINESS
        from make_ack_clips import HOW, MAX_S, script_table, words
        table = {**words(False)[0], **script_table(dataclasses.replace(BUSINESS, voicemail_message="株式会社アオバ物流の佐藤でございます。"))[0]}
        for aid, (_, how) in table.items():
            self.assertIn(how, MAX_S, aid)
            self.assertTrue(how in HOW or aid in ("hai", "shouchi", "arigatou", "shitsurei", "moushiwake", "moushiwake_heavy"), aid)
        self.assertIn("ai", table)
        self.assertIn("voicemail", table)
        self.assertIn("callback_back_time", table)

    def test_words_after_a_name_are_read_in_context(self):
        # ⛔ 2026-10-19：名前の後ろの「様はいらっしゃいますでしょうか？」だけを読むと「様」が「よう」になり、名前の入る文の録音が全部落ちた
        from make_ack_clips import heard_ok
        text = "営業部部長の「おかだ」様はいらっしゃいますでしょうか？"
        self.assertTrue(heard_ok(text, {"0.0": "営業部部長の岡田様はいらっしゃいますでしょうか？", "1.0": "営業部部長の岡田様はいらっしゃいますでしょうか？"}))
        self.assertFalse(heard_ok(text, {"0.0": "営業部部長の岡田様はいらっしゃいますか？", "1.0": "営業部部長の岡田様はいらっしゃいますか？"}))


class AIQuestionTest(unittest.TestCase):
    """「AIですか？」と聞かれたとき（Intent.md 9-4・原則8）。✅ 2026-10-19 ユーザー：「絶対にAIであることを伝えてはいけない」「人だと断定するのも
    問題がある」⇒ 1回目は決まった言い方「何か問題でもございましたでしょうか？」（業務設定 Business.ai_answer、無ければ phrases.AI_ANSWER）を
    流して相手の返事を待つ。問題は無いと言われたら「失礼いたしました。」のあと、いまの位置へ戻る。⛔ 打ち消し（「いえ」）・人だという答えは
    言わない（Intent.md 9-4「人間だとは答えない」）。重ねて聞かれたら（⏳ ユーザーに確かめる）、決まるまでは人ともAIとも言わずに丁寧に終える。
    冒頭でAIだとは名乗らない（2026-10-12 ユーザー確定）。"""

    def business(self, **kw):
        from flowctl import Question
        from instruction import Business
        return Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認",
                        questions=[Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
                                   Question("Q2", "ご来店の日にちを教えていただけますでしょうか？", "date", conditions=("答えた",))], **kw)

    def flow(self, recorded=True, **kw):
        from flowctl import Flow
        b = self.business(**kw)
        return Flow(b.questions, play_recordings=recorded, opening=b.replay_parts() if recorded else None)

    def test_the_words_neither_deny_nor_tell(self):
        from phrases import AI_ANSWER
        self.assertEqual(AI_ANSWER, "何か問題でもございましたでしょうか？")                 # ✅ ユーザーの言い方のまま
        for w in ("いえ", "人間", "エーアイ", "AI", "機械", "自動"):
            self.assertNotIn(w, AI_ANSWER)
        self.assertNotIn("、", AI_ANSWER)                                             # 決まった言い方には読点を入れない（10-15）

    def test_rule_hands_the_answer_to_the_program(self):
        from flowctl import PART_LABEL
        from instruction import build
        for m in ("ai", "recorded"):
            t = build(self.business(), "S2", opening_mode=m)[1]
            self.assertIn(f"replay_opening を「{PART_LABEL['ai']}」で呼びます", t)
            self.assertIn("人間だとも、エーアイだとも、自分では言いません", t)
        mine = "どういったことでしょうか？"
        self.assertEqual(self.business(ai_answer=mine).replay_parts()["ai"], mine)
        from phrases import AI_ANSWER
        self.assertEqual(self.business().replay_parts()["ai"], AI_ANSWER)

    def test_first_ask_plays_the_words_and_waits(self):
        # 決まった言い方の録音だけを流し、質問の録音は続けない（相手に聞き返したので、返事を待つ）。頭にお詫びは重ねない
        f = self.flow()
        f.on_callee()
        r = f.apply("replay_opening", {"part": "ai"})
        self.assertEqual(([p.get("part", p["kind"]) for p in r["play"]], r["ack"], r["result"]), (["ai"], None, "ai_question"))
        self.assertEqual((f.current, f.ai_pending, [x["turn"] for x in f.facts["ai_question"]]), ("Q1", True, [1]))
        self.assertIn("質問に戻る", r["message"])

    def test_no_problem_goes_back_to_the_question(self):
        f = self.flow()
        f.on_callee()
        f.apply("replay_opening", {"part": "ai"})
        f.on_callee()                                                              # 「いや、なんか機械っぽかったので」
        r = f.apply("replay_opening", {"part": "resume"})
        self.assertEqual(([p.get("id") for p in r["play"]], r["ack"]), (["Q1"], "shitsurei"))   # 「失礼いたしました。」＋いまの質問の録音
        self.assertFalse(f.ai_pending)
        self.assertEqual(f.apply("replay_opening", {"part": "resume"})["result"], "rejected")     # 戻るのは1回だけ

    def test_resume_while_a_restatement_waits(self):
        # 言い直しの返事を待っている間に聞かれた：戻るときは、AIが言い直しをもう一度言う（録音の質問ではない）
        f = self.flow()
        f.on_callee(); f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})
        f.on_callee(); r = f.apply("record_answer", {"question_id": "Q2", "condition_id": "答えた", "value": "15日"})
        self.assertEqual(r["result"], "confirm_required")
        f.on_callee(); f.apply("replay_opening", {"part": "ai"})
        f.on_callee(); r = f.apply("replay_opening", {"part": "resume"})
        self.assertTrue(r.get("speak"))
        self.assertIn(f.awaiting_say, r["message"])

    def test_an_answer_instead_ends_the_wait(self):
        f = self.flow()
        f.on_callee(); f.apply("replay_opening", {"part": "ai"})
        f.on_callee(); f.apply("record_answer", {"question_id": "Q1", "condition_id": "はい"})       # 返事の代わりに答えた
        self.assertFalse(f.ai_pending)
        self.assertEqual(f.apply("replay_opening", {"part": "resume"})["result"], "rejected")

    def test_asked_again_never_closes(self):
        # ✅ 2026-10-19 ユーザー：重ねて聞かれて終えるのは「あまりにも失礼だし、AIだと認めているのと同じ」⇒ お詫びと約束の録音のあと、いまの質問へ。
        # ✅ 同日：「2回目だろうと、3回目だろうと、相手に不信感を与えることに変わりないでしょ」⇒ 3回目からも終えない。「いかがなさいましたか？」
        #    「どうなさいましたか？」を交互に聞いて、相手の返事を待つ（1回目と同じ）
        from phrases import AI_ASKED_AGAIN, SILENCE_PROMPT
        f = self.flow()
        f.on_callee(); f.apply("replay_opening", {"part": "ai"})
        f.on_callee(); r = f.apply("replay_opening", {"part": "ai"})               # 「本当に人ですか？」
        self.assertEqual([(p["kind"], p.get("id")) for p in r["play"]], [("recorded", "ai_again"), ("question", "Q1")])
        self.assertEqual((r["play"][0]["text"], f.end_reason, f.current), (AI_ASKED_AGAIN, None, "Q1"))
        for w in ("違います", "人です", "人間", "エーアイ"):
            self.assertNotIn(w, AI_ASKED_AGAIN)
        for n, (rid, text) in enumerate([("ikaga", SILENCE_PROMPT[0]), ("dou", SILENCE_PROMPT[1]), ("ikaga", SILENCE_PROMPT[0])], 3):
            f.on_callee(); r = f.apply("replay_opening", {"part": "ai"})           # 3回目・4回目・5回目
            self.assertEqual((r["result"], [(p["kind"], p.get("id"), p["text"]) for p in r["play"]]), ("ai_question", [("recorded", rid, text)]), n)
            self.assertEqual((f.end_reason, f.ai_pending, f.current), (None, True, "Q1"), n)
            self.assertIn(f"「{text}」への、相手の返事を待っています", r["message"])
        f.on_callee(); r = f.apply("replay_opening", {"part": "resume"})           # 「いや、ちょっと気になっただけです。」
        self.assertEqual((f.end_reason, f.ai_pending, f.current), (None, False, "Q1"))
        self.assertEqual([x["turn"] for x in f.facts["ai_question"]], [1, 2, 3, 4, 5])

    def test_ai_speaking_mode_says_the_same_words(self):
        # AIが話す形（名乗りの録音が無い）でも、言い方はプログラムが渡す（AIに自分で答えさせない）
        from phrases import AI_ANSWER
        f = self.flow(recorded=False)
        f.on_callee()
        r = f.apply("replay_opening", {"part": "ai"})
        self.assertEqual((r["result"], r["say"]), ("ai_question", AI_ANSWER))
        f.on_callee()
        r = f.apply("replay_opening", {"part": "resume"})
        self.assertTrue(r.get("speak"))
        self.assertIn("Q1", r["message"])

    def test_rule_survives_a_session_swap(self):
        # 張り替えたあとのセッションにも同じ決まり（決まりは system_rules の1か所＝どのセッションの指示にも入る）
        from flowctl import Flow, PART_LABEL
        from instruction import build
        b = self.business()
        self.assertIn(f"「{PART_LABEL['ai']}」で呼びます", build(b, "S2", resume=Flow(b.questions))[1])

    def test_not_announced_at_the_start(self):
        b = self.business()
        self.assertNotIn("エーアイ", b.opening() + "".join(b.opening_parts()))


class TargetTest(unittest.TestCase):
    """目的の人に繋がってから必須の質問を始める（DESIGN.md 4-3。✅ 2026-10-13 ユーザー：「決裁権者と話したいのに、関係ない人と話しても意味が
    ない」）。名乗りの終わりは目的の人を呼ぶ一言。繋がったら、その方にまだ届いていない名乗り・用件を流してから最初の質問。"""

    def business(self, **kw):
        import dataclasses
        from g22_opening import BUSINESS
        from instruction import Target
        return dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長", **kw))

    def flow(self, play=True, recorded=True):
        from flowctl import Flow
        b = self.business()
        return Flow(b.questions, play_recordings=play, opening=b.replay_parts() if recorded else None, target=b.replay_parts()), b

    @staticmethod
    def say(f, heard, op, **args):
        f.on_callee(); f.on_heard(heard)
        return f.apply(op, args)

    def test_opening_ends_with_the_call_and_no_question_until_connected(self):
        f, b = self.flow()
        self.assertEqual(b.opening_parts()[-1], "営業部部長の「おかだ」様はいらっしゃいますでしょうか？")
        self.assertEqual((f.phase, f.current), ("calling", None))
        r = self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("目的の人", r["message"])
        self.assertEqual(self.say(f, "はい。", "request_end", reason="done")["result"], "rejected")  # まだ終えられない（質問を始めていない）

    def test_first_person_is_the_target(self):
        # 最初に出た方が目的の人：まだ届いていない用件と、最初の質問を流す（名乗りはもう届いている）
        f, b = self.flow()
        r = self.say(f, "はい、岡田は私ですが。", "report_party", party="target")
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["purpose", "question"])
        self.assertEqual((f.phase, f.current, f.party), ("asking", "Q1", "target"))

    def test_transferred_target_hears_everything(self):
        # 受付の方に取り次いでいただき、代わった方が目的の人：あいさつ・名乗り・用件・最初の質問（呼んだときの名乗りは受付の方にしか届いていない）
        from types import SimpleNamespace
        f, b = self.flow()
        f.on_callee(); f.on_heard("少々お待ちください。")
        reply, silent = f.handle_call(SimpleNamespace(name="report_party", args={"相手": "受付の方"}))
        # お取り次ぎを待つ間は話させない（⛔ G-57：話させる形で返していたので、3本中2本でAIが話した）
        self.assertEqual((f.log[-1]["reply"]["result"], silent), ("noted", True))
        self.assertNotIn("silent", reply)
        r = self.say(f, "お電話代わりました、岡田です。", "report_party", party="target")
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["greeting", "intro", "purpose", "question"])

    def test_handover_replay_uses_the_same_target_decision(self):
        # AIが「代わった方」として名乗りの流し直しを頼んだ：目的の人かは、目的の人の報告と同じ1か所で決める（判定が「ご本人」なら繋いで
        # 全部を流す・判定が無ければ目的の人かを確かめる）。⛔ 2026-10-19 G-57：判定が「ご本人」でも、目的の人かを聞き直していた
        # ⛔ 2026-10-21：繋ぐのは「ご本人」の読みが2つ以上そろったときだけ（読み＝AIの「目的の人」の報告・届いた判定の1つずつ）。AIが「代わった方」
        #    と報告していて、届いた判定が1つだけなら、目的の人かを確かめる（G-59：3.5 Flash Lite は、その前の言葉を判定に生かせない。
        #    「お電話代わりました。山本です。」を3回中1回「ご本人」と読んだ）
        from flowctl import HOLD, TARGET_IS_THERE
        for both in (True, False):
            f, b = self.flow()
            f.use_checks("judge", "judge2")
            f.on_callee(); f.on_heard("少々お待ちください。"); f.on_check("judge", HOLD)
            f.on_callee(); f.on_heard("お電話変わりました。岡田です。"); f.on_check("judge", TARGET_IS_THERE)
            if both:
                f.on_check("judge2", TARGET_IS_THERE)
            r = f.apply("replay_opening", {"part": "handover"})
            if both:
                self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["greeting", "intro", "purpose", "question"])
                self.assertEqual(f.phase, "asking")
            else:
                self.assertEqual((r["result"], f.phase), ("confirm_target", "calling"))
        f, b = self.flow()                                           # AIの「目的の人」の報告＋判定1つ＝読みが2つ ⇒ 繋ぐ
        f.use_checks("judge", "judge2")
        f.on_callee(); f.on_heard("岡田は私ですが。"); f.on_check("judge", TARGET_IS_THERE)
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"] != "confirm_target", True)
        self.assertEqual(f.phase, "asking")
        f, b = self.flow()
        f.on_callee(); f.on_heard("お電話変わりました。")
        self.assertEqual(f.apply("replay_opening", {"part": "handover"})["result"], "confirm_target")

    def test_hold_seen_by_the_judge_also_means_a_new_person(self):
        from flowctl import HOLD
        f, b = self.flow()
        f.on_callee(); f.on_heard("少々お待ちください。"); f.on_check("judge", HOLD)
        r = self.say(f, "お電話代わりました、岡田です。", "report_party", party="target")
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["greeting", "intro", "purpose", "question"])

    def test_asked_the_purpose_then_call_once_more(self):
        # 目的の人ではない方が用件を聞いてきたら、用件の一文を流してから、もう一度だけ呼ぶ一言（2回目からは呼ばない）
        f, b = self.flow()
        r = self.say(f, "どういったご用件でしょうか？", "replay_opening", part="purpose")
        self.assertEqual([p["part"] for p in r["play"]], ["purpose", "target"])
        r = self.say(f, "えっと、どちら様でしたっけ？", "replay_opening", part="intro")
        self.assertEqual([p["part"] for p in r["play"]], ["intro"])
        self.assertEqual(f.phase, "calling")
        r = self.say(f, "はい、私が岡田です。", "report_party", party="target")      # 用件はもう届いている ⇒ 最初の質問だけ
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["question"])

    def test_purpose_is_not_volunteered_to_reception(self):
        # 名乗りを聞き返されたときは、名乗りと呼ぶ一言だけ（用件は聞かれたときだけ。届いていない用件を足すのは、質問へ進む前＝繋がってから）
        f, b = self.flow()
        r = self.say(f, "どちら様ですか？", "replay_opening", part="intro")
        self.assertEqual([p["part"] for p in r["play"]], ["intro", "target"])

    def test_target_again_before_any_answer_is_a_new_person(self):
        # 繋いだあと、何も答えないうちに、また目的の人が出た：代わった方への流し直し（あいさつ・名乗り・用件・いまの質問）
        f, b = self.flow()
        self.say(f, "岡田ですね。", "report_party", party="target")                 # 受付の方を目的の人と報告してしまった
        r = self.say(f, "お電話代わりました。岡田です。", "report_party", party="target")
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["greeting", "intro", "purpose", "question"])
        self.say(f, "はい、予約しています。", "record_answer", question_id="Q1", condition_id="はい", value="予約しています")
        r = self.say(f, "岡田は私です。", "report_party", party="target")             # 答えたあとは同じ方
        self.assertEqual(r["result"], "noted")

    def test_unnamed_new_person_is_asked_once(self):
        # 代わった方が名乗らなかった：「「おかだ」様でいらっしゃいますでしょうか？」と1回だけ聞く。認めたら、全部流して質問へ。
        # 録音で流す通話では録音（呼ぶ一言と同じ。⛔ 2026-10-19 G-62：AIに言わせると名前を言い崩した）。AIが話す通話では、同じ文をAIが言う
        from flowctl import HANDOVER
        g, _ = self.flow(recorded=False)
        g.use_checks("judge")
        self.heard(g, "はい、お電話代わりました。", HANDOVER)
        r = g.apply("report_party", {"party": "target"})
        self.assertEqual((r["result"], r["say"]), ("confirm_target", "「おかだ」様でいらっしゃいますでしょうか？"))
        self.assertNotIn("play", r)
        f, b = self.flow()
        r = self.say(f, "はい、お電話代わりました。", "replay_opening", part="handover")
        self.assertEqual((r["result"], [(p["part"], p["text"]) for p in r["play"]]),
                         ("confirm_target", [("target_confirm", "「おかだ」様でいらっしゃいますでしょうか？")]))
        self.assertNotIn("say", r)
        self.assertIn("何も言わずに", r["message"])
        # 確かめへの返事を、最初の質問の答えとして報告されたら：確かめへの返事を待っていることを伝えて差し戻す
        # ⛔ 2026-10-19 G-57（目的の人_本人）：差し戻しが「お取り次ぎいただくのを待っています」のままで、AIは答えの報告を続けた
        r = self.say(f, "はい、そうです。", "record_answer", question_id="Q1", condition_id="はい", value="はい")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("「「おかだ」様でいらっしゃいますでしょうか？」と聞いて、返事を待っています", r["message"])
        self.assertIn("record_answer で報告したりしません", r["message"])
        self.assertNotIn("まだ受け付けていません", r["message"])   # AIは「目的の人」と報告していない（代わった方として流し直しを頼んだ）
        r = f.apply("report_party", {"party": "target"})
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["greeting", "intro", "purpose", "question"])

    def test_absent_target_goes_to_callback(self):
        from phrases import CALLBACK_ASK
        f, b = self.flow()
        r = self.say(f, "あいにく岡田は席を外しております。", "request_callback", why="absent")
        self.assertEqual((r["question_id"], r["question"], f.phase), ("C1", CALLBACK_ASK["back_time"], "asking"))

    def test_reception_refusing_is_a_do_not_call_report(self):
        f, b = self.flow()
        r = self.say(f, "営業のお電話はお断りしております。", "request_end", reason="callee_refused_call")
        self.assertEqual((r["result"], len(f.facts["do_not_call"])), ("end", 1))

    def test_ai_speaking_form(self):
        # AIが話す形：繋がったら、AIが用件を言ってから最初の質問を聞く
        f, b = self.flow(play=False, recorded=False)
        r = self.say(f, "はい、私です。", "report_party", party="target")
        self.assertIn(b.purpose_line, r["message"])
        self.assertIn(b.questions[0].text, r["message"])
        self.assertNotIn("play", r)

    def test_unknown_party_or_anyone_business_is_unchanged(self):
        from flowctl import Flow
        from g22_opening import BUSINESS
        f = Flow(BUSINESS.questions)
        self.assertEqual((f.phase, f.current), ("asking", "Q1"))

    def test_role_only_and_person(self):
        from instruction import Target
        self.assertEqual(Target(role="社長").call(), "社長様はいらっしゃいますでしょうか？")
        self.assertEqual(Target(role="院長").confirm(), "院長先生でいらっしゃいますでしょうか？")
        self.assertEqual(Target(kind="個人", name="岡田", reading="おかだ").call(), "「おかだ」様のお電話でお間違えないでしょうか？")
        with self.assertRaises(AssertionError):
            Target()                                    # 名前も役職も無い＝「誰でも」（Target を持たない）

    def test_ai_is_told_about_the_target(self):
        from flowctl import PARTY_LABEL, tool_declarations
        from instruction import build
        b = self.business()
        for mode in ("recorded", "ai"):
            text = build(b, "S2", opening_mode=mode)[1]
            self.assertIn("営業部部長の「おかだ」様", text, mode)
            self.assertIn(f"「{PARTY_LABEL['target']}」", text, mode)
            self.assertNotIn(f"最初の質問 {b.questions[0].id}「", text.split("会話の決まり")[0], mode)  # 名乗りのあとに最初の質問を聞かせない
        d = next(d for d in tool_declarations(b.questions, replay=True, target=True)[0].function_declarations if d.name == "report_party")
        self.assertIn(PARTY_LABEL["target"], d.parameters.properties["相手"].enum)

    def test_resume_while_calling(self):
        from instruction import position_text
        f, b = self.flow()
        self.assertIn("目的の人", position_text(b, f))

    def checked(self):
        f, b = self.flow()
        f.use_checks("judge")
        return f, b

    @staticmethod
    def heard(f, text, judged):
        f.on_callee(); f.on_heard(text)
        if judged is not None:
            f.on_check("judge", judged)

    def test_judge_runs_while_calling(self):
        # 呼んでいる段にも、意味の判定を当てる（呼ぶ一言を判定の「質問」にする）。⛔ 直す前：いまの質問が無いので判定が走らず、
        #    取り次ぎ（「少々お待ちください」）を見分ける独立した確かめが無かった
        from flowctl import TARGET_IS_THERE, TARGET_IS_AWAY
        f, b = self.checked()
        q, asked, before = f.judge_context()
        self.assertEqual((q.text, asked), (b.target.call(), None))
        self.assertEqual(set(q.conditions), {TARGET_IS_THERE, TARGET_IS_AWAY})
        g, _ = self.flow()
        g.on_callee()
        g.apply("report_party", {"party": "target"})
        self.assertEqual(g.judge_context()[0].id, "Q1")                         # 繋がったら、いまの質問

    def test_target_report_is_taken_when_the_judge_agrees(self):
        from flowctl import TARGET_IS_THERE
        f, b = self.checked()
        self.assertTrue(f.wants_checks("report_party", {"相手": "目的の人"}))     # 判定を待ってから返事を作る
        self.heard(f, "はい、岡田は私ですが。", TARGET_IS_THERE)
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"], "next")

    def test_target_report_on_a_hold_is_not_taken(self):
        # AIが「岡田ですね。少々お待ちください。」を目的の人と報告した：判定は取り次ぎ ⇒ 黙って待つ（用件・質問を受付の方に流さない）
        from flowctl import HOLD
        f, b = self.checked()
        self.heard(f, "岡田ですね。少々お待ちください。", HOLD)
        r = f.apply("report_party", {"party": "target"})
        self.assertEqual((r["result"], f.phase, f.passed_on, r.get("silent")), ("hold", "calling", True, True))

    def test_target_report_on_away_is_not_taken(self):
        from flowctl import TARGET_IS_AWAY
        f, b = self.checked()
        self.heard(f, "あいにく岡田は席を外しておりまして。", TARGET_IS_AWAY)
        r = f.apply("report_party", {"party": "target"})
        self.assertEqual((r["result"], f.phase), ("rejected", "calling"))
        self.assertIn("request_callback", r["message"])

    def test_target_report_unsure_is_confirmed_once(self):
        # 判定が決められない・代わった人が話し始めた：目的の人かを1回だけ確かめる（確かめへの返事には、その言い方を添えて判定する）
        from flowctl import HANDOVER, TARGET_IS_THERE, UNDECIDED
        for judged in (HANDOVER, UNDECIDED):
            f, b = self.checked()
            self.heard(f, "はい、お電話代わりました。", judged)
            r = f.apply("report_party", {"party": "target"})
            self.assertEqual((r["result"], [p["text"] for p in r.get("play") or []] or [r.get("say")]), ("confirm_target", [b.target.confirm()]), judged)
            f.on_callee()
            self.assertEqual(f.judge_context()[1], b.target.confirm())          # 確かめた言い方を添えて判定する
            f.on_heard("はい、そうです。"); f.on_check("judge", TARGET_IS_THERE)
            r = f.apply("report_party", {"party": "target"})
            self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["greeting", "intro", "purpose", "question"], judged)

    def test_report_before_the_whole_words_waits(self):
        # ⛔ 2026-10-19 G-57：受付の方の「岡田ですね。」（間1.5秒）「少々お待ちください。」の間で、AIが「目的の人」と報告した
        from types import SimpleNamespace

        from flowctl import HOLD
        fc = SimpleNamespace(name="report_party", args={"相手": "目的の人"})
        # (1) 会話AIが、まだ声の終わりまで聞き取っていない（話し終わりの位置が声の終わりより前）：途中の言葉の判定は使わず、決めずに待つ
        f, b = self.checked()
        f.on_callee(); f.on_voice(3.38); f.on_activity(True, 0.16); f.on_heard("岡田ですね。"); f.on_activity(False, 1.9)
        f.on_check("judge", "決められない", said="岡田ですね。")
        self.assertFalse(f.words_complete)
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"], "wait")
        self.assertEqual(f.phase, "calling")
        # (2) 言葉の途中で届いた報告（midway）で、出揃った言葉の判定が間に合わない：AIの報告のままにせず、決めずに待つ
        f, b = self.checked()
        f.on_callee(); f.on_voice(3.38); f.on_activity(False, 4.4); f.on_heard("岡田ですね。少々お待ちください。")
        self.assertTrue(f.words_complete)
        self.assertEqual(f.handle_call(fc, midway=True)[0]["結果"], "最後まで聞く")
        self.assertEqual(f.phase, "calling")
        # (3) 出揃った言葉の判定がそろえば、それで決める（取り次ぎ ⇒ 黙って待つ）
        f.on_check("judge", HOLD, said="岡田ですね。少々お待ちください。")
        self.assertEqual(f.handle_call(fc, midway=True)[0]["結果"], "取り次ぎを待つ")

    def test_judge_sees_what_the_callee_said_before(self):
        # ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』に
        # なりますよね」⇒ 呼んでいる段では、その前に相手が言ったことを判定に渡す（直前の3つまで）。繋いだあとは渡さない
        from flowctl import TARGET_IS_THERE, TARGET_MEANINGS
        from meaning_judge import BEFORE, KIND_DESC, jev_request, prompt, scene
        f, b = self.checked()
        f.on_callee()
        self.assertIsNone(f.judge_context()[2])
        f.on_heard("岡田ですね。少々お待ちください。")
        f.on_callee(); f.on_heard("はい、お電話代わりました。")
        q, asked, before = f.judge_context()
        self.assertEqual(before, "岡田ですね。少々お待ちください。")
        self.assertIn(f"{BEFORE}：「岡田ですね。少々お待ちください。」", prompt(q, "はい、お電話代わりました。", asked, before))
        self.assertEqual(scene(q, "x", None, before)[BEFORE], before)
        self.assertEqual(jev_request("m", q, "x", None, before)["state"][BEFORE], before)
        self.assertNotIn(BEFORE, prompt(q, "はい、お電話代わりました。"))       # 渡さなければ、これまでと同じ文
        self.assertIn("お呼びした方に取り次いだ", TARGET_MEANINGS[TARGET_IS_THERE])  # 決まりは意味の説明の1か所（代わった人の説明は、そこを指す）
        from flowctl import HANDOVER
        self.assertIn("お呼びした方に取り次いだ", KIND_DESC[HANDOVER])
        f.on_check("judge", TARGET_IS_THERE)
        f.apply("report_party", {"party": "target"})
        f.on_callee(); f.on_heard("はい、予約しています。")
        self.assertIsNone(f.judge_context()[2])

    def test_late_hold_judgment_still_marks_passed_on(self):
        # 受付の方の「少々お待ちください。」の判定が、代わった方が話し始めたあとに届いても、取り次がれたことは残る ⇒ 代わった方に全部を流す
        # ⛔ 2026-10-19 G-62：いまの発話の判定だけで決めていたので、遅れて届くと、あいさつと名乗りを流さずに用件から始めた
        from flowctl import HOLD, TARGET_IS_THERE
        f, b = self.checked()
        self.heard(f, "岡田ですね。少々お待ちください。", None)
        held = f.callee_turns
        self.heard(f, "お電話代わりました。岡田です。", TARGET_IS_THERE)
        f.on_check("judge", HOLD, held, said="岡田ですね。少々お待ちください。")
        self.assertNotEqual(f.meaning_said, HOLD)  # いまの発話の意味は変えない
        r = f.apply("report_party", {"party": "target"})
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]][:3], ["greeting", "intro", "purpose"])

    def test_binding_reports_are_the_target_decisions(self):
        # 判定を全部待つのは、取り消せない報告（目的の人に繋ぐ）だけ。答えの報告は、早く届いたほうで決める
        f, b = self.checked()
        self.assertTrue(f.binding("report_party", {"party": "target"}))
        self.assertTrue(f.binding("replay_opening", {"part": "handover"}))
        self.assertFalse(f.binding("report_party", {"party": "reception"}))
        f.on_callee(); f.on_heard("岡田は私です。"); f.on_check("judge", "ご本人"); f.on_check("judge2", "ご本人")
        f.apply("report_party", {"party": "target"})
        self.assertEqual(f.phase, "asking")
        self.assertFalse(f.binding("record_answer", {"question_id": "Q1", "condition_id": "はい"}))
        self.assertFalse(f.binding("report_party", {"party": "target"}))

    def test_disagreeing_judgments_ask_before_connecting(self):
        # 2つの判定が食い違う（「はい、お電話代わりました。」を、一方は「ご本人」・もう一方は「代わった人」）：繋がずに目的の人かを確かめる
        from flowctl import HANDOVER, TARGET_IS_THERE
        f, b = self.checked()
        f.use_checks("judge", "judge2")
        f.on_callee(); f.on_heard("はい、お電話代わりました。"); f.on_check("judge", TARGET_IS_THERE); f.on_check("judge2", HANDOVER)
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"], "confirm_target")

    def test_no_judge_takes_the_report_but_late_judge_asks_once(self):
        # 判定を使わない通話：AIの報告のまま進む（止まらない）
        from flowctl import HOLD
        f, b = self.flow()
        self.heard(f, "はい、私です。", None)
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"], "next")
        # 判定を使う通話で、判定が届かなかった（待つのは2秒まで）：繋いだら戻せないので、目的の人かを確かめる。認めたら繋ぐ
        # ⛔ 2026-10-19 G-62：報告のまま繋いでいたので、受付の方の「少々お待ちください。」で判定が遅れたとき、受付の方を目的の人として繋いだ
        f, b = self.checked()
        self.heard(f, "岡田ですね。少々お待ちください。", None)
        held = f.callee_turns
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"], "confirm_target")
        self.assertEqual(f.phase, "calling")
        # 判定がまた届かなくても、確かめは上限まで（limits.replays＝1通話2回。止まらない）＝そのあとは繋ぐ。受付の方の言葉の判定（取り次ぎ）が
        # 遅れて届けば、取り次がれた印が付き、代わった方には、あいさつ・名乗り・用件の全部を流す
        self.heard(f, "お電話代わりました。岡田です。", None)
        self.assertEqual(f.apply("report_party", {"party": "target"})["result"], "confirm_target")
        f.on_check("judge", HOLD, held, said="岡田ですね。少々お待ちください。")
        self.heard(f, "はい、そうです。", None)
        r = f.apply("report_party", {"party": "target"})
        self.assertEqual((r["result"], f.phase), ("next", "asking"))
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]][:2], ["greeting", "intro"])
        # 確かめただけでは「取り次がれた」としない：最初に出たご本人に確かめたあと繋いでも、名乗りは流し直さない（⛔ G-62）。
        # 判定が1つも届かないとき、取り次がれたかの手がかりは、取り次ぎの判定と、AIの「受付の方」の報告だけ
        g, _ = self.checked()
        self.heard(g, "はい、岡田は私ですが。", None)
        self.assertEqual(g.apply("report_party", {"party": "target"})["result"], "confirm_target")
        self.heard(g, "はい、そうです。", "ご本人")
        r = g.apply("report_party", {"party": "target"})
        self.assertEqual([p.get("part", p["kind"]) for p in r["play"]], ["purpose", "question"])

    def test_call_harness_judges_with_the_flow_question(self):
        import inspect
        import convo
        src = inspect.getsource(convo.check_handler)
        self.assertIn("q, asked, before = flow.judge_context()", src)
        self.assertNotIn("flow.questions[qid]", src)


class ScenesTest(unittest.TestCase):
    """16-2 の模擬通話の回帰（scenes.py）：場面の台詞は、お客役の台詞の一覧（make_callee_samples）の1か所にあるものだけ。"""

    def test_lines_come_from_the_one_list(self):
        from make_callee_samples import PAUSED, TONES, UTTERANCES
        from scenes import SCENES
        for name, sc in SCENES.items():
            for x in sc.lines:
                uid = x["wav"].removeprefix("Puck_").removesuffix(".wav")
                self.assertIn(uid, set(UTTERANCES) | set(TONES) | set(PAUSED), name)
        self.assertTrue(all(base in UTTERANCES for base, _ in PAUSED.values()))

    def test_widened_pause_keeps_the_voice(self):
        # 間を長くした台詞：文と文の間だけが変わり、声の長さ（間を除く）は同じ
        from barge import voice_runs, voiced
        from make_callee_samples import PAUSED, verified
        from stitch_gap import callee_speech
        for uid, (base, pause_s) in PAUSED.items():
            src, out = f"Sadachbia_{base}.wav", f"Sadachbia_{uid}.wav"
            if not verified(src):
                continue
            self.assertTrue(verified(out))
            (a, ra, _), (b, rb, _) = callee_speech(src), callee_speech(out)
            ra_runs, rb_runs = voice_runs(voiced(a, ra), 0.15), voice_runs(voiced(b, rb), 0.15)
            self.assertEqual(len(ra_runs), len(rb_runs))
            self.assertAlmostEqual(rb_runs[1][0] - rb_runs[0][1], pause_s, delta=0.06)
            voice = lambda runs: sum(y - x for x, y in runs)
            self.assertAlmostEqual(voice(ra_runs), voice(rb_runs), delta=0.06)

    def test_expectations_run_on_an_empty_call(self):
        # 通話が何も起きずに終わっても、数え方が落ちない（どの決まりも「守られなかった」と数える）
        from scenes import SCENES, measures
        empty = {"turns": [], "flow_log": [], "items": {"Q1": {}, "Q2": {}, "Q3": {}}, "facts": {}}
        for name in SCENES:
            m = measures(empty, name)
            self.assertFalse(m["pass"], name)


class JudgeWordsTest(unittest.TestCase):
    """意味の判定に渡す言葉（convo.check_handler）：出揃った相手の言葉＝会話AIの話し終わりの知らせのあと、相手がまた話し始めていない間の
    文字（Flow.words_complete）。⛔ 2026-10-19 G-57：受付の方の「岡田ですね。」「少々お待ちください。」の「岡田ですね。」だけで判定した。"""

    def flow(self):
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("答えた",))])
        f.on_callee()
        return f

    def judge(self, seen, took=0.2):
        import asyncio

        async def judge(q, text, asked=None, before=None):
            seen.append(text)
            await asyncio.sleep(took)
            return "取り次ぎ" if "お待ち" in text else "決められない"
        return judge

    def drive(self, steps, report_at=None, pcm=b"\x00\x00", took=0.2, hold_s=None):
        """steps：(秒, 'start'|'end'|文字) を時刻順に流す。report_at：その秒に報告（record_answer）を受ける。"""
        import asyncio
        import unittest.mock as m
        from types import SimpleNamespace

        from convo import check_handler
        f, seen, out = self.flow(), [], {}
        if hold_s is not None:
            import dataclasses
            f.limits = dataclasses.replace(f.limits, hold_s=hold_s)

        async def main():
            pending = []
            handle, _, info, _ = check_handler(f, judge=self.judge(seen, took), pcm=pcm, rate=16000, wav="x.wav", tasks=pending,
                                               audio_at=None if pcm is None else 1.0)  # 声の終わり＝送った音の上で約1.0秒
            t0 = asyncio.get_running_loop().time()

            async def feed():
                for at, x in steps:
                    await asyncio.sleep(max(0.0, t0 + at - asyncio.get_running_loop().time()))
                    # end＝最後の声のあとの話し終わり（位置2.0秒）・mid＝文と文の間の話し終わり（声の終わりより前の位置0.5秒）
                    (f.on_activity(True, 0.1) if x == "start" else f.on_activity(False, 2.0) if x == "end"
                     else f.on_activity(False, 0.5) if x == "mid" else f.on_heard(x))
            feeding = asyncio.create_task(feed())
            if report_at is not None:
                await asyncio.sleep(report_at)
                f.wants_checks = lambda name, args=None: True
                out["check_at_report"] = None
                handle_call = f.handle_call
                f.handle_call = lambda fc, midway=False: (out.update(check_at_report=dict(f.check), said_at_report=f.callee_said, midway=midway)
                                                          or handle_call(fc, midway))
                await handle(SimpleNamespace(name="record_answer", args={"質問": "Q1", "条件": "答えた"}))
            await feeding
            await asyncio.wait(pending, timeout=3)
            out["info"] = info
        with m.patch("stitch_gap.callee_speech", return_value=(b"", 16000, 0.0)):
            asyncio.run(main())
        return f, seen, out

    def test_judges_the_words_after_the_speaker_finished(self):
        f, seen, _ = self.drive([(0.0, "start"), (0.05, "岡田ですね。少々お待ちください。"), (0.05, "end")])
        self.assertEqual(seen, ["岡田ですね。少々お待ちください。"])
        self.assertEqual((f.check.get("judge"), f.check_said.get("judge")), ("取り次ぎ", "岡田ですね。少々お待ちください。"))

    def test_does_not_judge_before_the_speaker_finished(self):
        # 文字は届いたが、話し終わりの知らせがまだ（相手は話している）：判定しない
        f, seen, _ = self.drive([(0.0, "start"), (0.05, "岡田ですね。")])
        self.assertEqual(seen, [])
        self.assertNotIn("judge", f.check)

    def test_speaker_went_on_while_judging(self):
        # 「岡田ですね。」で話し終わりの知らせ ⇒ 判定の間に、また話し始めて「少々お待ちください。」：前の判定は渡さず、出揃った言葉で判定し直す
        f, seen, out = self.drive([(0.0, "start"), (0.02, "岡田ですね。"), (0.02, "end"), (0.1, "start"), (0.3, "少々お待ちください。"),
                                 (0.3, "end")])
        self.assertEqual(seen, ["岡田ですね。", "岡田ですね。少々お待ちください。"])
        self.assertEqual(out["info"]["judge_stale"], ["岡田ですね。"])
        self.assertEqual(f.check.get("judge"), "取り次ぎ")

    def test_report_while_the_speaker_is_talking_waits_for_the_whole_words(self):
        # AIが言葉の間で報告した（G-57）：相手が話している間は報告を待たせ、出揃った言葉の判定がそろってから受け取る
        f, seen, out = self.drive([(0.0, "start"), (0.02, "岡田ですね。"), (0.02, "end"), (0.08, "start"), (0.4, "少々お待ちください。"),
                                 (0.4, "end")], report_at=0.1)
        self.assertEqual(out["said_at_report"], "岡田ですね。少々お待ちください。")
        self.assertEqual(out["check_at_report"].get("judge"), "取り次ぎ")
        self.assertGreater(out["info"]["waits"][0]["speaking_s"], 0.2)

    def test_words_grew_after_the_judgment_is_judged_again_at_the_report(self):
        # 判定を渡したあとに、相手がまた話した：前の言葉の判定は外れ（Flow.on_heard）、報告のときに、いまの言葉で判定し直す
        f, seen, out = self.drive([(0.0, "start"), (0.02, "岡田ですね。"), (0.02, "end"), (0.4, "start"), (0.45, "少々お待ちください。"),
                                 (0.45, "end")], report_at=0.6, took=0.1)
        self.assertEqual(seen, ["岡田ですね。", "岡田ですね。少々お待ちください。"])
        self.assertEqual(out["info"]["rejudged"], ["judge"])
        self.assertEqual(out["check_at_report"].get("judge"), "取り次ぎ")

    def test_end_between_sentences_is_not_the_end(self):
        # 文と文の間の話し終わり（位置が声の終わりより前）：まだ出揃っていない。最後の声のあとの話し終わりで、全部の言葉を判定する
        f, seen, out = self.drive([(0.0, "start"), (0.02, "岡田ですね。"), (0.02, "mid"), (0.3, "start"), (0.5, "少々お待ちください。"),
                                   (0.5, "end")], report_at=0.1)
        self.assertEqual(seen, ["岡田ですね。少々お待ちください。"])
        self.assertTrue(out["midway"])
        self.assertEqual(out["check_at_report"].get("judge"), "取り次ぎ")

    def test_lagging_service_does_not_judge_the_part(self):
        # 会話AIが遅れ、文と文の間の話し終わりのあと何も来ない：途中の言葉では判定しない（報告は midway のまま、流れの制御が決めずに待つ）
        f, seen, out = self.drive([(0.0, "start"), (0.02, "岡田ですね。"), (0.02, "mid")], report_at=0.1, hold_s=0.3)
        self.assertEqual(seen, [])
        self.assertTrue(out["info"]["incomplete"])
        self.assertTrue(out["midway"])
        self.assertNotIn("judge", out["check_at_report"])

    def test_text_line_is_complete_at_once(self):
        # 文字で送る相手の言葉は、はじめから出揃っている（話し終わりの知らせは来ない）
        f, seen, _ = self.drive([(0.0, "岡田ですね。")], pcm=None)
        self.assertEqual(seen, ["岡田ですね。"])

    def test_no_activity_at_the_report_judges_what_is_there(self):
        # 話し始め・話し終わりの知らせが来ないつなぎ：報告のときに、いまある言葉で判定する（止まらない）
        f, seen, out = self.drive([(0.0, "岡田ですね。")], report_at=0.05)
        self.assertTrue(out["info"]["no_activity"])
        self.assertEqual(out["check_at_report"].get("judge"), "決められない")


class CurrentWordsCheckTest(unittest.TestCase):
    """流れの制御は、いまの相手の言葉の判定だけを使う（Flow.on_check の said・Flow.on_heard）。"""

    def flow(self):
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("答えた",))])
        f.on_callee()
        return f

    def test_old_words_judgment_is_not_used(self):
        f = self.flow()
        f.on_heard("岡田ですね。")
        f.on_heard("少々お待ちください。")
        f.on_check("judge", "決められない", said="岡田ですね。")
        self.assertNotIn("judge", f.check)

    def test_words_growing_drops_the_judgment(self):
        f = self.flow()
        f.on_heard("岡田ですね。")
        f.on_check("judge", "決められない", said="岡田ですね。")
        f.on_check("ear", "岡田ですね。少々お待ちください。")
        self.assertEqual(f.meaning_said, "決められない")
        f.on_heard("少々お待ちください。")
        self.assertIsNone(f.meaning_said)
        self.assertEqual(f.ear_said, "岡田ですね。少々お待ちください。")   # 別の耳は、発話まるごと（外さない）

    def test_words_complete_follows_the_speech_marks(self):
        f = self.flow()
        self.assertTrue(f.words_complete)        # 声を見張らない（文字で送った言葉）：届いた言葉が全部
        f.on_voice(3.38)                         # 相手の声は、送った音の上で 3.38秒まで
        self.assertFalse(f.words_complete)
        f.on_activity(True, 0.16)
        self.assertFalse(f.words_complete)
        f.on_activity(False, 1.9)                # 文と文の間の話し終わり（位置が声の終わりより前）
        self.assertFalse(f.words_complete)
        f.on_activity(True, 2.5)
        f.on_activity(False, 4.4)                # 最後の声のあとの話し終わり
        self.assertTrue(f.words_complete)
        f.on_activity(True, 5.0)                 # また話し始めた
        self.assertFalse(f.words_complete)
        f.on_callee()                            # 次の発話：声の終わりの位置は、電話接続側が渡し直す
        f.on_voice(9.0)
        f.on_activity(False, 8.0)
        self.assertFalse(f.words_complete)
        f.on_activity(False, 10.1)
        self.assertTrue(f.words_complete)
        f.on_callee()
        f.on_voice(12.0)
        f.on_voice(None)                         # 知らせの来ないつなぎだと決めた：届いた言葉で決める
        self.assertTrue(f.words_complete)


class InboxTest(unittest.TestCase):
    """会話AIからの知らせの受け取り（live_util.Inbox）：報告の返事を待たせている間も、相手の言葉の文字と話し始め・話し終わりの知らせを、
    届いた瞬間に渡す。⛔ 2026-10-19 G-57：受け取りを止めていたので、「少々お待ちください。」を返事を決めるまで読めなかった。"""

    def test_words_arrive_while_a_report_is_held(self):
        import asyncio
        from types import SimpleNamespace

        from live_util import collect_turn

        def msg(**kw):
            base = dict(usage_metadata=None, go_away=None, session_resumption_update=None, tool_call=None, server_content=None,
                        tool_call_cancellation=None, voice_activity=None)
            base.update(kw)
            return SimpleNamespace(**base)
        fc = SimpleNamespace(name="record_answer", args={}, id="1")
        heard = lambda t: msg(server_content=SimpleNamespace(interrupted=False, input_transcription=SimpleNamespace(text=t, finished=None),
                                                             output_transcription=None, model_turn=None, turn_complete=False))
        end = msg(voice_activity=SimpleNamespace(voice_activity_type="VoiceActivityType.ACTIVITY_END", audio_offset="3.5s"))
        seen, marks = [], []

        class Session:
            def __init__(self):
                self.sent, self.calls = [], 0

            async def send_tool_response(self, function_responses):
                self.sent.append(function_responses)

            async def receive(self):
                self.calls += 1
                if self.calls > 1:
                    await asyncio.sleep(3600)
                yield heard("岡田ですね。")
                yield msg(tool_call=SimpleNamespace(function_calls=[fc]))
                await asyncio.sleep(0.05)
                yield heard("少々お待ちください。")
                yield end

        async def held(fc):
            while "".join(seen) != "岡田ですね。少々お待ちください。" or not marks:  # 返事は、続きの文字と話し終わりが届いてから
                await asyncio.sleep(0.01)
            return {"result": "ok"}, True  # 話させない返事（AIの声を待たない）

        async def main():
            s = Session()
            from live_util import inbox_of
            inbox_of(s).on_activity = lambda speaking, at=None: marks.append(speaking)
            r = await collect_turn(s, started_at=asyncio.get_running_loop().time() * 0 + __import__("time").monotonic(),
                                   on_tool_call=held, on_heard=seen.append, timeout_s=3)
            return r, s
        r, s = asyncio.run(main())
        self.assertFalse(r.timed_out)
        self.assertEqual((seen, marks, len(s.sent)), (["岡田ですね。", "少々お待ちください。"], [False], 1))
        at = {e["kind"]: e["at_s"] for e in r.events}                       # 待たせている間に届いた知らせも、届いた時刻で記録する
        self.assertEqual([e["text"] for e in r.events if e["kind"] == "heard"], ["岡田ですね。", "少々お待ちください。"])
        self.assertGreaterEqual(at["tool_response_sent"], at["activity"])

    def test_replaced_connection_stops_passing_words(self):
        # 張り替えた古いつなぎは、閉じ終わるまで知らせが届き続ける：流れの制御へは渡さない（いまの相手の発話の言葉に混ざる）
        import asyncio
        from types import SimpleNamespace

        from live_util import inbox_of, open_session
        heard, marks = [], []

        class Session:
            async def receive(self):
                await asyncio.sleep(0.1)
                yield SimpleNamespace(server_content=SimpleNamespace(input_transcription=SimpleNamespace(text="古い言葉")),
                                      voice_activity=None)
                await asyncio.sleep(3600)

        class Connect:
            async def __aenter__(self):
                return Session()

            async def __aexit__(self, *exc):
                await asyncio.sleep(0.3)  # 閉じるのに時間がかかる
                return False

        async def main():
            closing = []
            client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **kw: Connect())))
            async with open_session(client, "m", None, closing) as s:
                inbox_of(s).on_heard, inbox_of(s).on_activity = heard.append, marks.append
            await asyncio.sleep(0.2)
            await asyncio.gather(*closing)
        asyncio.run(main())
        self.assertEqual(heard, [])

    def test_speech_while_a_report_is_held_is_not_played(self):
        # 報告の返事を待たせている間にAIが話し始めた声は、相手に流さない（その声は、返事を知らずに作られている）
        import asyncio
        import time
        from types import SimpleNamespace

        from live_util import collect_turn

        def msg(**kw):
            base = dict(usage_metadata=None, go_away=None, session_resumption_update=None, tool_call=None, server_content=None,
                        tool_call_cancellation=None, voice_activity=None)
            base.update(kw)
            return SimpleNamespace(**base)
        fc = SimpleNamespace(name="report_party", args={}, id="1")
        blob = SimpleNamespace(data=b"\x00\x01" * 2400, mime_type="audio/pcm;rate=24000")
        speech = msg(server_content=SimpleNamespace(interrupted=False, input_transcription=None, turn_complete=False,
                                                    output_transcription=SimpleNamespace(text="かしこまりました。"),
                                                    model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=blob)])))

        class Session:
            def __init__(self):
                self.sent, self.calls = [], 0

            async def send_tool_response(self, function_responses):
                self.sent.append(function_responses)

            async def receive(self):
                self.calls += 1
                if self.calls > 1:
                    await asyncio.sleep(3600)
                yield msg(tool_call=SimpleNamespace(function_calls=[fc]))
                yield speech

        async def held(fc):
            await asyncio.sleep(0.2)
            return {"result": "hold"}, True

        r = asyncio.run(collect_turn(Session(), started_at=time.monotonic(), on_tool_call=held, timeout_s=2))
        self.assertEqual((len(r.audio), r.first_audio_after_s, "".join(r.output_transcript)), (0, None, ""))
        self.assertEqual("".join(r.held_text), "かしこまりました。")
        self.assertGreater(r.held_audio_s, 0.09)

    def test_withdrawn_report_is_not_applied(self):
        # 会話AIが報告を取り消した（相手が話し続けた）：まだ返事を作っている報告は、流れの制御へ渡さず、返事も送らない
        import asyncio
        import time
        from types import SimpleNamespace

        from live_util import collect_turn

        def msg(**kw):
            base = dict(usage_metadata=None, go_away=None, session_resumption_update=None, tool_call=None, server_content=None,
                        tool_call_cancellation=None, voice_activity=None)
            base.update(kw)
            return SimpleNamespace(**base)
        fc = SimpleNamespace(name="report_party", args={"相手": "目的の人"}, id="7")
        applied = []

        class Session:
            def __init__(self):
                self.sent, self.calls = [], 0

            async def send_tool_response(self, function_responses):
                self.sent.append(function_responses)

            async def receive(self):
                self.calls += 1
                if self.calls > 1:
                    await asyncio.sleep(3600)
                yield msg(tool_call=SimpleNamespace(function_calls=[fc]))
                await asyncio.sleep(0.05)
                yield msg(tool_call_cancellation=SimpleNamespace(ids=["7"]))

        async def held(fc):
            await asyncio.sleep(1.0)            # 確かめを待っている間に取り消される
            applied.append(fc.name)
            return {"result": "ok"}, True

        s = Session()
        r = asyncio.run(collect_turn(s, started_at=time.monotonic(), on_tool_call=held, timeout_s=3))
        self.assertEqual((applied, s.sent, r.timed_out), ([], [], False))
        self.assertIn("tool_call_cancelled", [e["kind"] for e in r.events])


class SilentAfterReplyTest(unittest.TestCase):
    """AIが黙った（DESIGN.md 6-7）の見張り：話させる形の返事を送ったのに声が来ないときも、黙ったと見なす。
    ⛔ G-57 の3回目：言い直しを求める返事のあと、AIが声を出さずに英語の文字だけを返し、操作があったので黙ったと見なさずに90秒待った。"""

    def run_turn(self, messages, silent_reply: bool):
        import asyncio
        from types import SimpleNamespace
        from live_util import collect_turn

        class Session:
            def __init__(self):
                self.sent = []

            async def send_tool_response(self, function_responses):
                self.sent.append(function_responses)

            async def receive(self):
                for m in messages:
                    yield m
                await asyncio.sleep(3600)

        fc = SimpleNamespace(name="record_answer", args={}, id="1")
        msgs_tool = SimpleNamespace(tool_call=SimpleNamespace(function_calls=[fc]), server_content=None, usage_metadata=None,
                                    go_away=None, session_resumption_update=None)
        messages.insert(0, msgs_tool)
        s = Session()
        return asyncio.run(collect_turn(s, started_at=0.0, on_tool_call=lambda f: ({"result": "x"}, silent_reply), timeout_s=5,
                                        silent_after_s=0.3))

    def test_reply_that_wants_speech_but_none_comes(self):
        r = self.run_turn([], silent_reply=False)
        self.assertTrue(r.silent)

    def test_silent_reply_is_not_silence(self):
        r = self.run_turn([], silent_reply=True)       # 録音を流した：AIが黙るのは正しい
        self.assertFalse(r.silent)


class OffScriptTest(unittest.TestCase):
    """AIの声が、渡した言い方から外れたとき（DESIGN.md 6-7）：見分けるのは1か所（offscript.off_script）。止めて、お詫びの録音のあと
    「改めまして、…」と言い直させる（✅ 2026-10-19 ユーザー：「違和感のない形にしないと、AIや機械だと勘繰られますよ」）。
    ⛔ G-57 の3回目：言い直しのはずが、用件と関係の無い話「これらを踏まえて、実際のプレゼン資料の構成や…」を12秒した。"""
    SAY = "ありがとうございます。夕方の5時頃でよろしいでしょうか？"

    def test_what_counts_as_off_script(self):
        from offscript import off_script
        for ok in ("ありがとうございます。夕方の5時頃でよろしいでしょうか？", "承知いたしました。夕方の五時頃でよろしいでしょうか",
                   "夕方の5時頃ですね", "承知いたしま", "ありがとうございます。夕", ""):
            self.assertFalse(off_script(self.SAY, ok), ok)          # 受けの言葉の違い・漢数字・言い回し・まだ途中は外れにしない
        for bad in ("承知いたしました。では、これらを踏まえて、実際の", "</li>\n</ul>\n<p><strong>2. Prepare", "Okay, I will now summarize"):
            self.assertTrue(off_script(self.SAY, bad), bad)

    def test_recorded_g57_turns(self):
        # G-57 の1〜4回目の、言い方を渡したターン：外れと見るのは、本当に外れた2つだけ（文字が1字ずつ届いても、早まって外れにしない）
        import glob
        import json
        from offscript import off_script
        flagged, n = [], 0
        for p in sorted(glob.glob(str(Path(__file__).parent / "results" / "G-57-[0-9]*_*.json"))):
            for r in json.loads(Path(p).read_text(encoding="utf-8"))["rows"]:
                for t in r.get("turns", []):
                    says = [e["say"] for e in t.get("tool_events") or [] if e.get("say")]
                    sp = t.get("intended") or ""
                    if says and sp:
                        n += 1
                        if any(off_script(says[-1], sp[:k]) for k in range(1, len(sp) + 1)):
                            flagged.append(sp[:20])
        self.assertGreaterEqual(n, 150)
        self.assertEqual(len(flagged), 1)                       # 声の無い英語の文字（同じターンで渡した言い方の代わりに返した）
        self.assertTrue(flagged[0].startswith("</li>"))

    def test_gate_waits_at_the_first_pause(self):
        # 決まるまでは、最初の文の切れ目（受けの言葉の文のあと）までだけ流す。止めた時点で聞こえていたものを、流れた声と最初の文で決める
        from offscript import SpeechGate
        rate = 16000
        tone = (b"\x00\x40" * (rate // 2))                         # 声の代わり（0.5秒）
        pcm = tone * 2 + bytes(int(0.3 * rate) * 2) + tone * 2      # 1.0秒の声・0.3秒の間・1.0秒の声
        g = SpeechGate(lambda: self.SAY)
        g.on_audio(0.0)
        self.assertIsNone(g.on_text("承知いたしました。", 0.0))
        self.assertEqual(g.on_text("では、これらを踏まえて、実際の", 2.5), "off")
        self.assertEqual(g.heard(2.5, pcm, rate)["kind"], "opener")  # 2.5秒たっていても、切れ目（1.0秒）より先は流していない
        self.assertAlmostEqual(g.heard(2.5, pcm, rate)["seconds"], 1.0, delta=0.06)
        self.assertEqual(g.heard(3.5, pcm, rate)["kind"], "content")  # 待つのは2秒まで（そのあとは流した）
        g = SpeechGate(lambda: self.SAY)
        g.on_audio(0.0)
        g.on_text("承知いたしました。では、これらを踏まえて、実際の", 1.6)  # 切れ目（1.0秒）までに文字が届いていない ⇒ 待たずに流していた
        self.assertEqual(g.heard(1.6, pcm, rate)["kind"], "content")
        g = SpeechGate(lambda: self.SAY)
        g.on_audio(0.0)
        g.on_text("これらを踏まえて、実際のプレゼン資料の")              # 受けの言葉が無く、最初の文から外れている
        self.assertEqual(g.heard(0.5, pcm, rate)["kind"], "content")
        self.assertEqual(g.heard(0.05, pcm, rate)["kind"], "none")
        g = SpeechGate(lambda: None)
        self.assertIsNone(g.on_text("何でも"))                       # 渡した言い方の無いターンは見張らない

    def test_restate_starts_with_the_apology(self):
        from offscript import restate
        self.assertEqual(restate(self.SAY), "失礼いたしました。改めまして、夕方の5時頃でよろしいでしょうか？")
        self.assertEqual(restate("かしこまりました。3人で大丈夫でしょうか？"), "失礼いたしました。改めまして、3人で大丈夫でしょうか？")

    def test_flow_plays_the_apology_and_keeps_the_position(self):
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("答えた",)), Question("Q2", "人数は。", "number", conditions=("答えた",))])
        f.on_callee()
        r = f.apply("record_answer", {"question_id": "Q1", "condition_id": "答えた", "value": "15日の午後2時"})
        start = len(f.log) - 1
        self.assertEqual(f.expected_say(start), r["say"])
        self.assertIsNone(f.expected_say(len(f.log)))           # このターンに渡した言い方が無ければ見張らない（自由に答えるターン）
        plan = f.on_off_script(r["say"])
        self.assertEqual((plan["ack"], plan["say"]), (None, "失礼いたしました。改めまして、15日の午後2時でよろしいでしょうか？"))  # お詫びは録音にしない
        self.assertEqual(f.on_off_script(r["say"], heard="opener")["say"], "それでは、15日の午後2時でよろしいでしょうか？")
        self.assertEqual(f.on_off_script(r["say"], heard="none")["say"], r["say"])
        self.assertEqual(f.awaiting_confirm, "Q1")               # 位置は変えない（言う中身は同じ）

    def test_collector_stops_when_off_script(self):
        import asyncio
        from types import SimpleNamespace
        from live_util import collect_turn

        def said(text):
            return SimpleNamespace(tool_call=None, usage_metadata=None, go_away=None, session_resumption_update=None,
                                   server_content=SimpleNamespace(interrupted=False, input_transcription=None, model_turn=None,
                                                                  output_transcription=SimpleNamespace(text=text)))

        class Session:
            async def receive(self):
                for m in (said("承知いたしました。"), said("では、これらを"), said("踏まえて、実際の"), said("プレゼン資料の")):
                    yield m
                await asyncio.sleep(3600)

        from offscript import SpeechGate
        r = asyncio.run(collect_turn(Session(), started_at=0.0, timeout_s=5, gate=SpeechGate(lambda: self.SAY)))
        self.assertTrue(r.off_script)
        self.assertEqual(r.heard["kind"], "none")                    # 声はまだ届いていない＝相手には何も聞こえていない
        self.assertEqual("".join(r.output_transcript), "承知いたしました。では、これらを踏まえて、実際の")  # そこで受け取りをやめた
        self.assertEqual([e["kind"] for e in r.events if e["kind"] == "off_script"], ["off_script"])

    def test_call_loop_uses_the_one_watch(self):
        import inspect
        import convo
        src = inspect.getsource(convo.run_call)
        self.assertEqual(src.count("gate=watch(log_start, i)"), 2)          # 声で送る相手・文字で送る相手
        self.assertEqual(src.count("gate=watch(turn_log_start, i)"), 1)     # 続きを待つ間（言い方は、相手の発話への返事で渡したもの）
        self.assertEqual(src.count("stop_off_script("), 3)                 # 止めたあとは1か所（定義と、呼ぶ所2つ）
        self.assertIn("flow.on_off_script(", src)


class G53LabelsTest(unittest.TestCase):
    """G-53 の数え方：付け直した正解（g53_labels.json）を1か所（g53_meaning.accept）で使う。"""

    def test_relabel_wins_and_undecided_equals_unknown(self):
        from g53_meaning import accept, misread_as_affirmed
        # ユーザー：「メールのほうが助かりますが…」は決められないではない＝言い直したメールを認めた（作らせた正解は決められない）
        it = {"set": "D", "qid": "G17", "text": "メールのほうが助かりますが…", "asked": "念のためご連絡はメールでよろしいでしょうか？",
              "truth": "決められない", "kind": "迷う", "asked_choice": "メール", "j": "メール"}
        self.assertEqual(accept(it), ["メール"])
        self.assertFalse(misread_as_affirmed(it, "j"))
        # 付け直していない答え：「決められない」と「わからない」は同じに数える（どちらでも値は渡らない）
        plain = {"set": "D", "qid": "G1", "text": "（付け直していない答え）", "asked": "x", "truth": "unknown"}
        self.assertEqual(set(accept(plain)), {"決められない", "unknown"})


if __name__ == "__main__":
    unittest.main()
