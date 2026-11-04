"""会話の処理が書き込む入口の試験（core/conversation_io.py。2026-10-24 ユーザー：入口は「CL-2」。DESIGN.md 4-4・7-1）。"""

from __future__ import annotations

import unittest

from salescall.core import attempts, business_rules, call_report, conversation_io, dnc, gate, settings
from salescall.core.access import Rejected
from salescall.core.conversation_io import ItemInvalid, ItemValue
from tests.support import CLEAN, OPERATOR, SYSTEM
from tests.test_gate import FakeDriver, dial_exit
from tests.test_settle import SettleBase, rep


class Write(SettleBase):
    def answered(self, s, path=("ringing", "answered", "in_conversation")):
        """本物の門・出口を通して1回かけ、path まで進める（通話の途中：項目・報告・検証はまだ）。"""
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in path:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        return c.attempt_id

    def write(self, s, aid, item):
        return self.tx(s, lambda tx: conversation_io.write_item(tx, aid, item))

    def items(self, s):
        return self.tx(s, lambda tx: tx.all(
            "SELECT question_id, state::text AS state, value, condition, reason, hedged, attempt_id FROM salescall.item_values"
            " WHERE task_id = %s", (s["task"],)))

    def test_the_conversation_writes_through_the_entry_and_settle_reads_it(self):
        """通話の途中で答えを書き（書き換えもできる）、報告を置き、締めがそれを読んで「完了」にする（入口だけを通す1本）。"""
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "heard", {"text": "来週の火曜"}, "答えた"))
        self.write(s, aid, ItemValue("Q1", "confirmed", {"text": "来週の火曜の午後"}, "答えた"))
        got = self.items(s)
        self.assertEqual([(r["question_id"], r["state"], r["value"], r["condition"], r["attempt_id"]) for r in got],
                         [("Q1", "confirmed", {"text": "来週の火曜の午後"}, "答えた", aid)])
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, aid, st))
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, rep().to_json()))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.assertEqual(self.settle(s, aid)["state"], "completed")

    def test_wrong_items_are_refused(self):
        """書けない答えは断る（別の版にしかない質問・決まっていない状態・返りえない選択肢・状態に合わない理由・決まっていない理由・
        空の言い切っていない言葉）。理由の無い「わからない」は受け取る（判定が理由の抜けとして修正依頼を出す。答えは失わない）。"""
        s = self.ready()
        aid = self.answered(s)
        for item in (ItemValue("Q9", "confirmed", None, "答えた"),
                     ItemValue("Q1", "maybe", None, "答えた"),
                     ItemValue("Q1", "confirmed", None, "はい"),
                     ItemValue("Q1", "confirmed", None, "答えた", reason="undecided"),
                     ItemValue("Q1", "dont_know", None, "わからない", reason="policy"),
                     ItemValue("Q1", "refused", None, "答えたくない", reason="busy"),
                     ItemValue("Q1", "confirmed", None, "答えた", hedged="  ")):
            with self.subTest(item=item), self.assertRaises(ItemInvalid):
                self.write(s, aid, item)
        self.assertEqual(self.items(s), [])
        self.write(s, aid, ItemValue("Q1", "dont_know", None, "わからない"))
        self.write(s, aid, ItemValue("Q1", "refused", None, "答えたくない", reason="unwilling", hedged="ちょっとそれは"))
        self.assertEqual([(r["state"], r["reason"], r["hedged"]) for r in self.items(s)], [("refused", "unwilling", "ちょっとそれは")])

    def test_only_an_answered_call_before_its_verification_is_done(self):
        """お客様が出ていない通話・通話後の検証が済んだ通話には書かない（済むと締めが読むので、そのあとに書き換えない）。"""
        s = self.ready()
        aid = self.answered(s, path=("ringing", "no_answer"))
        with self.assertRaises(ItemInvalid):
            self.write(s, aid, ItemValue("Q1", "confirmed", None, "答えた"))
        t = self.ready()
        bid = self.call(t, items=[], report=rep())    # 出て話して終わり、検証も済んだ
        with self.assertRaises(ItemInvalid):
            self.write(t, bid, ItemValue("Q1", "confirmed", None, "答えた"))

    def test_only_the_newest_call_of_the_row_writes(self):
        """答えは、その行のいちばん新しい通話の番号でだけ書く（前の通話の番号では、データベースが断る＝0108）。前の答えは残る。"""
        s = self.ready()
        first = self.answered(s)
        self.write(s, first, ItemValue("Q1", "dont_know", None, "わからない"))
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, first, st))
        second = self.w.insert_claim(s)                    # 同じ行の次の通話（門の間隔は、ここでは試さない）
        for st in ("dialing", "ringing", "answered"):
            self.w.move(s, second, st)
        with self.assertRaises(Rejected) as e:
            self.write(s, first, ItemValue("Q1", "dont_know", None, "わからない", reason="undecided"))
        self.assertEqual(e.exception.constraint, "item_values_from_newest_call")
        self.write(s, second, ItemValue("Q1", "confirmed", None, "答えた"))
        kept = self.tx(s, lambda tx: tx.all("SELECT attempt_id, state::text AS state FROM salescall.item_value_history"
                                            " WHERE task_id = %s", (s["task"],)))
        self.assertEqual([(r["attempt_id"], r["state"]) for r in kept], [(first, "dont_know")])

    def test_reports_and_declarations_go_through_the_same_one_place(self):
        """報告の形の確かめは call_report.from_json の1か所（合わない報告は断る）。「もうかけないで」は dnc.register_declared の
        1か所（その場で台帳へ。何度呼んでも同じ）。"""
        s = self.ready()
        aid = self.answered(s)
        with self.assertRaises(call_report.ReportInvalid):
            self.tx(s, lambda tx: conversation_io.save_report(tx, aid, {"version": 3, "unknown_key": 1}))
        first = self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        again = self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        self.assertEqual(first["entry_id"], again["entry_id"])
        self.assertTrue(self.tx(s, lambda tx: dnc.refused_in(tx, aid, "refused_in_call")))


