"""接続せずに確かめられる部分の試験（python -m unittest test_offline）。

- .env の読み方（書き方の違いを吸収できるか）
- キーの値が、画面・結果のファイルに出ないか（DESIGN.md I-10。わざと漏らそうとして、伏せられるか）
- 試験用の流れの制御（g06_flow.MiniFlow）が、決まりどおりに進む・止まるか（I-18・I-20 の入口）
"""
from __future__ import annotations

import json
import tempfile
import unittest
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
        decls = {d.name: d for d in tool_declarations(self.business().questions)[0].function_declarations}
        self.assertEqual(set(decls), set(OP_ARGS))
        self.assertFalse({n for n, d in decls.items() if d.behavior})  # 返事を待たない宣言は使わない（G-33・G-36 の形 B は外した）
        for name, (req, opt) in OP_ARGS.items():
            self.assertEqual(set(decls[name].parameters.properties), {ARG_LABEL[k] for k in req + opt}, name)
            self.assertEqual(decls[name].parameters.required, [ARG_LABEL[k] for k in req], name)

    def test_no_english_shown_to_ai(self):
        import json
        import re

        from flowctl import Flow, tool_declarations
        from instruction import build
        b = self.business()
        allowed = {"record_answer", "report_unclear", "confirm_field", "request_end", "Q1", "Q2"}  # 操作の名前（道具の決まり）と質問の番号
        shown = [build(b, t)[1] for t in ("S2", "L4")]
        shown += [build(b, "S2", opening_mode="recorded")[1]]
        for d in tool_declarations(b.questions)[0].function_declarations:
            shown.append(json.dumps(d.model_dump(exclude_none=True), ensure_ascii=False))
        f = Flow(b.questions)
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
        self.assertEqual((f.log[-1]["reply"]["result"], f.log[-1]["reply"]["play"]), ("end", [{"kind": "closing", "text": CLOSING}]))

    def test_lead_is_fixed_by_question_order(self):
        """つなぎの言葉は質問の順番で決まる（依頼主ごとの録音は、質問1つにつき1本のまま）。最初の質問は名乗りと一緒なので付けない。"""
        from flowctl import Question, spoken_question
        qs = [Question(f"Q{i}", f"質問{i}ですか？", "free_text") for i in range(1, 6)]
        self.assertEqual([spoken_question(qs, q.id)[1] for q in qs], ["", "それでは", "続きまして", "では", "それでは"])
        self.assertEqual(spoken_question(qs, "Q3")[0], "続きまして質問3ですか？")

    def test_declined_call_is_not_played(self):
        """通話を断られたときの締めは、これまでどおり（録音の締めは、質問が全部終わったときだけ）。"""
        f = self.flow()
        f.on_callee()
        out, silent = f.handle_call(fc("request_end", 理由="通話を断られた"))
        self.assertFalse(silent)
        self.assertIsNone(f.log[-1]["reply"].get("play"))

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


if __name__ == "__main__":
    unittest.main()
