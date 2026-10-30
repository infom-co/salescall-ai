"""取り消せない決定の確かめと、発信禁止の取り消しの試験（2026-10-30 管理者のご決定 (6)(7)。表の作り 0152。データベースを通した場面）。

管理者 (6)「案A　検証モードでは、人が録音を確認し、承認を押した場合だけ営業拒否と確定。人が録音やその場のやり取りを確認できる状態に
する必要があります。」(7)「案A」（誤って入れた発信禁止を取り消したら、その発信禁止で終えた行を予定に戻し、依頼者へ渡す結果の
「営業拒否」も外す）。試験のデータは架空のものだけ。
"""

from __future__ import annotations

import unittest

from salescall.core import (approval, attempts, backup, call_report, conversation_io, dnc, fix_requests, gate, next_action, results, settle,
                            tasks)
from salescall.core.access import Rejected
from salescall.core.call_report import Cut, ReportInvalid
from salescall.core.conversation_io import Utterance, UtteranceInvalid
from salescall.core.phone import normalize
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, app_access, fictional_jp_fixed, make_report, owner_access
from tests.test_gate import FakeDriver, dial_exit
from tests.test_settle import TALK, SettleBase


def rep(parties=("target",), **kw):
    return make_report(parties, **kw)


class Helpers(SettleBase):
    """取り消せない決定の試験の土台（試験は持たない）。"""

    def answered_call(self, s, path=TALK[1:3]):
        """本物の門・出口でかけ、お客様が出た所まで進める（締めはまだ）。"""
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in path:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        return c.attempt_id

    def other_row(self, s):
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])   # 同じお客様の、ほかの一覧の行（番号を共有する）
        return self.w.task(s["tenant"], s["client"], run2, s["contact"])

    def dnc_reasons(self, s):
        return sorted(r["r"] for r in self.tx(s, lambda tx: tx.all(
            "SELECT reason::text AS r FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL", (s["phone"],))))

    def released(self, s, aid):
        return self.tx(s, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.result_releases l JOIN salescall.task_results r"
            " ON r.task_id = l.task_id AND r.version_no = l.version_no WHERE r.attempt_id = %s", (aid,)))["n"]

    def pending(self, s, aid):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.decision_pending(%s) AS p, salescall.refusal_undecided(%s) AS u",
                                            (aid, aid)))

    def held(self, s, aid, kind, other=None):
        """保留のあいだ：発信禁止を登録しない・行を終えない（判定待ち）・結果を確定しない・同じお客様のほかの行も待たせる。"""
        self.assertEqual(self.dnc_reasons(s), [])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "hold")
        self.assertEqual(self.released(s, aid), 0)
        self.assertEqual(dict(self.pending(s, aid)), {"p": True, "u": True})
        self.assertEqual([(w["attempt_id"], w["kind"]) for w in self.tx(s, approval.pending_decisions)], [(aid, kind)])
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["detail"]["decision_wait"], kind)
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT disconnect FROM salescall.attempts WHERE attempt_id = %s",
                                                       (aid,)))["disconnect"])
        if other is not None:
            self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, other)).binding, "related_decision_pending")
        self.assertEqual(self.reason(s), "judge_pending")