class Materials(SettleBase):
    """会話の処理に渡す材料と、通話の途中の知らせ（2026-10-24 CL-1 の連絡帳 4-34・4-37・4-41。どれも今ある正本を読む・書くだけ）。"""
    answered = Write.answered
    write = Write.write

    def test_call_settings_are_read_in_the_scope_of_the_call(self):
        """会話の決まりの設定は、その通話の業務の値（受付の保留を待つ長さ call.hold_wait＝初め300秒。2026-10-24 ユーザー：「業務ごとに
        選べる：デフォルト…5分、理由があればそれまで待つ」）。会話の決まりでない設定は読ませない。"""
        s = self.ready()
        aid = self.answered(s)
        self.assertEqual(self.tx(s, lambda tx: conversation_io.call_setting(tx, aid, "call.hold_wait")), 300)
        self.tx(s, lambda tx: settings.set_value(
            tx, "call.hold_wait", 600, scope="business", tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"],
            status="decided", rationale="試験：この業務は長く待つ", review_when="試験"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: conversation_io.call_setting(tx, aid, "call.hold_wait")), 600)
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: conversation_io.call_setting(tx, aid, "dial.failed_retry_days"))

    def test_earlier_answers_and_the_handlings_of_a_question(self):
        """前の通話で取れた答え（続きから聞く電話で、取れた質問を伺わない）と、質問ごとの「理由×扱い」（業務の決まりの1か所）。"""
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "confirmed", {"text": "来週の火曜"}, "答えた"))
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"])),
                         {"Q1": {"state": "confirmed", "value": {"text": "来週の火曜"}, "condition": "答えた"}})
        got = self.tx(s, lambda tx: conversation_io.handlings(tx, s["version"], "Q1"))
        rules = self.tx(s, lambda tx: business_rules.load(tx, s["version"]))
        self.assertEqual(got, {(st, r): h.kind for (q, st, r), h in rules.handling.items() if q == "Q1"})
        self.assertTrue(got)
        self.assertEqual(self.tx(s, lambda tx: conversation_io.handlings(tx, s["version"], "Q9")), {})

    def test_earlier_answers_carry_the_other_wish_and_the_hedged_words(self):
        """CL-1 の連絡帳 4-73：前の通話で取れた答えに、言い切っていない答えのお客様の言葉（"hedged"。言い切っていれば無し）と、その質問に伺った
        ほかのご希望の結果（"wish"＝その行の通話の事実 other_wish のうち、その質問の最後のもの。結びがあればそれ・結びが無くご連絡の方法があれば
        means・どちらも無ければ無し）を足す。⛔ 渡していなかった＝前の通話でほかのご希望を言っていただいた最終目的の答えが、続きの通話で
        「どれも要らない」だけになり、「目的が果たせない」で締めた。"""
        import dataclasses
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "accepted", "答えた", "答えた", hedged="たぶん大丈夫です"))
        said = call_report.Said(4, "LINEならいいです。")
        base = rep()

        def wishes(*w):
            return dataclasses.replace(base, other_wishes=tuple(call_report.OtherWish(said, "Q1", *x) for x in w)).to_json()
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, wishes(("無い", None, None, None, "none"),   # 同じ質問は最後のもの
                                                                          ("LINEでの連絡", "LINE", "line", None, None))))
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"])),
                         {"Q1": {"state": "accepted", "value": "答えた", "condition": "答えた", "hedged": "たぶん大丈夫です",
                                 "wish": "means", "wish_means": "line"}})   # 連絡先を伺う途中で切れた＝ご連絡の方法は言っていただいた（続きの電話で連絡先を伺う）
        cases = ((("LINEでの連絡", "LINE", "line", "test_line_id", "means"), "means"), (("無い", None, None, None, "none"), "none"),
                 ((None, None, None, None, None), None))           # W1 の答えをいただく前に切れた＝入れない
        for w, want in cases:
            with self.subTest(want=want):
                r = self.tx(s, lambda tx: call_report.from_json(wishes(w)))
                self.assertEqual(r.other_wishes[0].result, want)
        self.write(s, aid, ItemValue("Q1", "accepted", "答えた", "答えた"))   # 言い切っていれば hedged は無し
        self.assertNotIn("hedged", self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"])
        # 答えを取った通話とは別の通話の報告にあるほかのご希望は付けない（答えを取った通話の報告からだけ読む。⛔ 2026-11-03 夜の見直し：行のすべての
        # 通話から拾っていた＝別の方と話していた通話の結果を、取り直した答えに付けうる形だった）
        self.w.move(s, aid, "ending")
        self.w.move(s, aid, "ended")
        later = self.w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation"):
            self.w.move(s, later, st)
        self.tx(s, lambda tx: call_report.save(tx, later, rep()))           # あとの通話では、ほかのご希望を伺っていない
        self.write(s, later, ItemValue("Q1", "accepted", "答えた", "答えた"))   # 答えを、あとの通話で取り直した
        self.assertNotIn("wish", self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"])

    def test_earlier_answers_carry_how_far_the_steps_went_and_the_contacts_already_asked(self):
        """CL-1 の連絡帳 4-80〜4-83（✅ 2026-11-04 日本時間18時16分・19時26分 ユーザー）：前の通話で取れた答えに、その答えを取った通話の報告から
        (1) ご承認いただけなかったときの手順の進み "point"（事実 not_agreed の最後のものの結び、無ければ heard＝点は伺った）と、手順を伺い終えて
        いないときのご説明する点 "point_said"（点を伺ったお客様の言葉、無ければ点の値）(2) 連絡先を伺う前に切れたご連絡の方法 "wish_means"（お電話・
        連絡先を伺えた方法には無い）(3) 伺えた連絡先 "wish_contact" と LINE の伺い方 "wish_via"（連絡先のあるときだけ）を足す。事実が無ければ入れない
        （点の答えをいただく前に切れた＝続きの電話の始めに伺う）。読み方は報告の形の1か所（会話の側の flowctl.earlier_* と同じ決まり）。"""
        import dataclasses
        base = rep()

        def facts(agreed=(), wishes=()):
            return dataclasses.replace(base, not_agreed=tuple(agreed), other_wishes=tuple(wishes)).to_json()

        def earlier(report):   # 報告は通話ごとに1つ（書き換えられない）ので、場合ごとに通話を作る
            s = self.ready()
            aid = self.answered(s)
            self.write(s, aid, ItemValue("Q1", "accepted", "答えた", "答えた"))   # 試験の業務の Q1（どの型の質問でも同じ読み方）
            self.tx(s, lambda tx: conversation_io.save_report(tx, aid, report))
            return self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]
        said = call_report.Said(4, "料金のところです。")
        heard = call_report.NotAgreed(4, "Q1", "料金のところです。", 1, True, None, False, "料金")
        got = earlier(facts([heard]))
        self.assertEqual((got["point"], got["point_said"]), ("heard", "料金のところです。"))   # ご説明の途中で切れた
        cases = (((dataclasses.replace(heard, said=None),), ("heard", "料金")),             # お客様の言葉が無ければ点の値
                 ((heard, dataclasses.replace(heard, outcome="agreed")), ("agreed", None)),   # 最後のもの・手順を終えていればご説明しない
                 ((dataclasses.replace(heard, question="Q9"),), (None, None)))              # ほかの質問の事実は入れない
        for agreed, want in cases:
            with self.subTest(want=want):
                got = earlier(facts(agreed))
                self.assertEqual((got.get("point"), got.get("point_said")), want)
        line = call_report.OtherWish(said, "Q1", "LINEでの連絡", "LINE", "line", "abc_123", "means", "id")
        cases = ((line, {"wish": "means", "wish_contact": "abc_123", "wish_via": "id"}),
                 (dataclasses.replace(line, contact=None, outcome=None, via=None), {"wish": "means", "wish_means": "line"}),
                 (dataclasses.replace(line, means="phone", value="お電話", contact=None, via=None, outcome=None), {"wish": "means"}),  # お電話は伺わない
                 (dataclasses.replace(line, means="email", contact="a@example.jp", via=None), {"wish": "means", "wish_contact": "a@example.jp"}),
                 (dataclasses.replace(line, outcome=None), {"wish": "means", "wish_contact": "abc_123", "wish_via": "id"}),   # 連絡先は伺えた
                 (dataclasses.replace(line, contact=None, via=None), {"wish": "means"}))   # 伺い終えた（連絡先は無いまま申し伝えた）
        for w, want in cases:
            with self.subTest(want=want):
                got = earlier(facts(wishes=(w,)))
                self.assertEqual({k: got[k] for k in got if k.startswith("wish")}, want)

    def test_keys_used_keep_only_names_and_fingerprints(self):
        """使ったキーの識別名（CL-1 の 4-67）：名前と値の指紋の頭8桁だけ。同じものは重ねず、前のものは消さない。キーの値らしいものは
        データベースが断る（0130）。"""
        s = self.ready()
        aid = self.answered(s)
        live = {"service": "live", "key": "GEMINI_API_KEY_1#872cbac5"}
        judge = {"service": "judge", "key": "ANTHROPIC_JUDGE_KEY#0a1b2c3d"}
        self.tx(s, lambda tx: conversation_io.record_keys(tx, aid, [live]))
        self.tx(s, lambda tx: conversation_io.record_keys(tx, aid, [live, judge]))
        got = self.tx(s, lambda tx: tx.one("SELECT keys_used FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["keys_used"]
        self.assertEqual(sorted(got, key=str), sorted([live, judge], key=str))
        for bad in ({"service": "live", "key": "AIzaSyD-0123456789abcdefghijklmnopqrstu"},   # 値そのもの
                    {"service": "live", "key": "GEMINI_API_KEY_1#872cbac5ff"},               # 指紋が長い
                    {"service": "voice", "key": "GEMINI_API_KEY_1#872cbac5"},                # 知らない種類
                    {"service": "live", "key": "GEMINI_API_KEY_1#872cbac5", "value": "x"}):   # 余分な項目
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                self.tx(s, lambda tx: conversation_io.record_keys(tx, aid, [bad]))


if __name__ == "__main__":
    unittest.main()
