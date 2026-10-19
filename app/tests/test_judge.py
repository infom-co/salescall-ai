"""業務の判定（Q5）と次の動き（Q4）の決め方の試験（データベースを使わない部品）。DESIGN.md 7-1・7-1c・7-2。

同じ入力からは、いつも同じ結果が1つに決まる。順のどれにも当たる入力で、上の順が勝つことも確かめる。
"""

from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from salescall.core import gate
from salescall.core.business_rules import RulesInvalid, check
from salescall.core.call_report import CallReport, Cut, ReportInvalid, Said, from_json
from salescall.core.judge import Facts, Item, JudgeInput, classify_cut, judge
from salescall.core.next_action import Alternate, AskAgain, Recheck, Target, choose_number, decide_after_call, decide_after_denial
from tests.support import callback, make_report, rules_raw

V = uuid.uuid4()
RULES = check(V, ("Q1", "Q2"), ("Q1", "Q2", "Q3"), rules_raw(("Q1", "Q2"), decline={"Q3": ["いりません"]}))
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=timezone.utc)
TALKED = Facts("ended", None, True, False, NOW)
TAKEN = {"Q1": Item("confirmed", None, None), "Q2": Item("accepted", "はい", None)}


LATER = NOW + timedelta(hours=1)


def report(parties=("target",), *, dnc=False, oos=False, complaint=False, not_reached=None, callback=None,
           cut=Cut("ai", True, False)) -> CallReport:
    return make_report(parties, dnc=dnc, oos=oos, complaint=complaint, not_reached=not_reached, callback=callback, cut=cut)


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
            ("かけ直しの約束", dict(items={}, rep=report(callback=callback("later", LATER))), "callback_scheduled"),
            ("目的のお客様に繋がらなかった", dict(items={}, rep=report(("reception",), not_reached="absent")), "not_reached"),
            ("出なかった", dict(facts=Facts("no_answer", None, False, False, NOW), rep=None, items={}), "open"),
            ("留守電だけ", dict(facts=Facts("ended", None, True, False, NOW), rep=report(("voicemail",), cut=None), items={}), "open"),
            ("お客様が出る前の結果不明", dict(facts=Facts("technical_error", None, False, False, NOW), rep=None, items={}), "open"),
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

    def test_explicit_refusal_wins_over_missing_cut_material(self):
        """はっきりした拒否（通話中の申告・通話後の検証で見つかった）は、切れ方の材料が無くても拒否（2026-10-16 の見直し J9）。
        材料が無く、拒否も無いときだけ判定できない。"""
        self.assertEqual(run(rep=report(dnc=True, cut=None)).state, "do_not_call")
        self.assertEqual(run(rep=report(cut=None), refused_after=True).state, "do_not_call")
        self.assertEqual(run(rep=report(cut=None)).detail["unknown_cause"], "cut_missing")

    def test_a_conclude_handling_ends_the_row_even_if_later_questions_were_not_asked(self):
        """扱いが「結論として終える」の項目があれば、結論で終える（会話の側は丁寧に締めて終えるので、あとの質問は聞かれない。
        2026-10-17：前日の「どれもが結論のときだけ」は扱いの表と食い違っていたので取り下げた）。"""
        refused_q1 = Item("refused", None, "policy")
        self.assertEqual(run(items={"Q1": refused_q1}).state, "concluded")
        self.assertEqual(run(items={"Q1": refused_q1}, rep=report(callback=callback("later", LATER))).state,
                         "concluded")
        self.assertEqual(run(items={"Q1": Item("dont_know", None, "undecided")}).state, "incomplete")   # 扱いが「日を改める」

    def test_decline_answer_must_be_taken(self):
        self.assertEqual(run(items={**TAKEN, "Q3": Item("heard", "いりません", None)}).state, "completed")

    def test_input_errors_are_unknown_not_guessed(self):
        for name, kw in [
            ("わからないの理由が無い", dict(items={"Q1": Item("refused", None, None), "Q2": TAKEN["Q2"]})),
            ("宣言に無い理由", dict(items={"Q1": Item("dont_know", None, "そのほか"), "Q2": TAKEN["Q2"]})),
            ("繋がらなかった理由が無い", dict(items={}, rep=report(("reception",)))),
            ("お客様が出たのに報告が無い", dict(rep=None, items={})),
        ]:
            with self.subTest(name):
                self.assertEqual(run(**kw).state, "unknown")
        self.assertEqual(run(facts=Facts("technical_error", None, True, False, NOW), rep=None, items={}).state, "open")   # 落ちた通話

    def test_dnc_reasons_and_flags(self):
        self.assertEqual(run(rep=report(dnc=True)).dnc_reason, "refused_in_call")
        self.assertEqual(run(refused_after=True).dnc_reason, "refused_found_after")
        self.assertEqual(run(rep=report(("wrong_party",))).dnc_reason, "wrong_party")
        # 番号違いの電話口が断った・何も言わずに切った：その番号だけ（行のお客様ごと止めない。2026-10-16 見直し）
        for name, kw in (("断った", dict(rep=report(("wrong_party",), dnc=True))),
                         ("検証で断りが見つかった", dict(rep=report(("wrong_party",)), refused_after=True))):
            with self.subTest(name):
                j = run(**kw)
                self.assertEqual((j.state, j.dnc_reason, j.detail["wrong_party_refused"]),
                                 ("out_of_scope", "wrong_party", True))
        self.assertIsNone(run().dnc_reason)
        self.assertTrue(run(rep=report(complaint=True)).complaint)
        # FAX の回線：その番号は以後かけない（Intent.md 9-4。2026-10-17 の計画書との突き合わせ：前はかけ直していた）。止めるのは
        # その番号だけ（番号違いと同じ）。お客様の声が混じれば FAX の回線ではない
        j = run(rep=report(("fax",)), items={})
        self.assertEqual((j.state, j.dnc_reason, j.detail["out_of_scope"]), ("out_of_scope", "fax_line", "fax_line"))
        self.assertNotEqual(run(rep=report(("reception", "fax")), items={}).dnc_reason, "fax_line")

    def test_policy_keys(self):
        self.assertEqual(run(items={"Q1": TAKEN["Q1"]}).detail["policy_key"], "incomplete")
        self.assertEqual(run(items={}, rep=report(("reception",), not_reached="not_transferred")).detail["policy_key"],
                         "not_reached_not_transferred")
        self.assertEqual(run(items={}, rep=report(callback=callback("ask_who", person="岡田"))).detail["policy_key"],
                         "callback_unspecified_time")
        self.assertIsNone(run(items={}, rep=report(callback=callback("later", LATER))).detail["policy_key"])

    def test_same_input_same_result(self):
        kw = dict(items={"Q1": TAKEN["Q1"]})
        self.assertEqual(run(**kw), run(**kw))