class Irreversible(Helpers):
    """会話AIの報告と2つ目の判定（意味の判定）がそろったときだけ、その場で決める。そろわなければ、録音の確かめで決まるまで保留。"""

    def test_an_agreed_refusal_is_decided_on_the_spot(self):
        s = self.ready()
        aid = self.call(s, report=rep(dnc=True, check="agreed"))
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].end_reason), ("do_not_call", "do_not_call"))
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])
        self.assertEqual(self.released(s, aid), 1)
        self.assertEqual(self.tx(s, approval.pending_decisions), [])

    def test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it(self):
        """食い違った断り × 録音の確かめで断りとそろった ⇒ 決まりどおり（発信禁止・行を終える・確定）。録音の確かめがまだのあいだに
        検証が済んでいれば、会話の担当へ修正依頼（録音から作り直した報告で返してもらう）。"""
        s = self.ready()
        other = self.other_row(s)
        aid = self.call(s, report=rep(dnc=True, check="disagreed"))
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind), ("do_not_call", "hold"))
        self.held(s, aid, "recording", other)
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r["kind"] for r in fix_requests.open_requests(tx, "CL-1")])
        self.assertIn("irreversible_unchecked", asked)
        folder = backup.client_folder(self.w.a, s["tenant"], s["client"])          # 決まるまで、その通話の録音を消さない
        self.assertIn(str(aid), backup.prune_rules(self.w.a, s["tenant"])[folder]["held"])
        self.assertEqual(self.tx(s, settle.rejudge_held), [])                   # まだ録音の確かめが無い：待ったまま
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="disagreed"), evidence="試験：録音から"))
        self.assertEqual(self.tx(s, settle.rejudge_held), [])                   # 作り直してもそろわない：決めつけず待ったまま
        self.assertTrue(self.pending(s, aid)["p"])
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="agreed"), evidence="試験：録音から"))
        done = self.tx(s, settle.rejudge_held)
        self.assertEqual([d["state"] for d in done], ["do_not_call"])
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "do_not_call")
        self.assertEqual(dict(self.pending(s, aid)), {"p": False, "u": False})
        self.assertEqual(self.released(s, aid), 1)                               # 決まった締め直しの版が確定する
        self.assertNotIn(str(aid), backup.prune_rules(self.w.a, s["tenant"])[folder]["held"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, other, cause="試験：ほかの行の最初の予定"))
        self.assertEqual(self.reason(s, task=other), "dnc_client")               # 広げ方は、ふつうの断りと同じ

    def test_a_refusal_the_recording_check_did_not_find_is_settled_as_usual(self):
        """判定が無い（古い形の報告）断り × 録音の確かめで断りが報告から消えた ⇒ 元に戻す（その報告のまま、ふつうに締める）。"""
        s = self.ready()
        raw = rep(dnc=True).to_json()
        del raw["irreversible_check"]                                            # 古い形の報告（項目が無い）
        aid = self.call(s, report=call_report.from_json(raw), items=[("Q1", "confirmed", None)])
        self.assertEqual(self.settle(s, aid)["decision"].kind, "hold")
        self.held(s, aid, "recording")
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(check="none"), evidence="試験：録音から"))
        done = self.tx(s, settle.rejudge_held)
        self.assertEqual([d["state"] for d in done], ["completed"])
        self.assertEqual(self.dnc_reasons(s), [])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "completed")
        self.assertEqual(self.released(s, aid), 1)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT outcome, by_kind FROM salescall.decision_answers WHERE attempt_id = %s",
                                                      (aid,))), {"outcome": "withdrawn", "by_kind": "recording"})

    def test_wrong_party_and_anger_wait_too_when_not_agreed(self):
        for kw, part in ((dict(parties=("wrong_party",)), "wrong_party"), (dict(complaint=True), "complaint")):
            with self.subTest(part):
                s = self.ready()
                aid = self.call(s, report=rep(check="disagreed", **kw))
                self.assertEqual(self.settle(s, aid)["decision"].kind, "hold")
                self.assertEqual([w["parts"] for w in self.tx(s, approval.pending_decisions)], [[part]])
                self.assertEqual(self.dnc_reasons(s), [])
                self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.notification_events"
                                                              " WHERE kind = 'complaint'"))["n"], 0)   # 苦情も、決まるまで知らせない

    def test_a_declared_refusal_is_sure_but_the_report_check_is_read_for_the_rest(self):
        """通話の途中の申告（会話の側は、そろったときだけ入れる）は、報告の項目によらず、その場で決める。"""
        s = self.ready()
        aid = self.answered_call(s, TALK[1:])
        self.assertFalse(self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))["held"])
        self.tx(s, lambda tx: call_report.save(tx, aid, rep(dnc=True, check="none")))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.assertEqual(self.settle(s, aid)["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(s, approval.pending_decisions), [])


    def test_the_waits_and_answers_are_never_changed(self):
        """保留と答えは、書き換えない・消さない（表の持ち主でも。引き金が守る）。"""
        s = self.ready()
        aid = self.call(s, report=rep(dnc=True, check="disagreed"))
        self.settle(s, aid)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(check="none"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        for sql in ("UPDATE salescall.decision_waits SET kind = 'approval' WHERE attempt_id = %s",
                    "DELETE FROM salescall.decision_waits WHERE attempt_id = %s",
                    "UPDATE salescall.decision_answers SET outcome = 'confirmed' WHERE attempt_id = %s",
                    "DELETE FROM salescall.decision_answers WHERE attempt_id = %s"):
            with self.subTest(sql=sql[:30]), self.assertRaises(Rejected):
                owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(sql, (aid,)))


class VerificationMode(Helpers):
    """検証モードの業務：断り・番号違い・怒っていたは、そろっていても、契約者が録音とやり取りを確かめて承認を押したときだけ確定する。"""

    def human(self, s):
        self.put_client(s, "approval.human", True)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)

    def test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button(self):
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"))
        self.assertEqual(self.settle(s, aid)["decision"].kind, "hold")
        self.held(s, aid, "approval")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.notification_events"
                                                      " WHERE kind = 'decision_approval'"))["n"], 1)
        put = ("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, outcome, parts, by_kind, answered_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'confirmed', '{refusal}', %s, salescall.cur_actor())")
        for kind, actor in (("approval", SYSTEM), ("approval", ADMIN), ("recording", OPERATOR), ("recording", SYSTEM)):
            # 承認は契約者だけ・録音の確かめはシステムの処理だけ・その種類の保留が無ければ答えられない（データベースが守る）
            with self.subTest(kind=kind, actor=actor.kind), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(put, (aid, kind)), actor)
        out = self.tx(s, lambda tx: approval.confirm_decision(tx, aid), OPERATOR)
        self.assertEqual(out["state"], "do_not_call")
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "do_not_call")
        self.assertEqual(self.released(s, aid), 1)
        with self.assertRaises(LookupError):                                     # 答えは1つ
            self.tx(s, lambda tx: approval.send_back_decision(tx, aid), OPERATOR)

    def test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual(self):
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        out = self.tx(s, lambda tx: approval.send_back_decision(tx, aid), OPERATOR)
        self.assertEqual((out["state"], out["decision"].end_reason), ("completed", "completed"))
        self.assertEqual(self.dnc_reasons(s), [])
        self.assertFalse(self.pending(s, aid)["p"])
        self.assertEqual(self.released(s, aid), 0)                               # 締め直した版も、ほかの結果と同じく契約者が確かめる
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["snapshot"]["withdrawn"], ["refusal"])

    def test_the_declaration_in_the_call_is_not_put_in_the_ledger(self):
        s = self.ready()
        self.human(s)
        aid = self.answered_call(s)
        got = self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        self.assertEqual((got["entry_id"], got["held"]), (None, True))
        self.assertEqual(self.dnc_reasons(s), [])


