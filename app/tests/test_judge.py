"""業務の判定（Q5）と次の動き（Q4）の決め方の試験（データベースを使わない部品）。DESIGN.md 7-1・7-1c・7-2。

同じ入力からは、いつも同じ結果が1つに決まる。順のどれにも当たる入力で、上の順が勝つことも確かめる。
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from salescall.core import gate
from salescall.core.business_rules import RulesInvalid, check
from salescall.core.call_report import CallReport, Callback, Cut, ReportInvalid, from_json
from salescall.core.judge import Facts, Item, JudgeInput, classify_cut, judge
from salescall.core.next_action import Alternate, AskAgain, Recheck, Target, choose_number, decide_after_call, decide_after_denial
from tests.support import rules_raw

V = uuid.uuid4()
RULES = check(V, ("Q1", "Q2"), ("Q1", "Q2", "Q3"), rules_raw(("Q1", "Q2"), decline={"Q3": ["いりません"]}))
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=timezone.utc)
TALKED = Facts("ended", None, True, False)
TAKEN = {"Q1": Item("confirmed", None, None), "Q2": Item("accepted", "はい", None)}


def report(parties=("target",), *, dnc=False, oos=False, complaint=False, not_reached=None, callback=None,
           cut=Cut("ai", True, False)) -> CallReport:
    return CallReport(tuple(parties), dnc, oos, complaint, False, not_reached, callback, cut)


def run(*, facts=TALKED, rep=report(), items=None, refused_after=False, unclear_is_refusal=True):
    return judge(JudgeInput(facts, rep, TAKEN if items is None else items, RULES, refused_after, unclear_is_refusal))


class Order(unittest.TestCase):
    def test_each_rule(self):
        cases = [
            ("拒否の申告", dict(rep=report(dnc=True)), "do_not_call"),
            ("検証で拒否が見つかった", dict(refused_after=True), "do_not_call"),
            ("番号違い", dict(rep=report(("reception", "wrong_party"))), "out_of_scope"),
            ("範囲外", dict(rep=report(oos=True)), "out_of_scope"),
            ("用件を断られた", dict(items={**TAKEN, "Q3": Item("accepted", "いりません", None)}), "declined"),
            ("すべて取れた", dict(), "completed"),
            ("答えない＝結論", dict(items={"Q1": Item("refused", None, "policy"), "Q2": TAKEN["Q2"]}), "concluded"),
            ("かけ直しの約束", dict(items={}, rep=report(callback=Callback("later", NOW, None, None, None))), "callback_scheduled"),
            ("目的の人に繋がらなかった", dict(items={}, rep=report(("reception",), not_reached="absent")), "not_reached"),
            ("出なかった", dict(facts=Facts("no_answer", None, False, False), rep=None, items={}), "open"),
            ("留守電だけ", dict(facts=Facts("ended", None, True, False), rep=report(("voicemail",), cut=None), items={}), "open"),
            ("相手が出る前の結果不明", dict(facts=Facts("technical_error", None, False, False), rep=None, items={}), "open"),
            ("一部だけ取れた", dict(items={"Q1": TAKEN["Q1"]}), "incomplete"),
            ("わからない＝日を改める", dict(items={"Q1": Item("dont_know", None, "undecided"), "Q2": TAKEN["Q2"]}), "incomplete"),
        ]
        for name, kw, expected in cases:
            with self.subTest(name):
                self.assertEqual(run(**kw).state, expected)

    def test_upper_rules_win(self):
        both = dict(rep=report(dnc=True, oos=True), items={**TAKEN, "Q3": Item("accepted", "いりません", None)})
        self.assertEqual(run(**both).state, "do_not_call")
        self.assertEqual(run(rep=report(oos=True)).state, "out_of_scope")            # 範囲外は、取れていても範囲外
        self.assertEqual(run(items={**TAKEN, "Q3": Item("accepted", "いりません", None)}).state, "declined")

    def test_decline_answer_must_be_taken(self):
        self.assertEqual(run(items={**TAKEN, "Q3": Item("heard", "いりません", None)}).state, "completed")

    def test_input_errors_are_unknown_not_guessed(self):
        for name, kw in [
            ("わからないの理由が無い", dict(items={"Q1": Item("refused", None, None), "Q2": TAKEN["Q2"]})),
            ("宣言に無い理由", dict(items={"Q1": Item("dont_know", None, "そのほか"), "Q2": TAKEN["Q2"]})),
            ("繋がらなかった理由が無い", dict(items={}, rep=report(("reception",)))),
            ("相手が出たのに報告が無い", dict(rep=None, items={})),
        ]:
            with self.subTest(name):
                self.assertEqual(run(**kw).state, "unknown")
        self.assertEqual(run(facts=Facts("technical_error", None, True, False), rep=None, items={}).state, "open")   # 落ちた通話

    def test_dnc_reasons_and_flags(self):
        self.assertEqual(run(rep=report(dnc=True)).dnc_reason, "refused_in_call")
        self.assertEqual(run(refused_after=True).dnc_reason, "refused_found_after")
        self.assertEqual(run(rep=report(("wrong_party",))).dnc_reason, "wrong_party")
        self.assertIsNone(run().dnc_reason)
        self.assertTrue(run(rep=report(complaint=True)).complaint)

    def test_policy_keys(self):
        self.assertEqual(run(items={"Q1": TAKEN["Q1"]}).detail["policy_key"], "incomplete")
        self.assertEqual(run(items={}, rep=report(("reception",), not_reached="not_transferred")).detail["policy_key"],
                         "not_reached_not_transferred")
        self.assertEqual(run(items={}, rep=report(callback=Callback("ask_who", None, None, None, "岡田"))).detail["policy_key"],
                         "callback_unspecified_time")
        self.assertIsNone(run(items={}, rep=report(callback=Callback("later", NOW, None, None, None))).detail["policy_key"])

    def test_same_input_same_result(self):
        kw = dict(items={"Q1": TAKEN["Q1"]})
        self.assertEqual(run(**kw), run(**kw))


class CutTable(unittest.TestCase):
    """7-1c（2026-10-14 ユーザー承認）。"""

    def test_each_row(self):
        dropped = lambda side: Facts("dropped", side, True, False)
        rows = [
            ("締めのあと", dropped("callee"), Cut("ai", True, False), "normal"),
            ("断りの言葉のあと", dropped("callee"), Cut("none", False, True), "refused"),
            ("相手の言葉が途中で途切れた", dropped("unknown"), Cut("callee", False, False), "interrupted"),
            ("回線の側で切れた", dropped("line"), Cut(None, False, False), "interrupted"),
            ("AIが話している途中に、相手が切った", dropped("callee"), Cut("ai", False, False), "refused"),
            ("話し終えたあとの間に、相手が切った", dropped("unknown"), Cut("none", False, False), "refused"),
            ("決められない", dropped("unknown"), Cut(None, False, False), "unclear"),
        ]
        for name, facts, cut, expected in rows:
            with self.subTest(name):
                self.assertEqual(classify_cut(facts, report(cut=cut)), expected)

    def test_only_conversations_with_people(self):
        self.assertIsNone(classify_cut(Facts("dropped", "callee", True, False), report(("voicemail",), cut=Cut("ai", False, False))))
        self.assertIsNone(classify_cut(Facts("no_answer", None, False, False), None))
        self.assertEqual(classify_cut(Facts("dropped", "callee", True, False), report(cut=None)), "missing")

    def test_missing_cut_facts_are_an_input_error_not_a_refusal(self):
        """切れ方の材料の報告漏れで、ふつうに終えた相手を営業拒否にしない（材料が無い ≠ 決められない）。"""
        j = run(rep=report(cut=None))
        self.assertEqual((j.state, j.dnc_reason, j.disconnect), ("unknown", None, None))

    def test_unclear_follows_the_setting(self):
        unclear = dict(facts=Facts("dropped", "unknown", True, False), rep=report(cut=Cut(None, False, False)), items={})
        as_refusal = run(**unclear, unclear_is_refusal=True)
        as_recall = run(**unclear, unclear_is_refusal=False)
        self.assertEqual((as_refusal.state, as_refusal.disconnect), ("do_not_call", "refused"))
        self.assertEqual((as_recall.state, as_recall.disconnect), ("incomplete", "interrupted"))

    def test_refusing_cut_is_do_not_call(self):
        j = run(facts=Facts("dropped", "callee", True, False), rep=report(cut=Cut("ai", False, False)), items={})
        self.assertEqual((j.state, j.dnc_reason, j.disconnect), ("do_not_call", "refused_in_call", "refused"))


P_NONE = gate.Pacing(3, NOW, None, False)
MAIN, ALT = uuid.UUID(int=1), uuid.UUID(int=2)     # 一覧の番号・相手が言った別の番号（連絡先）


def after_call(j_kw, *, verification="done", normal=P_NONE, promised=gate.Pacing(3, NOW, None, True), times=1,
               alternate=None):
    j = run(**j_kw)
    return decide_after_call(j, verification=verification, normal=normal, promised=promised, rules=RULES,
                             times_with_key=times, now=NOW,
                             target=choose_number(j, dialed=Target(MAIN), alternate=alternate, recheck=None))


class AfterCall(unittest.TestCase):
    def test_ends(self):
        for kw, reason in [(dict(), "completed"), (dict(rep=report(dnc=True)), "do_not_call"),
                           (dict(rep=report(complaint=True), items={}), "complaint"),
                           (dict(rep=None, items={}), "judge_unknown")]:
            with self.subTest(reason=reason):
                d = after_call(kw)
                self.assertEqual((d.kind, d.end_reason), ("end", reason))
        d = after_call(dict(items={}), verification="failed")
        self.assertEqual((d.kind, d.end_reason), ("end", "verification_failed"))

    def test_callback(self):
        at = NOW + timedelta(hours=3)
        d = after_call(dict(items={}, rep=report(callback=Callback("later", at, None, None, None))),
                       normal=gate.Pacing(0, NOW, None, False))
        self.assertEqual((d.kind, d.at, d.promised), ("call", at, True))      # 約束したかけ直しは回数の上限でも止めない
        later = NOW + timedelta(hours=5)
        d = after_call(dict(items={}, rep=report(callback=Callback("later", at, None, None, None))),
                       promised=gate.Pacing(3, later, "cross_client_interval_other_tenant", True))
        self.assertEqual(d.at, later)                                          # 門の最も早い時刻より前にしない
        d = after_call(dict(items={}, rep=report(callback=Callback("ask_who", None, None, None, "岡田"))))
        self.assertEqual((d.kind, d.at, d.promised), ("call", NOW + timedelta(seconds=60), True))

    def test_policy(self):
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}))
        self.assertEqual((d.kind, d.at, d.promised), ("call", NOW + timedelta(seconds=60), False))
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}), times=4)
        self.assertEqual((d.kind, d.end_reason), ("end", "policy_max:incomplete"))
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}), normal=gate.Pacing(0, NOW, None, False))
        self.assertEqual((d.kind, d.end_reason), ("end", "max_attempts"))
        late = NOW + timedelta(days=1)
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}), normal=gate.Pacing(2, late, "daily_attempts", False))
        self.assertEqual(d.at, late)
        ending = check(V, ("Q1", "Q2"), ("Q1", "Q2", "Q3"), rules_raw(("Q1", "Q2"), retry={
            k: {"action": "end"} for k in ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred",
                                             "callback_unspecified_time")}))
        d = decide_after_call(run(items={"Q1": TAKEN["Q1"]}), verification="done", normal=P_NONE, promised=P_NONE,
                              rules=ending, times_with_key=1, now=NOW, target=Target(MAIN))
        self.assertEqual((d.kind, d.end_reason), ("end", "incomplete"))


class AlternateNumber(unittest.TestCase):
    """どの番号へかけるかは choose_number の1か所（DESIGN.md 3-5）。"""
    OTHER = report(callback=Callback("other_number", NOW + timedelta(hours=1), "03-0000-0002", "+81300000002", None))

    def test_goes_to_the_number_the_callee_gave(self):
        d = after_call(dict(items={}, rep=self.OTHER), alternate=Alternate(ALT, True, None),
                       normal=gate.Pacing(0, NOW, None, False))
        self.assertEqual((d.kind, d.contact_id, d.promised, d.at), ("call", ALT, True, NOW + timedelta(hours=1)))

    def test_records_only_when_not_called(self):
        for alt, reason in [(Alternate(ALT, False, None), "alternate_not_called"),
                            (Alternate(ALT, True, "number_kind_not_allowed"), "alternate_blocked:number_kind_not_allowed"),
                            (Alternate(None, True, "number_unknown"), "alternate_blocked:number_unknown")]:
            with self.subTest(reason):
                d = after_call(dict(items={}, rep=self.OTHER), alternate=alt)
                self.assertEqual((d.kind, d.end_reason), ("end", reason))

    def test_other_results_keep_the_number_just_called(self):
        j = run(items={"Q1": TAKEN["Q1"]})
        self.assertEqual(choose_number(j, dialed=Target(ALT), alternate=None, recheck=None), Target(ALT))
        d = decide_after_call(j, verification="done", normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1,
                              now=NOW, target=choose_number(j, dialed=Target(ALT), alternate=None, recheck=None))
        self.assertEqual((d.kind, d.contact_id), ("call", ALT))
        later = run(items={}, rep=report(callback=Callback("later", NOW, None, None, None)))
        self.assertEqual(choose_number(later, dialed=Target(ALT), alternate=None, recheck=None), Target(ALT))

    def test_missing_material_is_an_error(self):
        with self.assertRaises(ValueError):
            choose_number(run(items={}, rep=self.OTHER), dialed=Target(MAIN), alternate=None, recheck=None)
        with self.assertRaises(ValueError):
            choose_number(WrongNumberTheCalleeGave.wrong(), dialed=Target(ALT), alternate=None, recheck=None)

    def test_refusal_wins_over_the_other_number(self):
        j = run(items={}, rep=report(dnc=True, callback=self.OTHER.callback))
        d = decide_after_call(j, verification="done", normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1,
                              now=NOW, target=choose_number(j, dialed=Target(MAIN), alternate=Alternate(ALT, True, None),
                                                            recheck=None))
        self.assertEqual((d.kind, d.end_reason), ("end", "do_not_call"))


class WrongNumberTheCalleeGave(unittest.TestCase):
    """相手が言った番号が番号違いだったとき（2026-10-15 ユーザー決定。DESIGN.md 3-5）：証拠と付け合わせ、聞き違いなら正しい番号へ、
    言われたとおりなら、番号を聞いた通話の番号へかけ、番号違いだったことを告げて番号を聞き直す。"""
    FIXED = uuid.UUID(int=3)   # 聞き違いだったときの正しい番号

    @staticmethod
    def wrong(dialed_callee=True):
        return run(facts=Facts("ended", None, True, dialed_callee), rep=report(("wrong_party",)), items={})

    def decide(self, recheck, *, dialed=Target(ALT)):
        j = self.wrong()
        t = choose_number(j, dialed=dialed, alternate=None, recheck=recheck)
        return decide_after_call(j, verification="done", normal=gate.Pacing(0, NOW, None, False), promised=P_NONE,
                                 rules=RULES, times_with_key=1, now=NOW, target=t)

    def test_judged_as_not_reached_and_only_that_number_is_stopped(self):
        j = self.wrong()
        self.assertEqual((j.state, j.detail["not_reached"], j.dnc_reason), ("not_reached", "wrong_alternate", "wrong_party"))
        self.assertEqual(self.wrong(dialed_callee=False).state, "out_of_scope")     # 一覧の番号の番号違いは、いままでどおり

    def test_misheard_goes_to_the_corrected_number(self):
        d = self.decide(Recheck("misheard", Alternate(self.FIXED, True, None), MAIN, False))
        self.assertEqual((d.kind, d.contact_id, d.target.purpose, d.promised), ("call", self.FIXED, "normal", True))
        d = self.decide(Recheck("misheard", Alternate(self.FIXED, True, "dnc_client"), MAIN, False))
        self.assertEqual((d.kind, d.end_reason), ("end", "alternate_blocked:dnc_client"))

    def test_as_said_asks_the_first_party_again(self):
        for outcome in ("as_said", "cannot_check"):
            with self.subTest(outcome):
                d = self.decide(Recheck(outcome, None, MAIN, False))
                self.assertEqual((d.kind, d.contact_id, d.target.purpose, d.target.wrong_contact_id, d.promised),
                                 ("call", MAIN, "ask_number_again", ALT, True))   # 回数の上限でも止めない（約束のかけ直し）

    def test_asks_again_only_once(self):
        d = self.decide(Recheck("as_said", None, MAIN, True))
        self.assertEqual((d.kind, d.end_reason), ("end", "alternate_wrong_again"))

    def test_ask_again_keeps_its_purpose_while_nobody_answers(self):
        """2026-10-15 ユーザー：「時間帯により出られないことはよくある。3回までなどの制限をかけ、別の時間帯にかけ直す」。"""
        asking = Target(MAIN, None, "ask_number_again", ALT)
        no_answer = run(facts=Facts("no_answer", None, False, False), rep=None, items={})
        self.assertEqual(choose_number(no_answer, dialed=asking, alternate=None, recheck=None), asking)
        later = NOW + timedelta(hours=3)
        decide = lambda ask: decide_after_call(no_answer, verification="not_needed", normal=gate.Pacing(0, NOW, None, False),
                                               promised=P_NONE, rules=RULES, times_with_key=9, now=NOW, target=asking,
                                               ask=ask)
        d = decide(AskAgain(1, 3, later))
        self.assertEqual((d.kind, d.target, d.promised, d.at), ("call", asking, True, later))   # 行の上限・方針では止めない
        d = decide(AskAgain(3, 3, later))
        self.assertEqual((d.kind, d.end_reason), ("end", "ask_number_again_unanswered"))
        d = decide(AskAgain(1, 3, None))
        self.assertEqual((d.kind, d.end_reason), ("end", "no_calling_hours"))
        with self.assertRaises(ValueError):
            decide(None)
        talked = run(items={"Q1": TAKEN["Q1"]})                                    # 会話したら、目的は果たした扱い
        self.assertEqual(choose_number(talked, dialed=asking, alternate=None, recheck=None), Target(MAIN))


class AfterDenial(unittest.TestCase):
    def deny(self, reason):
        return gate.Denied(reason, *gate.REASONS[reason])

    def test_each_kind(self):
        self.assertIsNone(decide_after_denial(self.deny("awaiting_decision"), current_promised=False, current=Target(MAIN), pacing=P_NONE,
                                              now=NOW, opening=None, retry_wait=300))
        self.assertIsNone(decide_after_denial(self.deny("not_due"), current_promised=False, current=Target(MAIN), pacing=P_NONE, now=NOW,
                                              opening=None, retry_wait=300))
        self.assertIsNone(decide_after_denial(self.deny("no_schedule"), current_promised=False, current=Target(MAIN), pacing=P_NONE, now=NOW,
                                              opening=None, retry_wait=300))   # 予定の無い行に予定を作らない
        d = decide_after_denial(self.deny("dnc_client"), current_promised=True, current=Target(MAIN), pacing=P_NONE, now=NOW, opening=None,
                                retry_wait=300)
        self.assertEqual((d.kind, d.end_reason), ("end", "dnc_client"))
        opening = NOW + timedelta(hours=14)
        d = decide_after_denial(self.deny("outside_hours"), current_promised=True, current=Target(MAIN), pacing=P_NONE, now=NOW,
                                opening=opening, retry_wait=300)
        self.assertEqual((d.kind, d.at, d.promised), ("call", opening, True))   # 約束したかけ直しの印は残す
        d = decide_after_denial(self.deny("outside_hours"), current_promised=False, current=Target(MAIN), pacing=P_NONE, now=NOW,
                                opening=None, retry_wait=300)
        self.assertEqual((d.kind, d.end_reason), ("end", "no_calling_hours"))
        for reason in ("line_busy", "setting_missing"):
            d = decide_after_denial(self.deny(reason), current_promised=False, current=Target(ALT), pacing=P_NONE, now=NOW,
                                    opening=None, retry_wait=300)
            self.assertEqual((d.kind, d.at, d.contact_id), ("call", NOW + timedelta(seconds=300), ALT))   # 番号はいまの予定のまま
        self.assertIsNone(decide_after_denial(self.deny("task_ended"), current_promised=False, current=None,
                                              pacing=P_NONE, now=NOW, opening=None, retry_wait=300))   # 終えた行に書かない


class Shapes(unittest.TestCase):
    """業務の決まり・会話の報告は、1か所の確かめで、抜けと誤りを受け取らない。"""

    def test_rules_reject_gaps(self):
        good = rules_raw(("Q1",))
        bad = []
        x = rules_raw(("Q1",)); del x["handling"]["Q1"]["dont_know"]["unspecified"]; bad.append(("理由の抜け", x))
        x = rules_raw(("Q1",)); x["handling"]["Q1"]["refused"]["unwilling"] = {"kind": "retry"}; bad.append(("答えないに日を改める", x))
        x = rules_raw(("Q1",)); x["handling"]["Q1"]["refused"]["policy"] = {"kind": "conclude"}; bad.append(("結論の名前が無い", x))
        x = rules_raw(("Q1",)); del x["retry"]["open"]; bad.append(("再発信の方針の抜け", x))
        x = rules_raw(("Q1",)); x["retry"]["open"] = {"action": "call", "max_times": 0, "interval_seconds": 60}; bad.append(("回数0", x))
        x = rules_raw(("Q1",)); x["decline"] = {"Q9": ["いりません"]}; bad.append(("無い質問", x))
        for name, raw in bad:
            with self.subTest(name), self.assertRaises(RulesInvalid):
                check(V, ("Q1",), ("Q1", "Q3"), raw)
        with self.assertRaises(RulesInvalid):
            check(V, (), ("Q1",), rules_raw(()))                         # 必須の質問が無い業務
        for broken in ({"decline": [], "handling": {}, "retry": {}}, {"decline": {}, "handling": "x", "retry": {}}):
            with self.assertRaises(RulesInvalid):
                check(V, ("Q1",), ("Q1",), broken)                       # 表でない値も、確かめの中で断る
        self.assertEqual(check(V, ("Q1",), ("Q1",), good).required, ("Q1",))

    def test_report_rejects_wrong_shapes(self):
        good = report(callback=Callback("later", NOW, None, None, None)).to_json()
        self.assertEqual(from_json(good), report(callback=Callback("later", NOW, None, None, None)))
        for cb in (Callback("other_number", None, "ゼロサン、ゼロゼロゼロゼロの…", None, None),     # そろえられない番号も受ける
                   Callback("other_number", NOW, "03-0000-0001", "+81300000001", None)):
            self.assertEqual(from_json(report(callback=cb).to_json()).callback, cb)
        for name, change in [("知らない項目", {"extra": 1}), ("知らない相手の種別", {"parties": ["boss"]}),
                             ("版が違う", {"version": 99}), ("真偽でない", {"do_not_call": "no"}),
                             ("目的の人に繋がったのに理由", {"not_reached": "absent"}),
                             ("時差の無い時刻", {"callback": {"reason": "later", "at": "2030-01-07T10:00:00",
                                                         "number_said": None, "number_e164": None, "person": None}}),
                             ("別の番号なのに番号が無い", {"callback": {"reason": "other_number", "at": None,
                                                              "number_said": None, "number_e164": None, "person": None}}),
                             ("別の番号なのに、言ったままの番号が無い", {"callback": {
                                 "reason": "other_number", "at": None, "number_said": None, "number_e164": "+81300000001",
                                 "person": None}}),
                             ("別の番号でないのに番号がある", {"callback": {"reason": "later", "at": None,
                                                                 "number_said": "03-0000-0001", "number_e164": None,
                                                                 "person": None}}),
                             ("言ったままの番号が空", {"callback": {"reason": "other_number", "at": None,
                                                             "number_said": " ", "number_e164": None, "person": None}}),
                             ("そろえていない番号", {"callback": {"reason": "other_number", "at": None,
                                                           "number_said": "03-0000-0001", "number_e164": "03-0000-0001",
                                                           "person": None}})]:
            with self.subTest(name), self.assertRaises(ReportInvalid):
                from_json({**good, **change})


if __name__ == "__main__":
    unittest.main()