class CutTable(unittest.TestCase):
    """7-1c（2026-10-14 ユーザー承認）。"""

    def test_each_row(self):
        dropped = lambda side: Facts("dropped", side, True, False, NOW)
        rows = [
            ("締めのあと", dropped("callee"), Cut("ai", True, False), "normal"),
            ("断りの言葉のあと", dropped("callee"), Cut("none", False, True), "refused"),
            ("お客様の言葉が途中で途切れた", dropped("unknown"), Cut("callee", False, False), "interrupted"),
            ("回線の側で切れた", dropped("line"), Cut(None, False, False), "interrupted"),
            ("AIが話している途中に、お客様が切った", dropped("callee"), Cut("ai", False, False), "refused"),
            ("話し終えたあとの間に、お客様が切った", dropped("unknown"), Cut("none", False, False), "refused"),
            ("決められない", dropped("unknown"), Cut(None, False, False), "unclear"),
        ]
        for name, facts, cut, expected in rows:
            with self.subTest(name):
                self.assertEqual(classify_cut(facts, report(cut=cut)), expected)

    def test_only_conversations_with_people(self):
        self.assertIsNone(classify_cut(Facts("dropped", "callee", True, False, NOW), report(("voicemail",), cut=Cut("ai", False, False))))
        self.assertIsNone(classify_cut(Facts("no_answer", None, False, False, NOW), None))
        self.assertEqual(classify_cut(Facts("dropped", "callee", True, False, NOW), report(cut=None)), "missing")

    def test_missing_cut_facts_are_an_input_error_not_a_refusal(self):
        """切れ方の材料の報告漏れで、ふつうに終えたお客様を営業拒否にしない（材料が無い ≠ 決められない）。"""
        j = run(rep=report(cut=None))
        self.assertEqual((j.state, j.dnc_reason, j.disconnect), ("unknown", None, None))

    def test_unclear_follows_the_setting(self):
        unclear = dict(facts=Facts("dropped", "unknown", True, False, NOW), rep=report(cut=Cut(None, False, False)), items={})
        as_refusal = run(**unclear, unclear_is_refusal=True)
        as_recall = run(**unclear, unclear_is_refusal=False)
        self.assertEqual((as_refusal.state, as_refusal.disconnect), ("do_not_call", "refused"))
        self.assertEqual((as_recall.state, as_recall.disconnect), ("incomplete", "interrupted"))

    def test_refusing_cut_is_do_not_call(self):
        j = run(facts=Facts("dropped", "callee", True, False, NOW), rep=report(cut=Cut("ai", False, False)), items={})
        self.assertEqual((j.state, j.dnc_reason, j.disconnect), ("do_not_call", "refused_in_call", "refused"))

    def test_a_callee_whose_role_is_not_known_yet_is_a_conversation(self):
        """役が分かる前のお客様（person）：お客様と話した区間として扱う。AIが話している途中に黙って切った ⇒ 拒否（原則14）。
        役が分からないまま締めた ⇒ 目的のお客様に繋がらなかった理由が要る（無ければ判定待ち）。⚠️ 前は相手が空になり、
        「会話に至らなかった」と読んで拒否を登録しなかった（2026-10-19 の見直し）。"""
        cut_by_callee = Facts("dropped", "callee", True, False, NOW)
        j = run(facts=cut_by_callee, rep=report(("person",), cut=Cut("ai", False, False)), items={})
        self.assertEqual((j.state, j.dnc_reason, j.disconnect), ("do_not_call", "refused_in_call", "refused"))
        self.assertEqual(run(rep=report(("person",)), items={}).detail["unknown_cause"], "not_reached_reason_missing")
        self.assertEqual(run(rep=report(("person",), not_reached="absent"), items={}).state, "not_reached")
        self.assertEqual(run(rep=report(("person", "target")), items={}).state, "incomplete")
        self.assertEqual(run(facts=cut_by_callee, rep=report((), cut=Cut("ai", False, False)), items={}).state, "open")