class Revoke(Helpers):
    """誤って入れた発信禁止を取り消したら、その発信禁止で終えた行を予定に戻し、依頼者へ渡す結果の「営業拒否」も外す（(7)「案A」）。"""

    def refused(self, s, *, extra=None):
        """断りの通話を締める（extra＝締める前に、その通話でお客様が言った別の番号として行に足す番号＝広げた登録が2つになる）。"""
        aid = self.call(s, report=rep(dnc=True), items=[("Q1", "confirmed", None)])
        if extra is not None:
            self.tx(s, lambda tx: tasks.add_callee_number(tx, s["task"], normalize(extra), source_attempt_id=aid))
        self.assertEqual(self.settle(s, aid)["state"], "do_not_call")
        return aid

    def test_revoking_by_the_source_call_puts_the_rows_back_and_takes_the_refusal_out(self):
        s = self.ready()
        other = self.other_row(s)
        self.tx(s, lambda tx: next_action.schedule_now(tx, other, cause="試験：ほかの行の最初の予定"))
        aid = self.refused(s)
        denied = self.claim(s, task=other)
        self.assertEqual(denied.reason, "dnc_client")
        self.tx(s, lambda tx: next_action.after_denial(tx, other, denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, other))["end_reason"], "dnc_client")
        with self.assertRaises(Rejected):                                        # システムの処理は取り消せない（今の守りのまま）
            self.tx(s, lambda tx: dnc.revoke_source(tx, aid, "試験"), SYSTEM)
        got = self.tx(s, lambda tx: dnc.revoke_source(tx, aid, "依頼者からの正式な求め（試験）"), OPERATOR)
        self.assertEqual(self.dnc_reasons(s), [])
        self.assertEqual((got["resettled"], got["reopened"]), ([aid], [other]))
        latest = self.tx(s, lambda tx: results.latest(tx, s["task"]))
        self.assertEqual((latest["kind"], latest["business_state"]), ("rejudged", "completed"))   # 結果の「営業拒否」を外した
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, other))["kind"], "call")   # 予定に戻した
        self.assertNotIn(getattr(self.claim(s, task=other), "reason", None), ("dnc_client", "task_ended"))   # 門は、もう終えない

    def test_revoking_one_of_the_spread_entries_keeps_the_refusal(self):
        """広げた登録の一部だけを取り消しても、断りそのものは誤りではない（ほかの番号は止めたまま。締め直さない）。1件ずつの取り消しで
        残りも取り消したら、そこで締め直す（どちらの入口も同じ1か所）。"""
        s = self.ready()
        extra = normalize(fictional_jp_fixed()).e164
        aid = self.refused(s, extra=extra)
        entries = self.tx(s, lambda tx: {r["phone_e164"]: r["entry_id"] for r in tx.all(
            "SELECT phone_e164, entry_id FROM salescall.dnc_entries WHERE source_attempt_id = %s", (aid,))})
        self.assertEqual(set(entries), {s["phone"], extra})
        got = self.tx(s, lambda tx: dnc.revoke(tx, entries[extra], "試験"), ADMIN)
        self.assertEqual(got["resettled"], [])
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["business_state"], "do_not_call")
        got = self.tx(s, lambda tx: dnc.revoke(tx, entries[s["phone"]], "試験"), ADMIN)
        self.assertEqual(got["resettled"], [aid])
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["business_state"], "completed")


