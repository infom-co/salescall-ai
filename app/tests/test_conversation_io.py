"""会話の処理が書き込む入口の試験（core/conversation_io.py。2026-10-24 ユーザー：入口は「CL-2」。DESIGN.md 4-4・7-1）。"""

from __future__ import annotations

import unittest

from salescall.core import attempts, business_rules, call_report, conversation_io, customer_contacts, dnc, gate, settings
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
        self.assertTrue(self.tx(s, lambda tx: dnc.declared_in(tx, aid)))


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

    def test_earlier_answers_carry_the_last_facts_as_written_and_the_hedged_words(self):
        """前の通話で取れた答え（CL-1 の flowctl.EARLIER_KEYS＝state・value・condition・hedged・wish_fact・point_fact）。"hedged"＝言い切っていない答えの
        お客様の言葉（言い切っていれば無し。CL-1 の連絡帳 4-73 の (2)）。"wish_fact"・"point_fact"＝その答えを取った通話の報告の通話の事実 other_wish・
        not_agreed のうち、その質問の最後の1つを、報告に置いた形のまま（無ければ無し）。読み方は会話の側の1か所だけ（✅ CL-2 の連絡帳 4-212 の案2・
        CL-1 の連絡帳 4-88。⛔ 前は、ここで6つの項目に読み分けて渡し、会話の側の読み方と2か所になっていた＝もう伺った連絡先を伺い直す不具合）。"""
        import dataclasses
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "accepted", "答えた", "答えた", hedged="たぶん大丈夫です"))
        said = call_report.Said(4, "LINEならいいです。")
        none = call_report.OtherWish(said, "Q1", "無い", None, None, None, "none")
        line = call_report.OtherWish(call_report.Said(6, "LINEなら。"), "Q1", "LINEでの連絡", "LINE", "line", "abc_123", "means", "id")
        heard = call_report.NotAgreed(5, "Q1", "料金のところです。", 1, True, None, False, "料金")
        other_q = call_report.NotAgreed(7, "Q9", "うーん。", 1, None, None, False, None)
        first = dataclasses.replace(heard, turn=3, said="うーん。", point=None)
        other_w = dataclasses.replace(none, question="Q9")
        # 同じ質問は最後のもの・ほかの質問の事実（最後に並んでいても）は入れない
        report = dataclasses.replace(rep(), other_wishes=(none, line, other_w), not_agreed=(first, heard, other_q))
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, report.to_json()))
        placed = self.tx(s, lambda tx: tx.one("SELECT report FROM salescall.call_reports WHERE attempt_id = %s", (aid,)))["report"]["facts"]
        got = self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))
        self.assertEqual(got, {"Q1": {"state": "accepted", "value": "答えた", "condition": "答えた", "hedged": "たぶん大丈夫です",
                                      "wish_fact": placed["other_wish"][1], "point_fact": placed["not_agreed"][1],
                                      "who": "target"}})   # 置いた報告の形のまま・その通話で繋いだ方（4-106 の H6）
        self.assertEqual((got["Q1"]["wish_fact"]["contact"], got["Q1"]["wish_fact"]["via"], got["Q1"]["point_fact"]["said"]),
                         ("abc_123", "id", "料金のところです。"))
        self.write(s, aid, ItemValue("Q1", "accepted", "答えた", "答えた"))   # 言い切っていれば hedged は無し
        self.assertNotIn("hedged", self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"])
        # 答えを取った通話とは別の通話の報告にある事実は付けない（答えを取った通話の報告からだけ読む。⛔ 2026-11-03 夜の見直し：行のすべての
        # 通話から拾っていた＝別の方と話していた通話の結果を、取り直した答えに付けうる形だった）。その質問の事実が無ければ鍵ごと入れない
        self.w.move(s, aid, "ending")
        self.w.move(s, aid, "ended")
        later = self.w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation"):
            self.w.move(s, later, st)
        self.tx(s, lambda tx: call_report.save(tx, later, rep()))           # あとの通話では、ほかのご希望もご承認の手順も伺っていない
        self.write(s, later, ItemValue("Q1", "accepted", "答えた", "答えた"))   # 答えを、あとの通話で取り直した
        self.assertEqual(set(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]), {"state", "value", "condition", "who"})

    def test_earlier_answers_carry_who_was_connected_in_the_call_that_took_them(self):
        """前の通話の答えに、その答えを取った通話で繋いだ方（"who"＝call_report.CONNECT_WHO の target・successor・substitute）を質問ごとに添える
        （CL-1 の連絡帳 4-106 の H6：続きの電話で判断いただける代わりの方に繋がったとき、前の通話で代わりの方とお話しした答えかを見分ける）。
        繋いだ方は、その通話のいちばん新しい繋ぎ（`CallReport.connected_who` の1か所）。who の無い繋ぎ（前の形の報告）は目的のお客様。
        繋いだ事実の無い通話・報告の無い通話の答えには入れない。あとの通話の繋ぎは、前の通話で取った答えに付けない。"""
        import dataclasses
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "confirmed", {"text": "来週の火曜"}, "答えた"))
        self.assertNotIn("who", self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"])   # 報告がまだ無い
        base = rep()
        first, = base.connects
        report = dataclasses.replace(base, connects=(first, dataclasses.replace(first, evidence=call_report.Said(5, "代わりに伺います。"),
                                                                                 who="substitute")))
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, report.to_json()))
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["who"], "substitute")   # いちばん新しい繋ぎ
        self.w.move(s, aid, "ending")
        self.w.move(s, aid, "ended")
        later = self.w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation"):
            self.w.move(s, later, st)
        self.tx(s, lambda tx: call_report.save(tx, later, dataclasses.replace(base, connects=(dataclasses.replace(first, who="successor"),))))
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["who"], "substitute")   # あとの通話の繋ぎは付けない
        self.write(s, later, ItemValue("Q1", "confirmed", {"text": "来週の水曜"}, "答えた"))   # あとの通話で取り直した答えは、その通話の繋ぎ
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["who"], "successor")
        self.assertEqual([r.connected_who for r in (rep(), rep(("reception",)), dataclasses.replace(base, connects=()))],
                         ["target", None, None])   # who の無い繋ぎ＝目的のお客様・繋いでいない＝無し

    def test_customer_words_on_answers_not_taken_are_kept_but_are_not_hedged_marks(self):
        """依頼者へ渡すお客様の言葉（CL-1 の連絡帳 4-115 の (2)＝Flow.customer_words）は、「-」「✕」で渡る答え（わからない・答えたくない・保留・
        聞き取れない）にも付き、項目の値の欄 hedged に書ける（依頼者への報告で値の欄の横に出す＝DESIGN.md 7-1b）。言い切っていない答えの印として
        読むのは取れた答えのときだけ（`judge.hedged_words` の1か所）：前の通話の答えの "hedged" にも、判定の入力の「曖昧」の印にも入れない
        （⛔ 入れると、伺う条件の「言い切っていないときだけ伺う」が、わからない答えのあとにも伺う質問になる）。"""
        from salescall.core import judge
        s = self.ready()
        aid = self.answered(s)
        words = "まだ決まってないんですよね。／うーん、それもまだ決まってないですね。"
        for state, cond, reason in (("dont_know", "わからない", "undecided"), ("refused", "答えたくない", "unwilling"),
                                    ("unclear", None, None), ("empty", None, None)):
            with self.subTest(state=state):
                self.write(s, aid, ItemValue("Q1", state, None, cond, reason=reason, hedged=words))
                row, = Write.items(self, s)
                self.assertEqual(row["hedged"], words)                                         # 依頼者へ渡す言葉は残る
                self.assertNotIn("hedged", self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"])).get("Q1", {}))
                self.assertIsNone(judge.hedged_words(state, words))
        self.write(s, aid, ItemValue("Q1", "accepted", "15日あたり", "答えた", hedged="15日あたりですかね。"))   # 取れた答えの言葉は印
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["hedged"], "15日あたりですかね。")
        self.assertEqual((judge.hedged_words("confirmed", "x"), judge.hedged_words("accepted", None)), ("x", None))

    def test_values_that_are_not_definite_are_kept_as_said(self):
        """承っておく形で受け取った値（「15日あたり」「10名くらい」「14時ごろ」。CL-1 の連絡帳 4-115 の (3)）は、言葉のまま置き、言葉のまま返す
        （こちらは業務の質問の値を日時・数として読まない＝読むのは会話の報告のかけ直しの時刻だけ＝spoken_time.read の1か所。⏳ 値の形の確かめ
        core/value_types.py を作るときも、確定でない言葉の付いた値を断らない・確定した値に直さない）。"""
        s = self.ready()
        aid = self.answered(s)
        for value in ("15日あたり", "10名くらい", "14時ごろ"):
            with self.subTest(value=value):
                self.write(s, aid, ItemValue("Q1", "accepted", value, "答えた", hedged=f"{value}ですかね。"))
                self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["value"], value)

    def test_earlier_facts_include_what_later_calls_said_about_the_same_answer(self):
        """答えを取り直さない続きの電話で、同じ答えについて話した事実（申し伝え直した点＝not_agreed の outcome unresolved・told）も、
        次の電話へ渡す（その答えを取った通話と、そのあとの通話の報告から、最後のもの。⛔ 2026-11-06 の見直し (C) の1：答えを取った
        通話の報告だけを読んでいたので、また切れると3回目を申し伝えた＝CL-1 の連絡帳 4-99「また切れても3回目は言いません」）。"""
        import dataclasses
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "accepted", "いいえ", "答えた"))
        told = call_report.NotAgreed(5, "Q1", "料金のところです。", 1, False, None, False, "料金", "request")   # 申し伝える一言まで言って切れた
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, dataclasses.replace(rep(), not_agreed=(told,)).to_json()))
        self.assertEqual(self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["point_fact"]["outcome"], None)
        self.w.move(s, aid, "ending")
        self.w.move(s, aid, "ended")
        later = self.w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation"):
            self.w.move(s, later, st)
        again = dataclasses.replace(told, turn=2, outcome="unresolved")          # 続きの電話で申し伝え直した（答えは取り直さない）
        self.tx(s, lambda tx: conversation_io.save_report(tx, later, dataclasses.replace(rep(), not_agreed=(again,)).to_json()))
        fact = self.tx(s, lambda tx: conversation_io.earlier(tx, s["task"]))["Q1"]["point_fact"]
        self.assertEqual((fact["turn"], fact["outcome"], fact["told"]), (2, "unresolved", "request"))

    def test_contacts_heard_in_the_call_are_added_to_the_row(self):
        """電話でお客様が答えた連絡先は、その行（お客様リストの1行）に足す（2026-11-05 ユーザー：「お客様が電話で答えてくれた場合は、
        このシートを元にしたデータベースに加える必要があるでしょ？」。customer_contacts.from_report の1か所。報告を置いた取引の中）。
        LINE の ID・メール・郵送の住所・申し出で伺ったメールは連絡先に、LINE のご招待を送る番号・お電話の番号は記録だけの番号に
        （かけない）。形の合わない値は足さない（報告には残る）。"""
        import dataclasses
        s = self.ready()
        aid = self.answered(s)
        w = call_report.OtherWish
        wishes = (w(call_report.Said(4, "LINEなら。"), "Q1", "LINEでの連絡", "LINE", "line", "abc_123", "means", "id"),
                  w(call_report.Said(5, "LINEで。"), "Q1", "LINEでの連絡", "LINE", "line", "090-0000-0141", "means", "sms"),
                  w(call_report.Said(6, "メールで。"), "Q1", "メールでの連絡", "メール", "email", "taro@example.com", "means"),
                  w(call_report.Said(7, "電話で。"), "Q1", "お電話での連絡", "お電話", "phone", "090-0000-0142", "means"),
                  w(call_report.Said(8, "郵送で。"), "Q1", "郵送での連絡", "郵送", "post", "東京都千代田区1-1", "means"),
                  w(call_report.Said(9, "メールで。"), "Q1", "メールでの連絡", "メール", "email", "taro@", "means"))
        cb = call_report.Callback("other_means", None, None, None, call_report.Said(10, "資料はメールで送って。"),
                                  means=(("email", "hanako@example.com"),))
        report = dataclasses.replace(rep(), other_wishes=wishes, callbacks=(cb,))
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, report.to_json()))
        got = self.tx(s, lambda tx: tx.all(
            "SELECT kind, value, origin, attempt_id, turn FROM salescall.task_contacts WHERE task_id = %s ORDER BY seq", (s["task"],)))
        self.assertEqual([(r["kind"], r["value"], r["origin"], r["attempt_id"], r["turn"]) for r in got],
                         [("line_id", "abc_123", "call", aid, 4), ("email", "taro@example.com", "call", aid, 6),
                          ("address", "東京都千代田区1-1", "call", aid, 8), ("email", "hanako@example.com", "call", aid, 10)])
        nums = self.tx(s, lambda tx: tx.all("SELECT phone_e164, label, record_origin, source_attempt_id FROM salescall.task_numbers"
                                            " WHERE task_id = %s AND source = 'record' ORDER BY phone_e164", (s["task"],)))
        self.assertEqual([(r["phone_e164"], r["label"], r["record_origin"], r["source_attempt_id"]) for r in nums],
                         [("+819000000141", "line", "call", aid), ("+819000000142", "said", "call", aid)])
        now = self.tx(s, lambda tx: customer_contacts.now(tx, s["task"]))["contacts"]
        self.assertEqual({(x["kind"], x["value"]) for x in now},                       # いまの連絡先は種類ごとに最新
                         {("line_id", "abc_123"), ("email", "hanako@example.com"), ("address", "東京都千代田区1-1")})

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