P_NONE = gate.Pacing(3, NOW, None, False)
HOUR = timedelta(hours=1)        # 試験の業務の決まりの間隔（1時間おき）が指す時刻は NOW + HOUR（_retry_at の答えとして渡す）
MAIN, ALT = uuid.UUID(int=1), uuid.UUID(int=2)     # 一覧の番号・お客様が言った別の番号（連絡先）


def after_call(j_kw, *, normal=P_NONE, promised=gate.Pacing(3, NOW, None, True), times=1,
               alternate=None):
    j = run(**j_kw)
    return decide_after_call(j, normal=normal, promised=promised, rules=RULES,
                             times_with_key=times, now=NOW, retry_at=NOW + HOUR,
                             target=choose_number(j, dialed=Target(MAIN), alternate=alternate, recheck=None))


class AfterCall(unittest.TestCase):
    def test_ends(self):
        for kw, reason in [(dict(), "completed"), (dict(rep=report(dnc=True)), "do_not_call"),
                           (dict(rep=report(complaint=True), items={}), "complaint")]:
            with self.subTest(reason=reason):
                d = after_call(kw)
                self.assertEqual((d.kind, d.end_reason), ("end", reason))

    def test_unknown_holds_the_row(self):
        """判定できない：終えずに判定待ちで残す（2026-10-16 ユーザー決定）。"""
        d = after_call(dict(rep=None, items={}))
        self.assertEqual((d.kind, d.end_reason, d.at), ("hold", None, None))

    def test_callback(self):
        at = NOW + timedelta(hours=3)
        d = after_call(dict(items={}, rep=report(callback=callback("later", at))),
                       normal=gate.Pacing(0, NOW, None, False))
        self.assertEqual((d.kind, d.at, d.promised), ("call", at, True))      # 約束したかけ直しは回数の上限でも止めない
        later = NOW + timedelta(hours=5)
        d = after_call(dict(items={}, rep=report(callback=callback("later", at))),
                       promised=gate.Pacing(3, later, "cross_client_interval_other_tenant", True))
        self.assertEqual(d.at, later)                                          # 門の最も早い時刻より前にしない
        d = after_call(dict(items={}, rep=report(callback=callback("ask_who", person="岡田"))))
        self.assertEqual((d.kind, d.at, d.promised), ("call", NOW + HOUR, True))

    def test_callee_named_time_is_kept_on_the_schedule(self):
        """お客様が言った時刻は予定に残る（NG時間でもかけてよい根拠）。門で止められても引き継ぐ。"""
        at = NOW + timedelta(hours=3)
        d = after_call(dict(items={}, rep=report(callback=callback("later", at))))
        self.assertEqual((d.at, d.callee_at), (at, at))
        d = after_call(dict(items={}, rep=report(callback=callback("ask_who", person="岡田"))))
        self.assertIsNone(d.callee_at)                                         # 時刻を言われていない
        self.assertIsNone(after_call(dict(items={"Q1": TAKEN["Q1"]})).callee_at)
        d = decide_after_denial(gate.Denied("line_busy", *gate.REASONS["line_busy"]), current_promised=True,
                                current=Target(MAIN), pacing=P_NONE, now=NOW, opening=None, retry_wait=300,
                                current_callee_at=at)
        self.assertEqual(d.callee_at, at)

    def test_policy(self):
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}))
        self.assertEqual((d.kind, d.at, d.promised), ("call", NOW + HOUR, False))
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}), times=4)
        self.assertEqual((d.kind, d.end_reason), ("end", "policy_max:incomplete"))
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}), normal=gate.Pacing(0, NOW, None, False))
        self.assertEqual((d.kind, d.end_reason), ("end", "max_attempts"))
        j = run(items={"Q1": TAKEN["Q1"]})
        d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1,
                              now=NOW, retry_at=None, target=Target(MAIN))
        self.assertEqual((d.kind, d.at), ("call", NOW + timedelta(days=1)))   # 時間帯が無い：行を終えず、1日後にもう一度確かめる
        two_days = NOW + timedelta(days=2)
        d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1,
                              now=NOW, retry_at=two_days, target=Target(MAIN))
        self.assertEqual(d.at, two_days)                                           # 方針の間隔が指す時刻（n 日後）
        late = NOW + timedelta(days=1)
        d = after_call(dict(items={"Q1": TAKEN["Q1"]}), normal=gate.Pacing(2, late, "daily_attempts", False))
        self.assertEqual(d.at, late)
        ending = check(V, ("Q1", "Q2"), ("Q1", "Q2", "Q3"), rules_raw(("Q1", "Q2"), retry={
            k: {"action": "end"} for k in ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred",
                                             "callback_unspecified_time")}))
        d = decide_after_call(run(items={"Q1": TAKEN["Q1"]}), normal=P_NONE, promised=P_NONE,
                              rules=ending, times_with_key=1, now=NOW, retry_at=NOW + HOUR, target=Target(MAIN))
        self.assertEqual((d.kind, d.end_reason), ("end", "incomplete"))