class EndedWithoutCheck(Helpers):
    """前の作業担当が残した問い (8)：切れ方の分からない通話のあと、確かめのかけ直しが一度もかからないまま行が終わった（門が終えた など）。
    決まっていない＝営業拒否の証拠が無いので、営業拒否にせず、その通話の結果どおりで確定する（approval の1か所）。"""

    def test_the_unclear_call_result_is_confirmed_as_it_is_when_the_row_ends(self):
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="callee",
                        report=rep(cut=Cut("ai", False, False)))
        self.assertEqual(self.settle(s, aid)["state"], "incomplete")
        self.assertEqual(self.released(s, aid), 0)                               # 確かめのかけ直しを待つあいだは確定しない
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "manual"), OPERATOR)  # 契約者が止めた：門が終える
        denied = self.claim(s)
        self.assertEqual(denied.reason, "dnc_client")
        self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "end")
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.decision_pending(%s) AS p", (aid,)))["p"])
        self.assertEqual(self.released(s, aid), 1)
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["business_state"], "incomplete")


class Utterances(Helpers):
    """その場のやり取りの文字（お客様と AI の発話・時刻。計画書 11-1）：通話ごとに起きた順に残し、書き換えない・消さない。"""

    def test_the_words_are_kept_in_order_and_never_changed(self):
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        with self.assertRaises(UtteranceInvalid):                                # お客様が出ていない
            self.tx(s, lambda tx: conversation_io.write_utterance(tx, c.attempt_id, Utterance("callee", 1, "もしもし", 1.0)))
        for st in TALK[1:3]:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        work = conversation_io.utterance_work(c.attempt_id, Utterance("ai", 0, "お世話になっております。試験の会社です。", 0.5))
        self.assertEqual(self.tx(s, work), 1)
        self.assertEqual(self.tx(s, lambda tx: conversation_io.write_utterance(
            tx, c.attempt_id, Utterance("callee", 1, "営業の電話はお断りです", 3.2))), 2)
        for bad in (Utterance("person", 1, "x", 1.0), Utterance("callee", -1, "x", 1.0), Utterance("callee", 1, " ", 1.0),
                    Utterance("callee", 1, "x", -1)):
            with self.subTest(bad=bad), self.assertRaises(UtteranceInvalid):
                conversation_io.utterance_work(c.attempt_id, bad)
        got = self.tx(s, lambda tx: conversation_io.utterances(tx, c.attempt_id))
        self.assertEqual([(r["seq"], r["speaker"], r["turn"]) for r in got], [(1, "ai", 0), (2, "callee", 1)])
        for sql in ("UPDATE salescall.call_utterances SET said = 'x' WHERE attempt_id = %s",
                    "DELETE FROM salescall.call_utterances WHERE attempt_id = %s"):
            with self.subTest(sql=sql[:6]), self.assertRaises(Rejected):   # 表の持ち主でも（権限ではなく、引き金が守る）
                owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(sql, (c.attempt_id,)))
        other = self.ready()                                                     # ほかの依頼者からは見えない
        self.assertEqual(app_access().in_client(SYSTEM, other["tenant"], other["client"], lambda tx: conversation_io.utterances(
            tx, c.attempt_id)), [])
        for st in TALK[3:]:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep()))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        with self.assertRaises(UtteranceInvalid):                                # 検証が済んだら足さない
            self.tx(s, lambda tx: conversation_io.write_utterance(tx, c.attempt_id, Utterance("ai", 1, "失礼いたします", 9.0)))


class ReportShape(unittest.TestCase):
    """会話の報告の項目 irreversible_check（データの形 版 1.07）：無い・null は「判定が無い」、決まった値だけを受ける。"""

    def test_the_check_is_read_and_old_reports_have_none(self):
        raw = make_report(dnc=True).to_json()
        self.assertEqual(call_report.from_json(raw).irreversible_check, "agreed")
        for v in (None, "missing"):
            r = dict(raw)
            if v is None:
                del r["irreversible_check"]
            else:
                r["irreversible_check"] = None
            self.assertEqual(call_report.from_json(r).irreversible_check, "none")
        with self.assertRaises(ReportInvalid):
            call_report.from_json({**raw, "irreversible_check": "maybe"})

    def test_without_takes_the_decisions_out(self):
        r = make_report(("wrong_party",), dnc=True, complaint=True, cut=Cut("ai", False, True))
        w = r.without({"refusal", "wrong_party"})
        self.assertEqual((w.refusals, w.parties, w.cut.callee_refused_last, bool(w.complaints)), ((), (), False, True))
        self.assertEqual(r.without(()), r)
        with self.assertRaises(ValueError):
            r.without({"other"})


if __name__ == "__main__":
    unittest.main()
