"""取り消せない決定の確かめと、発信禁止の取り消しの試験（2026-10-30 管理者のご決定 (6)(7)。表の作り 0152。データベースを通した場面）。

管理者 (6)「案A　検証モードでは、人が録音を確認し、承認を押した場合だけ営業拒否と確定。人が録音やその場のやり取りを確認できる状態に
する必要があります。」(7)「案A」（誤って入れた発信禁止を取り消したら、その発信禁止で終えた行を予定に戻し、依頼者へ渡す結果の
「営業拒否」も外す）。試験のデータは架空のものだけ。
"""

from __future__ import annotations

import time
import unittest

from salescall.core import (approval, attempts, backup, call_report, conversation_io, dnc, fix_requests, gate, measure, next_action, results,
                            settle, tasks)
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

    def seq(self, s, aid):
        """いまの保留の版（承認と差し戻しに渡す）。"""
        return self.tx(s, lambda tx: tx.one("SELECT salescall.decision_wait_seq(%s) AS s", (aid,)))["s"]

    def approve(self, s, aid, actor=OPERATOR):
        return self.tx(s, lambda tx: approval.approve_decision(tx, aid, self.seq(s, aid)), actor)

    def send_back(self, s, aid, actor=OPERATOR):
        return self.tx(s, lambda tx: approval.send_back_decision(tx, aid, self.seq(s, aid)), actor)

    def asks(self, s):
        return [(w["ask"], w["ask_why"]) for w in self.tx(s, approval.pending_decisions)]

    def notices(self, s, kind):
        return self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.notification_events WHERE kind = %s", (kind,)))["n"]

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
        """食い違った断り × 録音から作り直した報告で断りとそろった ⇒ 決まりどおり（発信禁止・行を終える・確定）。締めは通話後の検証の
        あとなので、検証が作り直さずに済んでいれば、会話の担当へ修正依頼（録音から作り直した報告で返してもらう）と、契約者への確かめを
        すぐ求める。作り直してもそろわなければ、新しい版で契約者に求め直す。"""
        s = self.ready()
        other = self.other_row(s)
        aid = self.call(s, report=rep(dnc=True, check="disagreed"))
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind), ("do_not_call", "hold"))
        self.held(s, aid, "recording", other)
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r["kind"] for r in fix_requests.open_requests(tx, "CL-1")])
        self.assertIn("irreversible_unchecked", asked)
        self.assertEqual(self.asks(s), [(True, "recording_unchecked")])
        folder = backup.client_folder(self.w.a, s["tenant"], s["client"])          # 決まるまで、その通話の録音を消さない
        self.assertIn(str(aid), backup.prune_rules(self.w.a, s["tenant"])[folder]["held"])
        self.assertEqual(self.tx(s, settle.rejudge_held), [])                   # まだ録音の確かめが無い：待ったまま
        self.assertEqual(self.notices(s, "decision_approval"), 1)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="disagreed"), evidence="試験：録音から"))
        self.assertEqual([d["state"] for d in self.tx(s, settle.rejudge_held)], ["do_not_call"])   # 締め直した：まだそろわない
        self.assertTrue(self.pending(s, aid)["p"])
        self.assertEqual(self.asks(s), [(True, "recording_undecided")])          # 新しい版で、契約者に求め直す
        self.assertEqual(self.notices(s, "decision_approval"), 2)
        self.assertEqual(self.tx(s, settle.rejudge_held), [])                   # 新しい作り直しが届くまで、読み直さない
        self.assertEqual(self.notices(s, "decision_approval"), 2)
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
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT seq, parts FROM salescall.decision_waits WHERE attempt_id = %s"
                                                      " ORDER BY seq", (aid,))), [{"seq": 1, "parts": ["refusal"]},
                                                                                   {"seq": 2, "parts": []}])   # 待たなくなった版
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.decision_answers WHERE attempt_id = %s",
                                                      (aid,)))["n"], 0)

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
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="none"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)                                          # 作り直してもそろわない：契約者に求め直す
        self.send_back(s, aid)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.decision_asks WHERE attempt_id = %s",
                                                      (aid,)))["n"], 2)
        for sql in ("UPDATE salescall.decision_waits SET kind = 'approval' WHERE attempt_id = %s",
                    "DELETE FROM salescall.decision_waits WHERE attempt_id = %s",
                    "UPDATE salescall.decision_answers SET outcome = 'confirmed' WHERE attempt_id = %s",
                    "DELETE FROM salescall.decision_answers WHERE attempt_id = %s",
                    "UPDATE salescall.decision_asks SET why = 'recording_missing' WHERE attempt_id = %s",
                    "DELETE FROM salescall.decision_asks WHERE attempt_id = %s"):
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
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.assertEqual(self.settle(s, aid)["decision"].kind, "hold")
        self.held(s, aid, "approval")
        self.assertEqual(self.notices(s, "decision_approval"), 1)
        put = ("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, outcome, parts, by_kind, answered_by, wait_seq)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'confirmed', %s, %s, salescall.cur_actor(), %s)")
        for kind, actor, parts, seq in (("approval", SYSTEM, ["refusal"], 1), ("recording", OPERATOR, ["refusal"], None),
                                        ("approval", OPERATOR, ["refusal"], 2), ("approval", OPERATOR, ["refusal", "complaint"], 1)):
            # 承認は契約者か管理者だけ・録音の確かめの答えは外した（0154）・いまの版の、まだ答えの無い中身すべてにだけ（データベースが守る）
            with self.subTest(kind=kind, actor=actor.kind, parts=parts, seq=seq), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(put, (aid, parts, kind, seq)), actor)
        out = self.approve(s, aid, ADMIN)                                       # 管理者も押せる（ほかの承認とそろえる）
        self.assertEqual(out["state"], "do_not_call")
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "do_not_call")
        self.assertEqual(self.released(s, aid), 0)            # 承認したのは断りだけ：締め直した版は、ほかの結果と同じく確かめる
        with self.assertRaises(LookupError):                                     # もう答えを求めていない
            self.send_back(s, aid)
        latest = self.tx(s, lambda tx: results.latest(tx, s["task"]))["version_no"]
        self.tx(s, lambda tx: approval.approve_result(tx, s["task"], latest), OPERATOR)
        self.assertEqual(self.released(s, aid), 1)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT wrong_items FROM salescall.judgement_checks WHERE attempt_id = %s",
                                                      (aid,)))["wrong_items"], [])      # 承認した断りは、判定どおり

    def test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual(self):
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        out = self.send_back(s, aid)
        self.assertEqual((out["state"], out["decision"].end_reason), ("completed", "completed"))
        self.assertEqual(self.dnc_reasons(s), [])
        self.assertFalse(self.pending(s, aid)["p"])
        self.assertEqual(self.released(s, aid), 0)                               # 締め直した版も、ほかの結果と同じく契約者が確かめる
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["snapshot"]["withdrawn"], ["refusal"])
        # その場で決めていたはずの断り（2つ目の判定とそろっていた）を差し戻した：締め直した版を契約者が承認しても、その通話の正解には
        # 「違っていた」として入る（入れないと、システムの誤りが誤判定率から黙って落ちる。0153）
        latest = self.tx(s, lambda tx: results.latest(tx, s["task"]))["version_no"]
        self.tx(s, lambda tx: approval.approve_result(tx, s["task"], latest), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT source, wrong_items FROM salescall.judgement_checks WHERE attempt_id = %s",
                                                      (aid,))), {"source": "verification", "wrong_items": ["decision:refusal"]})
        got = self.tx(s, lambda tx: measure.rates(tx, s["business"], source="verification"))
        self.assertEqual((got["checked"], got["wrong"], got["items_wrong"]), (1, 1, 1))

    def test_a_sent_back_refusal_the_judges_did_not_agree_on_is_not_counted_as_wrong(self):
        """2つ目の判定とそろわなかった断りは、フルオートでも、その場では決めない（保留）。差し戻しても、システムの誤りに数えない（その場で
        決めていたはずのものだけ）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="disagreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        self.send_back(s, aid)
        latest = self.tx(s, lambda tx: results.latest(tx, s["task"]))["version_no"]
        self.tx(s, lambda tx: approval.approve_result(tx, s["task"], latest), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT wrong_items FROM salescall.judgement_checks WHERE attempt_id = %s",
                                                      (aid,)))["wrong_items"], [])

    def test_the_held_version_cannot_be_released_or_corrected_before_the_decision(self):
        """保留の版を、結果の承認・直す道で確定できない（見直し24a の (7)。データベースの守り release_blocked も）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        v = self.tx(s, lambda tx: results.latest(tx, s["task"]))["version_no"]
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: approval.approve_result(tx, s["task"], v), OPERATOR)
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=v, business_state="completed",
                                                          wrong_items=[], complaint=False, snapshot={}), OPERATOR)
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by, stage)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor(), 'verification')",
                (s["task"], v)), OPERATOR)
        self.assertEqual(self.released(s, aid), 0)

    def test_the_declaration_in_the_call_is_not_put_in_the_ledger(self):
        s = self.ready()
        self.human(s)
        aid = self.answered_call(s)
        got = self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        self.assertEqual((got["entry_id"], got["held"]), (None, True))
        self.assertEqual(self.dnc_reasons(s), [])

    def test_a_changed_report_makes_a_new_wait_and_an_old_button_is_refused(self):
        """承認待ちのあいだに、録音から作り直した報告で中身が変わった（怒っていた → 断り）：決まった間隔の仕事が締め直して、新しい版で
        求め直す。前の版への押し下げは断る。差し戻しで外れるのは、契約者が見た版の中身だけ（見直し24a の (4)・24b の (B)）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(complaint=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        old = self.seq(s, aid)
        self.assertEqual([w["parts"] for w in self.tx(s, approval.pending_decisions)], [["complaint"]])
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="agreed"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        self.assertEqual([(w["parts"], w["seq"]) for w in self.tx(s, approval.pending_decisions)], [(["refusal"], old + 1)])
        with self.assertRaises(LookupError):
            self.tx(s, lambda tx: approval.send_back_decision(tx, aid, old), OPERATOR)
        self.assertEqual(self.notices(s, "decision_approval"), 2)
        self.send_back(s, aid)
        self.assertEqual(self.dnc_reasons(s), [])                                # 契約者が見て差し戻した断りは入らない
        self.assertFalse(self.pending(s, aid)["p"])


class RecordingCannotDecide(Helpers):
    """録音の確かめで決められない保留（録音が届かなかった・作り直してもそろわない・検証が作り直さなかった）は、契約者に録音とやり取りを
    確かめてもらう（0153・0154。2026-10-30 管理者のご決定 (6)「人が録音やその場のやり取りを確認できる状態にする」）。それまでは保留の
    まま（かけない）。"""

    def unchecked_requests(self):
        """会話の担当への修正依頼 irreversible_unchecked のうち、まだ直していないもの（同じ不具合は1つなので、ほかの試験の分は「直した」に
        してから数える）。"""
        return self.w.a.in_system(SYSTEM, lambda tx: [r for r in fix_requests.open_requests(tx, "CL-1")
                                                      if r["kind"] == "irreversible_unchecked"])

    def close_unchecked(self):
        for r in self.unchecked_requests():
            self.w.a.in_system(SYSTEM, lambda tx: fix_requests.report_fixed(tx, r["request_id"], "試験：ほかの試験の分"))

    def missing(self, s):
        """録音が届かないまま通話後の検証が済んだ、食い違った断りの通話（フルオートの業務）を締める。"""
        self.close_unchecked()
        aid = self.call(s, report=rep(dnc=True, check="disagreed"), verify=None, items=[("Q1", "confirmed", None)])
        self.tx(s, lambda tx: attempts.record_verification(
            tx, aid, summary=attempts.VerificationSummary(0, 0, 0, recording_missing=True)))
        self.settle(s, aid)
        return aid

    def test_an_unchecked_wait_asks_both_the_conversation_side_and_the_contractor(self):
        """録音はあるのに、検証が作り直した報告を返さないまま済んだ：会話の担当へ修正依頼し、契約者にも求める（だれにも知らせずに待たない＝
        見直し24c の A3）。会話の担当が直して作り直した報告が届けば、締め直しで決まる。"""
        s = self.ready()
        self.close_unchecked()
        aid = self.call(s, report=rep(dnc=True, check="disagreed"))
        self.settle(s, aid)
        self.assertEqual(len(self.unchecked_requests()), 1)
        self.assertEqual(self.asks(s), [(True, "recording_unchecked")])
        self.assertEqual(self.notices(s, "decision_approval"), 1)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="agreed"), evidence="試験：録音から"))
        self.assertEqual([d["state"] for d in self.tx(s, settle.rejudge_held)], ["do_not_call"])
        self.assertFalse(self.pending(s, aid)["p"])
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])

    def test_a_rebuilt_report_that_still_disagrees_asks_the_contractor_at_once(self):
        """検証が録音から作り直した報告でも、そろわなかった（保留にした報告がもう作り直しだった）：すぐ契約者に求める。"""
        s = self.ready()
        self.close_unchecked()
        aid = self.call(s, report=rep(dnc=True, check="disagreed"), verify=None)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="disagreed"), evidence="試験：録音から"))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.settle(s, aid)
        self.held(s, aid, "recording")
        self.assertEqual(self.asks(s), [(True, "recording_undecided")])
        self.assertEqual(self.unchecked_requests(), [])

    def test_a_missing_recording_asks_the_contractor_who_confirms(self):
        s = self.ready()
        aid = self.missing(s)
        self.held(s, aid, "recording")                                           # 求めても、答えが出るまで保留のまま
        self.assertEqual(self.asks(s), [(True, "recording_missing")])
        self.assertEqual(self.notices(s, "decision_approval"), 1)
        self.assertEqual(self.tx(s, settle.rejudge_held), [])
        self.assertEqual(self.notices(s, "decision_approval"), 1)
        self.assertEqual(self.unchecked_requests(), [])                         # 録音が無いので、会話の担当には頼まない
        out = self.approve(s, aid)
        self.assertEqual(out["state"], "do_not_call")
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "do_not_call")
        self.assertEqual(self.released(s, aid), 1)                               # フルオート：締め直した版が確定する
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.judgement_checks WHERE attempt_id = %s",
                                                      (aid,)))["n"], 0)          # 結果の承認（正解）ではない

    def test_a_missing_recording_sent_back_is_settled_as_usual(self):
        s = self.ready()
        aid = self.missing(s)
        out = self.send_back(s, aid)
        self.assertEqual(out["state"], "completed")
        self.assertEqual(self.dnc_reasons(s), [])
        self.assertFalse(self.pending(s, aid)["p"])
        self.assertEqual(self.released(s, aid), 1)

    def test_marks_only_on_the_current_recording_wait(self):
        """契約者に確かめてもらう印は、答えの無い中身のある、いまの録音の確かめ待ちの版にだけ（データベースが守る）。"""
        s = self.ready()
        aid = self.missing(s)
        put = ("INSERT INTO salescall.decision_asks (tenant_id, client_id, attempt_id, seq, why)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'recording_missing')")
        self.send_back(s, aid)                                                   # 答えた：もう待たない（中身が空の版）
        for seq in (1, 2, 3):
            with self.subTest(seq=seq), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(put, (aid, seq)), SYSTEM)
        s2 = self.ready()
        self.put_client(s2, "approval.human", True)
        self.tx(s2, lambda tx: approval.approve_task(tx, s2["task"]), OPERATOR)
        aid2 = self.call(s2, report=rep(dnc=True, check="agreed"))
        self.settle(s2, aid2)
        with self.assertRaises(Rejected):                                        # 検証モードの承認待ちには付かない
            self.tx(s2, lambda tx: tx.execute(put, (aid2, 1)))


class Review26(Helpers):
    """見直し26b（0161）：取り消しで、あとに発信のある行も戻る・差し戻した古い版を確定できない・確定の印の段とだれが付けるか・取り消しの
    答えの中身・保留の版は締めの入口だけ。"""

    def human(self, s):
        self.put_client(s, "approval.human", True)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)

    def releases(self, s):
        return self.tx(s, lambda tx: [(r["v"], r["b"], r["stage"]) for r in tx.all(
            "SELECT l.version_no AS v, r.business_state::text AS b, l.stage FROM salescall.result_releases l JOIN salescall.task_results r"
            " ON r.task_id = l.task_id AND r.version_no = l.version_no WHERE l.task_id = %s ORDER BY 1", (s["task"],))])

    def test_revoking_a_refusal_that_ended_a_row_with_a_later_dial_puts_the_row_back(self):
        """断りで行を終えた通話のあとに、同じ行の発信（出なかった）があっても、取り消せば行は予定に戻り、依頼者へ渡す結果も「営業拒否」で
        なくなる（2026-10-30 (7)。⛔ 見直し26b の 1：あとに発信があるので予定を書き直さず、行も結果も「営業拒否」のままだった）。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        aid = self.call(s, CUT, report=rep(cut=None), items=(("Q1", "dont_know", None),))
        self.settle(s, aid)
        later = self.call(s, ["dialing", "no_answer"])
        self.settle(s, later)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="disagreed"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        self.approve(s, aid)
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["kind"], cur["end_reason"]), ("end", "do_not_call"))
        self.assertEqual(self.releases(s)[-1][1], "do_not_call")
        self.tx(s, lambda tx: dnc.revoke_source(tx, aid, "依頼者からの正式な求め（試験）"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "call")   # 行は予定に戻る
        self.assertNotEqual(self.releases(s)[-1][1], "do_not_call")                                   # 渡す結果も外れる
        self.assertEqual(self.dnc_reasons(s), [])

    def test_an_old_version_cannot_be_released_after_sending_back(self):
        """差し戻したあとに、画面に残っていた前の断りの版を、結果の承認・直す入口・直接の印で確定できない（0161。⛔ 見直し26b の 2）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        v1 = self.settle(s, aid)["version_no"]
        self.send_back(s, aid)
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.release_blocked(%s, %s) AS b", (s["task"], v1)))["b"])
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: approval.approve_result(tx, s["task"], v1), OPERATOR)
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=v1, business_state="do_not_call",
                                                          wrong_items=[], complaint=False, snapshot={}), OPERATOR)
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by, stage)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor(), 'verification')",
                (s["task"], v1)), OPERATOR)
        self.assertEqual(self.releases(s), [])

    def test_the_stage_of_a_release_follows_the_mode_when_called_and_only_people_confirm_verification(self):
        """確定の印の段は、その版の通話をかけたときのモード（データベースの1か所）。検証の段を確定するのは契約者か管理者だけ（0161。⛔
        見直し26b の 3：検証モードの業務の版に「本番」の印を付けられ、システムの処理が承認で確定できた）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, ["dialing", "no_answer"])
        v = self.settle(s, aid)["version_no"]
        put = ("INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by, stage)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor(), %s)")
        for actor, stage in ((OPERATOR, "production"), (SYSTEM, "production"), (SYSTEM, "verification")):
            with self.subTest(actor=actor.kind, stage=stage), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(put, (s["task"], v, stage)), actor)
        with self.assertRaises(Rejected):                                        # システムの処理は、承認の入口でも確定できない
            self.tx(s, lambda tx: approval.approve_result(tx, s["task"], v), SYSTEM)
        self.put_client(s, "approval.human", False)                             # かけたあとにモードを替えても、かけたときのモードのまま
        self.assertEqual(self.tx(s, lambda tx: approval.after_judged(tx, s["task"], v, None)), [])
        self.tx(s, lambda tx: approval.approve_result(tx, s["task"], v), OPERATOR)
        self.assertEqual(self.releases(s), [(v, self.tx(s, lambda tx: results.latest(tx, s["task"]))["business_state"], "verification")])

    def test_a_revoke_answer_carries_only_the_revoked_entries_parts(self):
        """取り消しの答えの中身は、その通話から入れた発信禁止をすべて取り消したときの、その登録の理由から決まる中身だけ（0161。⛔ 見直し26b
        の 4：取り消していない「怒っていた」を答えに入れられ、その保留が確かめないまま閉じた）。"""
        s = self.ready()
        aid = self.answered_call(s, TALK[1:])
        self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        self.tx(s, lambda tx: call_report.save(tx, aid, rep(dnc=True, complaint=True, check="disagreed")))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.settle(s, aid)
        self.tx(s, lambda tx: dnc.revoke_source(tx, aid, "依頼者からの正式な求め（試験）"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT salescall.revoked_parts(%s) AS p", (aid,)))["p"], ["refusal"])
        put = ("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, outcome, parts, by_kind, why, answered_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'withdrawn', '{complaint}', 'revoke', '試験',"
               " salescall.cur_actor())")
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(put, (aid,)), OPERATOR)
        self.assertTrue(self.pending(s, aid)["p"])                                # 怒っていたの保留は、まだ決まっていない

    def test_a_sent_back_check_call_refusal_does_not_come_back(self):
        """検証モード：確かめのかけ直しが鳴って出なかった（断りとみなす）通話を差し戻したら、締め直しで断りが出直さない・前の断りの版を
        確定できない（見直し26b の 2・M1）。"""
        s = self.ready()
        self.human(s)
        self.override("gate.same_contact_min_interval", 1)
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="callee",
                        report=rep(cut=Cut("ai", False, False)))
        self.settle(s, aid)
        time.sleep(1.2)
        check = self.call(s, ("dialing", "ringing", "no_answer"))
        got = self.settle(s, check)
        self.assertEqual(got["state"], "do_not_call")
        v1 = got["version_no"]
        out = self.send_back(s, check)
        self.assertNotEqual(out["state"], "do_not_call")
        self.assertFalse(self.pending(s, check)["p"])                          # 断りは出直さない（保留が繰り返さない）
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: approval.approve_result(tx, s["task"], v1), OPERATOR)

    def test_a_refusal_decided_after_a_later_call_talked_keeps_the_later_calls_plan(self):
        """前の通話の断りが、あとでお客様と話した通話のあとに決まったら、発信禁止は入れるが、行の予定はあとの通話のまま（見直し24a の (1)(9)・
        見直し26b の M4）。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        aid = self.call(s, CUT, report=rep(cut=None), items=(("Q1", "dont_know", None),))
        self.settle(s, aid)
        later = self.call(s, report=rep())                                    # あとで、お客様と話した
        self.settle(s, later)
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, check="disagreed"), evidence="試験：録音から"))
        self.tx(s, lambda tx: settle.resettle(tx, aid))                       # 録音から作り直した報告で締め直す（断りは保留）
        self.approve(s, aid)
        after = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((after["seq"], after["kind"]), (before["seq"], before["kind"]))   # 行の予定は、あとの通話のまま
        self.assertIn("refused_in_call", self.dnc_reasons(s))                              # 発信禁止は入る

    def test_an_answer_is_by_the_person_of_the_transaction(self):
        """答えた者は取引の管理者か契約者（名前を偽れない。見直し26b の M2）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        put = ("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, wait_seq, outcome, parts, by_kind, why,"
               " answered_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'confirmed', '{refusal}', 'approval',"
               " '試験', 'ほかの契約者')")
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(put, (aid, self.seq(s, aid))), OPERATOR)

    def test_only_the_settling_entry_writes_a_wait(self):
        """保留の版を書くのは、その通話を締めている締めの入口だけ（0161。⛔ 見直し26b の 5：契約者が中身の空の版を直接書け、承認を通らずに
        保留が閉じた）。"""
        s = self.ready()
        self.human(s)
        aid = self.call(s, report=rep(dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.settle(s, aid)
        put = ("INSERT INTO salescall.decision_waits (tenant_id, client_id, attempt_id, seq, task_id, kind, parts, checked)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 2, %s, 'approval', '{}', 'agreed')")
        for actor in (OPERATOR, SYSTEM):
            with self.subTest(actor=actor.kind), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(put, (aid, s["task"])), actor)
        self.assertTrue(self.pending(s, aid)["p"])


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


    def test_revoking_a_contractor_entry_puts_the_rows_back_by_the_job(self):
        """契約者の共通の発信禁止を取り消したら、その登録で門が終えた行も予定に戻す（依頼者の台帳と同じ1か所 next_action.reopen_revoked。
        契約者の範囲の取り消しの取引からは依頼者の行を読めないので、見守り役の決まった間隔の仕事が、依頼者ごとに呼ぶ）。"""
        s = self.ready()
        mine = lambda work: self.w.a.in_tenant(OPERATOR, s["tenant"], work)   # noqa: E731
        got = mine(lambda tx: dnc.register_contractor(tx, s["phone"], "誤って登録した（試験）"))
        denied = self.claim(s)
        self.assertEqual(denied.reason, "dnc_contractor")
        self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "dnc_contractor")
        back = lambda: self.tx(s, lambda tx: next_action.reopen_revoked(tx, cause="試験：取り消しのあと"))   # noqa: E731
        self.assertEqual(back(), [])                                              # まだ取り消していない：戻さない
        mine(lambda tx: dnc.revoke_contractor(tx, got["entry_id"], "依頼者からの正式な求め（試験）"))
        self.assertEqual(back(), [s["task"]])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "call")
        self.assertEqual(back(), [])                                              # 同じ取り消しで、何度も戻さない
        mine(lambda tx: dnc.register_contractor(tx, s["phone"], "また登録した（試験）"))
        denied = self.claim(s)
        self.assertEqual(denied.reason, "dnc_contractor")
        self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        self.assertEqual(back(), [])                                              # 終えたあとの取り消しが無い：門がまた終えたまま


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

    def test_the_words_go_with_the_recording_days_but_held_calls_are_kept(self):
        """やり取りの文字は、録音と同じ「Fly.io に残す日数」（契約者ごと。初め7日）を過ぎたら消える（計画書 11-4。0157）。消さないのは録音と
        同じ（測り終わるまで・まだ決まっていない取り消せない決定）。消すのは、その入口だけ。"""
        worlds, calls = [self.ready(), self.ready()], []
        for s in worlds:
            aid = self.answered_call(s)
            self.tx(s, lambda tx, aid=aid: conversation_io.write_utterance(tx, aid, Utterance("callee", 1, "はい", 1.0)))
            for st in TALK[3:]:
                self.tx(s, lambda tx, aid=aid: attempts.move(tx, aid, st))
            calls.append(aid)
        (s, s2), (old, held) = worlds, calls
        self.tx(s2, lambda tx: tx.execute("INSERT INTO salescall.recording_holds (tenant_id, client_id, attempt_id, reason)"
                                          " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'verification')", (held,)))
        purge = lambda w: app_access().in_tenant(SYSTEM, w["tenant"], conversation_io.purge_utterances)   # noqa: E731
        self.assertEqual((purge(s), purge(s2)), (0, 0))                              # まだ7日たっていない
        owner_access().in_system(SYSTEM, lambda tx: (   # 8日前の通話にする（試験の場面を作るため、この取引の中だけ守りを外す）
            tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER"),
            tx.execute("UPDATE salescall.attempts SET ended_at = ended_at - interval '8 days',"
                       " claimed_at = claimed_at - interval '8 days' WHERE attempt_id = ANY(%s)", (calls,)),
            tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")))
        self.assertEqual((purge(s), purge(s2)), (1, 0))                              # 測り終わるまで消さない通話の分は残る
        self.assertEqual(self.tx(s, lambda tx: conversation_io.utterances(tx, old)), [])
        self.assertEqual(len(self.tx(s2, lambda tx: conversation_io.utterances(tx, held))), 1)
        with self.assertRaises(Rejected):                                          # 表の持ち主でも、入口の外からは消せない
            owner_access().in_tenant(SYSTEM, s2["tenant"], lambda tx: tx.execute(
                "DELETE FROM salescall.call_utterances WHERE attempt_id = %s", (held,)))
        with self.assertRaises(Rejected):                                          # 契約者の取引のシステムの処理だけ
            app_access().in_tenant(OPERATOR, s["tenant"], conversation_io.purge_utterances)

    def test_a_call_waiting_for_the_unclear_cut_check_and_an_unended_call_are_kept(self):
        """消さない通話は1か所（salescall.attempt_kept。0159）：切れ方の確かめのかけ直しを待つ通話（保留の表には無い）も、録音の片付け・
        やり取りの文字を消す入口・その守りの3つで同じに残す。⛔ 見直し25a の D：前は、その1件で、契約者のやり取りの文字を消す仕事が毎日
        まるごと失敗し、録音の片付けはその通話の録音を消してよいとしていた。終わっていない通話の文字は、日数に数えない（E）。"""
        s = self.ready()
        waiting, open_call = self.answered_call(s), None
        self.tx(s, lambda tx: conversation_io.write_utterance(tx, waiting, Utterance("callee", 1, "はい", 1.0)))
        for st in TALK[3:]:
            self.tx(s, lambda tx: attempts.move(tx, waiting, st))
        self.tx(s, lambda tx: tx.execute("INSERT INTO salescall.unclear_checks (tenant_id, client_id, task_id, attempt_id)"
                                         " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s)", (s["task"], waiting)))
        s2 = self.ready()
        open_call = self.answered_call(s2)                                      # 終わっていない（固まった）通話
        self.tx(s2, lambda tx: conversation_io.write_utterance(tx, open_call, Utterance("callee", 1, "はい", 1.0)))
        owner_access().in_system(SYSTEM, lambda tx: (
            tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER"),
            tx.execute("UPDATE salescall.attempts SET ended_at = ended_at - interval '8 days',"
                       " claimed_at = claimed_at - interval '8 days' WHERE attempt_id = ANY(%s)", ([waiting, open_call],)),
            tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")))
        purge = lambda w: app_access().in_tenant(SYSTEM, w["tenant"], conversation_io.purge_utterances)   # noqa: E731
        self.assertEqual((purge(s), purge(s2)), (0, 0))                          # どちらも残る（仕事も落ちない）
        folder = backup.client_folder(self.w.a, s["tenant"], s["client"])          # 録音の片付けも、同じく消さない
        self.assertIn(str(waiting), backup.prune_rules(self.w.a, s["tenant"])[folder]["held"])
        with self.assertRaises(Rejected):                                        # 守りも同じ1か所（消す印を立てても消せない）
            owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: (
                tx.execute("SET LOCAL salescall.purging_utterances = 'on'"),   # 消す処理の印（契約者・誰が、の印ではない）
                tx.execute("DELETE FROM salescall.call_utterances WHERE attempt_id = %s", (waiting,))))


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


CUT = ("dialing", "ringing", "answered", "in_conversation", "technical_error")


class ByPart(Helpers):
    """見直し24 の a・b・c で見つかった場面（保留と答えは中身ごと・報告が変われば決め直す・同じ行も止める・取り消しの重なり・守り）。"""

    def test_revoking_the_declaration_does_not_close_the_complaint_wait(self):
        """通話の途中の申告（台帳にその場で入る）＋そろわない怒っていた ⇒ 怒っていたは保留。申告の発信禁止を取り消しても、怒っていたの
        保留は閉じない（答えは中身ごと。⛔ 見直し24a の (3)・24b の (A)：前は、取り消しの答えで保留が閉じ、確かめないまま苦情で行を終えた）。"""
        s = self.ready()
        aid = self.answered_call(s, TALK[1:])
        self.assertFalse(self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))["held"])
        self.tx(s, lambda tx: call_report.save(tx, aid, rep(dnc=True, complaint=True, check="disagreed")))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.assertEqual(self.settle(s, aid)["decision"].kind, "hold")
        self.assertEqual([w["parts"] for w in self.tx(s, approval.pending_decisions)], [["complaint"]])
        self.tx(s, lambda tx: dnc.revoke_source(tx, aid, "依頼者からの正式な求め（試験）"), OPERATOR)
        self.assertTrue(self.pending(s, aid)["p"])
        self.assertEqual([w["parts"] for w in self.tx(s, approval.pending_decisions)], [["complaint"]])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "hold")
        self.assertEqual(self.notices(s, "complaint"), 0)

    def test_a_wait_found_by_rejudging_an_earlier_call_stops_the_same_row(self):
        """途中で切れて判定できなかった通話 → かけ直しに出なかった → 録音から作り直した報告で前の通話に「そろわない断り」⇒ 保留。あとの発信が
        あっても、同じ行からそのお客様にかけない（門。⛔ 見直し24a の (2)）。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        aid = self.call(s, CUT, report=rep(cut=None), items=(("Q1", "dont_know", None),))
        self.assertEqual(self.settle(s, aid)["state"], "unknown")
        later = self.call(s, ["dialing", "no_answer"])
        self.settle(s, later)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc=True, cut=None, check="disagreed"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        self.assertTrue(self.pending(s, aid)["p"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：予定の時刻が来た"))
        self.assertEqual(self.reason(s), "related_decision_pending")
        # 契約者が録音とやり取りを確かめて承認した：あとの発信は出なかっただけなので、前の通話の断りで行を終える（行の結果も断り）
        self.assertEqual(self.asks(s), [(True, "recording_undecided")])
        self.approve(s, aid)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "do_not_call")
        self.assertEqual(self.released(s, aid), 1)
        self.assertEqual(self.dnc_reasons(s), ["refused_in_call"])

    def test_revoking_an_overlapping_entry_keeps_the_later_refusal(self):
        """お客様が言った別の番号 A が番号違い（A を止めた。出どころはその通話）→ 一覧の番号へ聞き直し → 断った（止める範囲に A も入る）。
        番号違いの登録を取り消しても、断りで A は止まったまま。行の結果も「営業拒否」のまま（⛔ 見直し24a の (1)）。"""
        from salescall.core import number_recheck
        from tests.test_alternate import other as other_report
        s = self.ready(phone=fictional_jp_fixed())
        self.override("gate.same_contact_min_interval", 1)
        said = fictional_jp_fixed()
        alt = normalize(said).e164
        first = self.call(s, report=other_report(said))
        self.settle(s, first)
        wrong = self.call(s, report=rep(("wrong_party",)), verify=None)
        self.tx(s, lambda tx: number_recheck.record(tx, wrong, "as_said", corrected=None, evidence="試験：付け合わせた",
                                                    checked_by="試験"))
        self.tx(s, lambda tx: attempts.record_verification(tx, wrong, summary=CLEAN))
        self.assertEqual(self.settle(s, wrong)["state"], "not_reached")
        refused = self.call(s, report=rep(dnc=True, check="agreed"))
        self.assertEqual(self.settle(s, refused)["state"], "do_not_call")
        self.tx(s, lambda tx: dnc.revoke_source(tx, wrong, "依頼者からの正式な求め（試験）：番号違いではなかった"), OPERATOR)
        hit = self.tx(s, lambda tx: tx.one("SELECT reason::text AS r, source_attempt_id FROM salescall.dnc_entries"
                                           " WHERE phone_e164 = %s AND revoked_at IS NULL", (alt,)))
        self.assertEqual((hit["r"], hit["source_attempt_id"]), ("refused_in_call", refused))
        latest = self.tx(s, lambda tx: results.latest(tx, s["task"]))
        released = self.tx(s, lambda tx: tx.one("SELECT max(version_no) AS v FROM salescall.result_releases WHERE task_id = %s",
                                                (s["task"],)))["v"]
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT business_state::text AS b FROM salescall.task_results"
                                                      " WHERE task_id = %s AND version_no = %s", (s["task"], released)))["b"],
                         "do_not_call")                                          # 依頼者へ渡す結果は、断りのまま
        self.assertNotEqual(latest["attempt_id"], refused)                       # 前の通話を締め直した版は足すが、確定しない

    def test_a_revoke_does_not_report_a_row_still_stopped(self):
        """行の番号のうち、今かける番号でない番号の登録だけを取り消した：まだ止まっている行を戻さない（門と同じかけない理由の1か所で
        確かめる。⛔ 見直し24b の (C)）。"""
        s = self.ready()
        aid = self.call(s, report=rep(), items=[("Q1", "confirmed", None)])
        extra = normalize(fictional_jp_fixed()).e164
        self.tx(s, lambda tx: tasks.add_callee_number(tx, s["task"], normalize(extra), source_attempt_id=aid))
        self.settle(s, aid)
        e_extra = self.tx(s, lambda tx: dnc.register(tx, extra, "manual"), OPERATOR)
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "manual"), OPERATOR)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：いま"))
        denied = self.claim(s)
        self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        out = self.tx(s, lambda tx: dnc.revoke(tx, e_extra["entry_id"], "試験：別の番号だけ取り消す"), OPERATOR)
        self.assertEqual(out["reopened"], [])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "end")

    def test_a_check_call_that_hit_power_off_or_busy_did_not_ring(self):
        """確かめのかけ直しが、電源オフ・圏外のお知らせ・話し中で終わった：お客様の電話は鳴っていない＝営業拒否にしない（15分おいて、
        お詫びからかけ直す。2026-10-30 管理者のご決定 (3)。⛔ 見直し24c の A1：前は「鳴った」として営業拒否にした）。"""
        for how in ("power_off", "out_of_range", "busy"):
            with self.subTest(how):
                s = self.ready()
                self.override("gate.same_contact_min_interval", 1)
                aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="callee",
                                report=rep(cut=Cut("ai", False, False)))
                self.settle(s, aid)
                time.sleep(1.2)                                                  # 同じお客様への最短の間隔（試験では1秒）
                c = self.claim(s)
                self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
                if how == "busy":
                    self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, "busy"))
                else:
                    self.tx(s, lambda tx: attempts.end_by_notice(tx, c.attempt_id, how))
                got = self.settle(s, c.attempt_id)
                self.assertEqual(self.dnc_reasons(s), [])
                self.assertEqual(self.tx(s, lambda tx: next_action.check_outcome(tx, c.attempt_id)).outcome, "unrung")
                self.assertNotEqual(got["state"], "do_not_call")

    def test_a_rebuilt_report_that_cannot_be_judged_is_told(self):
        """保留のあとに作り直した報告が判定できない：判定できない知らせ（だれにも知らせずに待たない。⛔ 見直し24a の (6)）。"""
        s = self.ready()
        aid = self.call(s, report=rep(dnc=True, check="disagreed"), items=[("Q1", "dont_know", None)])
        self.settle(s, aid)
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(check="agreed"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        self.assertGreaterEqual(self.notices(s, "judge_unknown"), 1)
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["business_state"], "unknown")   # 判定し直しを待つ
        self.assertFalse(self.pending(s, aid)["p"])                               # 断りは報告から消えた：保留は閉じた

    def test_guards(self):
        """データベースの守り（見直し24a の (8)）：取り消しの答えは発信禁止をすべて取り消したときだけ・保留はその通話の行で、版は順に・
        やり取りの文字は通話後の検証が済む前だけ。"""
        s = self.ready()
        aid = self.call(s, report=rep(dnc=True, check="disagreed"))
        self.settle(s, aid)
        revoke = ("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, outcome, parts, by_kind, why, answered_by)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'withdrawn', '{refusal}', 'revoke', '試験',"
                  " salescall.cur_actor())")
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(revoke, (aid,)), OPERATOR)
        wait = ("INSERT INTO salescall.decision_waits (tenant_id, client_id, attempt_id, seq, task_id, kind, parts, checked)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, 'recording', '{refusal}', 'none')")
        other = self.other_row(s)
        for seq, task in ((2, other), (5, s["task"])):   # 締めの入口の印を立てても（ほかの守りが断る）
            with self.subTest(seq=seq), self.assertRaises(Rejected):
                self.tx(s, lambda tx: (tx.execute("SELECT salescall.settling(%s)", (aid,)), tx.execute(wait, (aid, seq, task))))
        words = ("INSERT INTO salescall.call_utterances (tenant_id, client_id, attempt_id, seq, speaker, turn, said, at_s)"
                 " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 99, 'callee', 1, 'またかけてください（試験）', 1.0)")
        with self.assertRaises(Rejected):                                        # 検証が済んだあと
            self.tx(s, lambda tx: tx.execute(words, (aid,)))