class AlternateNumber(unittest.TestCase):
    """どの番号へかけるかは choose_number の1か所（DESIGN.md 3-5）。"""
    OTHER = report(callback=callback("other_number", NOW + timedelta(hours=1), "03-0000-0002"))

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
        d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1,
                              now=NOW, retry_at=NOW + HOUR, target=choose_number(j, dialed=Target(ALT), alternate=None, recheck=None))
        self.assertEqual((d.kind, d.contact_id), ("call", ALT))
        later = run(items={}, rep=report(callback=callback("later", LATER)))
        self.assertEqual(choose_number(later, dialed=Target(ALT), alternate=None, recheck=None), Target(ALT))

    def test_missing_material_is_an_error(self):
        with self.assertRaises(ValueError):
            choose_number(run(items={}, rep=self.OTHER), dialed=Target(MAIN), alternate=None, recheck=None)
        with self.assertRaises(ValueError):
            choose_number(WrongNumberTheCalleeGave.wrong(), dialed=Target(ALT), alternate=None, recheck=None)

    def test_refusal_wins_over_the_other_number(self):
        j = run(items={}, rep=report(dnc=True, callback=self.OTHER.callback))
        d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1,
                              now=NOW, retry_at=NOW + HOUR, target=choose_number(j, dialed=Target(MAIN), alternate=Alternate(ALT, True, None),
                                                            recheck=None))
        self.assertEqual((d.kind, d.end_reason), ("end", "do_not_call"))


class WrongNumberTheCalleeGave(unittest.TestCase):
    """お客様が言った番号が番号違いだったとき（2026-10-15 ユーザー決定。DESIGN.md 3-5）：証拠と付け合わせ、聞き違いなら正しい番号へ、
    言われたとおりなら、番号を聞いた通話の番号へかけ、番号違いだったことを告げて番号を聞き直す。"""
    FIXED = uuid.UUID(int=3)   # 聞き違いだったときの正しい番号

    @staticmethod
    def wrong(dialed_callee=True):
        return run(facts=Facts("ended", None, True, dialed_callee, NOW), rep=report(("wrong_party",)), items={})

    def decide(self, recheck, *, dialed=Target(ALT)):
        j = self.wrong()
        t = choose_number(j, dialed=dialed, alternate=None, recheck=recheck)
        return decide_after_call(j, normal=gate.Pacing(0, NOW, None, False), promised=P_NONE,
                                 rules=RULES, times_with_key=1, now=NOW, retry_at=NOW + HOUR, target=t)

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
        no_answer = run(facts=Facts("no_answer", None, False, False, NOW), rep=None, items={})
        self.assertEqual(choose_number(no_answer, dialed=asking, alternate=None, recheck=None), asking)
        later = NOW + timedelta(hours=3)
        decide = lambda ask: decide_after_call(no_answer, normal=gate.Pacing(0, NOW, None, False),
                                               promised=P_NONE, rules=RULES, times_with_key=9, now=NOW, retry_at=NOW + HOUR, target=asking,
                                               ask=ask)
        d = decide(AskAgain(1, 3, later))
        self.assertEqual((d.kind, d.target, d.promised, d.at), ("call", asking, True, later))   # 行の上限・方針では止めない
        d = decide(AskAgain(3, 3, later))
        self.assertEqual((d.kind, d.end_reason), ("end", "ask_number_again_unanswered"))
        d = decide(AskAgain(1, 3, None))
        self.assertEqual((d.kind, d.at, d.target, d.promised), ("call", NOW + timedelta(days=1), asking, True))   # 終えない
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
        self.assertEqual((d.kind, d.at, d.promised), ("call", NOW + timedelta(days=1), False))   # 行を終えない
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
        x = rules_raw(("Q1",)); x["retry"]["open"] = {"action": "call", "max_times": 0, "interval": {"hours": 1}}; bad.append(("回数0", x))
        for name, iv in [("間隔の単位が違う", {"minutes": 30}), ("間隔が2つ", {"hours": 1, "days": 1}), ("間隔が0", {"days": 0}),
                         ("間隔が数でない", {"hours": "2"}), ("前の形（秒）", None)]:
            x = rules_raw(("Q1",))
            x["retry"]["open"] = ({"action": "call", "max_times": 3, "interval_seconds": 60} if iv is None
                                  else {"action": "call", "max_times": 3, "interval": iv})
            bad.append((name, x))
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
        good = report(callback=callback("later", LATER), dnc=True, complaint=True).to_json()
        self.assertEqual(from_json(good), report(callback=callback("later", LATER), dnc=True, complaint=True))
        monthly = replace(callback("later", time="月末なら"), cycle="month")   # 月の理由（月ぎめ・入金の間隔など）
        self.assertEqual(from_json(report(callback=monthly).to_json()).callback, monthly)
        vm = replace(report(("voicemail",)), voicemail_messages=(Said(0, ""),))   # 留守番電話に伝言を残した
        self.assertEqual(from_json(vm.to_json()).voicemail_messages, (Said(0, ""),))
        for cb in (callback("other_number", None, "ゼロサン、ゼロゼロゼロゼロの…"),     # そろえられない番号も受ける
                   callback("other_number", LATER, "03-0000-0001"), callback("other_number"),   # 番号を聞けずに終わった
                   callback("later", time="いつでも")):
            self.assertEqual(from_json(report(callback=cb).to_json()).callback, cb)
        facts = good["facts"]
        rec = {"turn": 1, "said": "あとでかけて"}
        for name, change in [("知らない項目", {"extra": 1}), ("事実が無い", {"facts": None}),
                             ("知らない事実の種類", {"facts": {**facts, "praise": [rec]}}),
                             ("知らない電話口の種別", {"facts": {"party": [{**rec, "party": "boss", "by": "report"}]}}),
                             ("種別の出どころが無い", {"facts": {"party": [{**rec, "party": "target"}]}}),
                             ("版が違う", {"version": 2}), ("並びでない", {"facts": {"do_not_call": rec}}),
                             ("根拠の発話の番号が無い", {"facts": {"do_not_call": [{"said": "もうかけないで"}]}}),
                             ("根拠の発話の番号が負", {"facts": {"do_not_call": [{"turn": -1, "said": ""}]}}),
                             ("根拠の発話の番号が真偽", {"facts": {"do_not_call": [{"turn": True, "said": ""}]}}),
                             ("根拠の発話が長すぎる", {"facts": {"complaint": [{"turn": 1, "said": "あ" * 81}]}}),
                             ("記録に知らない項目", {"facts": {"complaint": [{**rec, "level": 3}]}}),
                             ("知らない締めた理由", {"facts": {"end": [{**rec, "reason": "bye"}]}}),
                             ("目的のお客様に繋がったのに理由", {"not_reached": "absent"}),
                             ("別の番号でないのに番号がある", {"facts": {"callback": [
                                 {**rec, "why": "later", "time": None, "number": "03-0000-0001", "person": None}]}}),
                             ("言ったままの番号が空", {"facts": {"callback": [
                                 {**rec, "why": "other_number", "time": None, "number": " ", "person": None}]}}),
                             ("時刻の言葉が文字でない", {"facts": {"callback": [
                                 {**rec, "why": "later", "time": 15, "number": None, "person": None}]}}),
                             ("知らないかけ直しの理由", {"facts": {"callback": [
                                 {**rec, "why": "soon", "time": None, "number": None, "person": None}]}}),
                             ("知らない周期", {"facts": {"callback": [
                                 {**rec, "why": "later", "time": "月末", "number": None, "person": None, "cycle": "week"}]}}),
                             ("留守番電話でないのに伝言", {"facts": {"voicemail_message": [rec]}})]:
            with self.subTest(name), self.assertRaises(ReportInvalid):
                from_json({**good, **change})

    def test_facts_are_read_in_one_place(self):
        """判定が読むもの（申告・苦情・電話口の種別・かけ直し・急いでいたか）は、通話の事実から読む。そろえた番号は言ったままの番号から。"""
        r = from_json({"version": 3, "not_reached": None, "cut": None, "apology_delivered": None, "facts": {
            "party": [{"turn": 0, "said": "はい、山田商事です", "party": "reception", "by": "report"},
                      {"turn": 2, "said": "お電話代わりました", "party": "target", "by": "report"}],
            "callback": [{"turn": 4, "said": "携帯にお願いします", "why": "other_number", "time": None,
                          "number": "ゼロキュウゼロのゼロゼロゼロゼロのゼロゼロゼロイチ", "person": None}],
            "end": [{"turn": 5, "said": "急いでるので", "reason": "callee_hurry"}]}})
        self.assertEqual((r.parties, r.do_not_call, r.complaint, r.out_of_scope, r.hurried, r.end_reason),
                         (("reception", "target"), False, False, False, True, "callee_hurry"))
        self.assertEqual((r.callback.reason, r.callback.number_e164), ("other_number", "+819000000001"))
        self.assertFalse(from_json({**r.to_json(), "facts": {"end": [{"turn": 5, "said": "", "reason": "done"}]}}).hurried)
        asked = from_json({**r.to_json(), "facts": {"ai_question": [{"turn": 3, "said": "これ機械ですか？"}],
                                                     "end": [{"turn": 6, "said": "AIなの？", "reason": "ai_question"}]}})
        self.assertEqual((len(asked.ai_questions), asked.end_reason, asked.hurried), (1, "ai_question", False))   # 版 0.5
        self.assertEqual(from_json(asked.to_json()), asked)

    def test_callback_time_is_read_from_the_words(self):
        """かけ直しの時刻は、お客様が言ったままの言葉を通話の時刻から「かけてよい時間の幅」として読む（spoken_time の1か所。
        2026-10-19 ユーザー）。1つに決まらない言い方を確かめずに終わった ⇒ 判定できない（直すのは会話＝CL-1）、読めない言い方
        ⇒ 判定できない（直すのは読み方＝CL-2）。時刻を任せる答えは、時刻の決まっていない約束。"""
        j = run(items={}, rep=report(callback=callback("later", time="明日の午後3時")))   # 通話は日本の 2030-01-07（月）19:00
        cb = j.detail["callback"]
        self.assertEqual((j.state, cb["at"], cb["until"], cb["daily"], cb["exact"], j.detail["policy_key"]),
                         ("callback_scheduled", "2030-01-08T15:00:00+09:00", None, None, True, None))
        self.assertEqual(cb["time_said"], "明日の午後3時")
        j = run(items={}, rep=report(callback=callback("later", time="明日の夕方")))
        cb = j.detail["callback"]
        self.assertEqual((cb["at"], cb["until"], cb["daily"], cb["exact"], j.detail["policy_key"]),
                         ("2030-01-08T16:00:00+09:00", "2030-01-08T18:00:00+09:00", [16 * 3600, 18 * 3600], False, None))
        j = run(items={}, rep=report(callback=callback("later", time="水曜日ならいつでも")))
        cb = j.detail["callback"]
        self.assertEqual((cb["at"], cb["until"], cb["daily"]), ("2030-01-09T00:00:00+09:00", "2030-01-10T00:00:00+09:00", None))
        j = run(items={}, rep=report(callback=callback("later", time="いつでも大丈夫です")))
        self.assertEqual((j.state, j.detail["callback"]["at"], j.detail["policy_key"]),
                         ("callback_scheduled", None, "callback_unspecified_time"))
        from salescall.core.judge import UNKNOWN_CAUSE_FIXERS
        for said, cause, fixer in (("夕方", "callback_time_unconfirmed", "CL-1"),     # 19時の通話：今日の夕方は過ぎた
                                   ("明日のお昼", "callback_time_unconfirmed", "CL-1"),   # 昼は確かめる（2026-10-19 ユーザー）
                                   ("そのへんで", "callback_time_unreadable", "CL-2")):
            with self.subTest(said=said):
                j = run(items={}, rep=report(callback=callback("later", time=said)))
                self.assertEqual((j.state, j.detail["unknown_cause"], UNKNOWN_CAUSE_FIXERS[cause]), ("unknown", cause, fixer))
        # 拒否は、読めない時刻より先（発信禁止を、時刻の読み方の抜けで遅らせない）
        self.assertEqual(run(items={}, rep=report(dnc=True, callback=callback("later", time="そのへんで"))).state, "do_not_call")


class Causes(unittest.TestCase):
    def test_program_causes_go_to_the_admin(self):
        """判定できない原因のうち、プログラムの誤り（会話の処理）は管理者が直す（2026-10-16 ユーザー承認）。理由の無い
        「わからない」「答えない」も会話の処理の誤り（業務の決まりは、決まった理由のすべてに扱いが無いと保存できない）。"""
        from salescall.core.judge import PROGRAM_CAUSES, UNKNOWN_CAUSES
        self.assertLessEqual(PROGRAM_CAUSES, set(UNKNOWN_CAUSES))
        self.assertEqual(set(UNKNOWN_CAUSES), PROGRAM_CAUSES)
        for items in ({"Q1": Item("refused", None, None), "Q2": TAKEN["Q2"]},
                      {"Q1": Item("dont_know", None, "そのほか"), "Q2": TAKEN["Q2"]}):
            self.assertEqual(run(items=items).detail["unknown_cause"], "reason_missing")


if __name__ == "__main__":
    unittest.main()
