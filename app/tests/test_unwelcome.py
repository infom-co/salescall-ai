"""お客様から見て煙たがられないかけ方の場面の試験（✅ 2026-11-08 ユーザー＝計画書 7-3「お客様から見て煙たがられないかけ方」(1)(2)。0216。
DESIGN.md 5-2・7-2 の【0215】。持ち主 CL-2）。本物の門・出口・締めの処理を通す。時計を進める場面（(3) 幅の約束・(4) かけられなかった約束）は
tests/test_life.py の `CallsNotUnwelcome`。(5)(6) は test_apology・test_alternate・test_settle の該当の場面。
⚠️ 試験のデータは架空のものだけ。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from salescall.core import apology, attempts, business_rules, gate, next_action, notify, quality_stage, scheduler
from tests.support import OPERATOR, callback, make_report, rules_raw, said
from tests.test_apology import ApologyBase
from tests.test_judge import goal_refused
from tests.test_settle import SettleBase, rep


class SameNumberOtherRows(SettleBase):
    """(1) 同じ番号へ、ほかの業務・ほかの実施回・ほかの依頼者からかける電話の間は1つの決まり（依頼者ごとの設定 gate.same_number_gap。初めの値は
    「その日はかけない」）。用件の断り・苦情のあとは、その依頼者からはかけない（依頼者ごとの設定 calling.call_after_refusal）。声と発信番号は
    必ず替える。ほかの契約者とまたぐ間隔（契約者の設定 gate.other_contractor_interval＝計画書 1-4）は別の決まりのまま。
    ⛔ 0215 まで：同じ依頼者なら同じ番号への最短の間隔5分・1日2回だけで、断られた・怒られた10分後に同じ会社名でほかの業務の電話が鳴った。"""

    def sibling(self, s, *, business=True):
        """同じ依頼者の、同じ番号のお客様の、ほかの行（business＝ほかの業務。偽なら同じ業務のほかの実施回）。いまかける予定にし、回線を1本足す。"""
        if business:
            b, v = self.w.business_with_version(s["tenant"], s["client"])
            self.tx(s, lambda tx: quality_stage.open_stage(tx, v, 1, None, evidence="試験"), OPERATOR)
            self.tx(s, lambda tx: business_rules.save(tx, v, rules_raw()), OPERATOR)
        else:
            b, v = s["business"], s["version"]
        run = self.w.run(s["tenant"], s["client"], b, v)
        task = self.w.task(s["tenant"], s["client"], run, s["contact"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, task, cause="試験：ほかの行の最初の予定"))
        y = dict(s, business=b, version=v, run=run, task=task, line=self.w.line(s["tenant"]))
        self.lease(y)
        return y

    def tomorrow(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.jst_day_start(salescall.jst(now())::date + 1) AS t")["t"])

    def denied_and_ended(self, y, reason):
        denied = self.claim(y)
        self.assertEqual(getattr(denied, "reason", denied), reason)
        self.tx(y, lambda tx: next_action.after_denial(tx, y["task"], denied, tenant_id=y["tenant"], client_id=y["client"],
                                                       business_id=y["business"]))   # 門の答えを受けた所と同じく
        self.assertEqual(self.tx(y, lambda tx: next_action.current(tx, y["task"]))["end_reason"], reason)   # 行を終える

    def test_after_a_refusal_the_client_does_not_call_from_another_business_unless_the_contractor_changes_it(self):
        """業務X で用件を断られた ⇒ 同じ依頼者の業務Y（同じ番号）は、もうかけない（行を終える）。契約者が依頼者ごとの設定を「かける」に変えれば、
        ほかの電話の間の決まり（その日はかけない＝次の日の、かけてよい時間帯の始まり）でかける。"""
        s = self.ready()
        y = self.sibling(s)
        got = self.settle(s, self.call(s, report=goal_refused(rep())))          # 業務X：「うちは要らない」
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "declined"))
        self.denied_and_ended(y, "declined_in_client")
        self.put_client(s, "calling.call_after_refusal", True)                    # 契約者が、この依頼者だけ「かける」に
        z = self.sibling(s)
        self.assertEqual(self.reason(z), "same_number_interval")
        p = self.tx(z, lambda tx: gate.pacing(tx, z["task"]))
        self.assertEqual((p.binding, p.earliest), ("same_number_interval", self.tomorrow(s)))   # 試験の時間帯は1日じゅう：次の日の0時

    def test_after_a_complaint_no_business_or_run_of_the_client_calls_again(self):
        """苦情のあとは、その依頼者のどの業務・どの実施回からもかけない（同じ業務の次の実施回も。用件の断りの同じ業務の次の実施回は、業務の設定
        business.call_declined_again が受け持つ）。"""
        s = self.ready()
        y, again = self.sibling(s), self.sibling(s, business=False)
        got = self.settle(s, self.call(s, report=make_report(complaint=True)))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "complaint"))
        self.denied_and_ended(y, "declined_in_client")
        self.denied_and_ended(again, "declined_in_client")

    def test_another_call_of_the_client_waits_until_the_next_day_and_the_setting_changes_it(self):
        """業務X の電話に出なかった ⇒ 業務Y・業務X のほかの実施回は、その日はかけない（次の日から）。依頼者ごとの設定を1時間にすれば、1時間あと。
        お客様と約束したかけ直しは待たない。⛔ 0215 まで：5分あけて同じ日に、1日2回までかけた。"""
        s = self.ready()
        y, again = self.sibling(s), self.sibling(s, business=False)
        aid = self.call(s, ["dialing", "no_answer"])
        self.settle(s, aid)
        for row in (y, again):
            self.assertEqual(self.reason(row), "same_number_interval")
            self.assertEqual(self.tx(row, lambda tx: gate.pacing(tx, row["task"])).earliest, self.tomorrow(s))
        self.put_client(s, "gate.same_number_gap", {"hours": 1})
        claimed = self.tx(s, lambda tx: tx.one("SELECT claimed_at FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["claimed_at"]
        self.assertEqual(self.tx(y, lambda tx: gate.pacing(tx, y["task"])).earliest, claimed + timedelta(hours=1))
        self.tx(y, lambda tx: next_action._append(tx, y["task"], call_at=tx.one("SELECT now() AS t")["t"], end_reason=None,
                                                  cause="試験：お客様と約束したかけ直し", promised=True,
                                                  target=next_action.Target(y["contact"])))
        self.assertIsInstance(self.claim(y), gate.Claim)                          # 約束の電話は待たない

    def test_the_voice_and_the_caller_number_change_between_businesses_of_the_client(self):
        """同じ依頼者のほかの業務の電話も、お客様が前に聞いた声・前に見た発信番号（その番号へのいちばん新しい電話）と替える（声と発信番号は必ず
        替える）。⛔ 0215 まで：同じ依頼者なら同じ声・同じ発信番号でかけた。"""
        s = self.ready()
        y = self.sibling(s)
        aid = self.call(s)                                                        # 業務X：お客様が出て話した
        self.settle(s, aid)
        first = self.tx(s, lambda tx: tx.one("SELECT voice_type FROM salescall.attempts WHERE attempt_id = %s", (aid,)))
        self.tx(y, lambda tx: next_action._append(tx, y["task"], call_at=tx.one("SELECT now() AS t")["t"], end_reason=None,
                                                  cause="試験：お客様と約束したかけ直し（間を待たない）", promised=True,
                                                  target=next_action.Target(y["contact"])))
        self.lease(y, line=s["line"])
        self.assertEqual(self.reason(y, line=s["line"]), "line_used_by_other_client")   # 前に見た発信番号ではかけない
        offered = lambda line: self.tx(y, lambda tx: [r["task_id"] for r in scheduler.due_tasks(tx, 10, line_id=line)])  # noqa: E731
        self.assertEqual(offered(s["line"]), [])
        second = self.claim(y)                                                    # ほかの回線（発信番号）で
        self.assertIsInstance(second, gate.Claim, second)
        self.assertNotEqual(second.voice_type, first["voice_type"])               # 前に聞いた声と替える

    def test_another_client_follows_its_own_setting_and_another_contractor_keeps_its_own_rule(self):
        """ほかの依頼者がかけたあとも同じ決まり（かける側の依頼者の設定。初めはその日はかけない）。ほかの契約者がかけたあとは、契約者の設定の間
        （初め3日。時刻は1時間単位に切り上げ＝中身を出さない）のまま（混ぜない）。"""
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])                      # 同じ契約者の、ほかの依頼者
        other = self.ready(phone=a["phone"])                                      # ほかの契約者
        aid = self.call(a, ["dialing", "no_answer"])
        self.settle(a, aid)
        self.assertEqual((self.reason(b), self.tx(b, lambda tx: gate.pacing(tx, b["task"])).earliest), ("same_number_interval", self.tomorrow(a)))
        self.put_client(b, "gate.same_number_gap", {"hours": 2})                 # かける側（b）の依頼者の設定
        claimed = self.tx(a, lambda tx: tx.one("SELECT claimed_at FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["claimed_at"]
        self.assertEqual(self.tx(b, lambda tx: gate.pacing(tx, b["task"])).earliest, claimed + timedelta(hours=2))
        self.put_client(other, "gate.same_number_gap", {"hours": 1})             # ほかの契約者には効かない（別の決まり）
        p = self.tx(other, lambda tx: gate.pacing(tx, other["task"]))
        self.assertEqual(p.binding, "cross_client_interval_other_tenant")
        self.assertGreater(p.earliest - claimed, timedelta(days=3) - timedelta(seconds=5))


class PromiseAndApology(ApologyBase):
    """(2) 言ってはいけないことを言った通話で、お客様がかけ直しを約束していた ⇒ お詫びは約束の時刻の電話の頭で伝え、そのまま約束の用件を続ける
    （約束を守り、1本で済ませる）。お詫びの承認（契約者）を待つ決まりはいままでと同じ（承認するまで、その約束の電話もかけない）。
    ⛔ 0215 まで：言ってはいけないことで先に行を終え、お詫びの電話だけかけて apology_done で終わり、約束の電話がかからなかった。"""

    def promise_call(self, s, *, report):
        """言ってはいけないこと（F1）を1件言った通話（質問の答えはまだ取れていない）を締める。"""
        aid = self.call(s, verify=None, report=report)
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))   # 言ってはいけないこと（F1）を1件
        return aid, self.settle(s, aid)

    def test_the_apology_is_said_at_the_head_of_the_promised_call(self):
        s = self.fresh(stage_cap=20)
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '1 day')::date AS d")["d"])
        aid, got = self.promise_call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日の15時")))
        d = got["decision"]
        self.assertEqual((d.kind, d.promised, d.target.purpose), ("call", True, "normal"))   # 行を終えない（約束を守る）
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["kind"], p["purpose"], p["apology_reasons"], p["callee_at"] is not None), ("call", "normal", ["forbidden_speech"], True))
        self.assertTrue(self.tx(s, lambda tx: gate.apology_unapproved(tx, s["task"])))
        self.assertEqual(self.tx(s, lambda tx: gate.waits_unknown(tx, s["task"])), "apology_unapproved")   # 候補に出さない
        seq = p["seq"]
        self.order(s, aid)                                                       # 契約者が案（と録音）を確かめて承認
        self.assertFalse(self.tx(s, lambda tx: gate.apology_unapproved(tx, s["task"])))
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["seq"], p["purpose"]), (seq, "normal"))              # お詫びだけの電話は書かない（約束の電話のまま）
        # 約束の時刻が来た（試験の近道：同じ約束の電話の予定を、いまにする）
        self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=tx.one("SELECT now() AS t")["t"], end_reason=None,
                                                  cause="試験：約束の時刻", promised=True, target=next_action.Target(s["contact"]),
                                                  apology="forbidden_speech"))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual((c.promised, c.purpose, "forbidden_speech" in c.apology), (True, "normal", True))   # 会話は頭でお詫び
        self.assertEqual(self.tx(s, lambda tx: apology.for_task(tx, s["task"]))["order_id"], self.draft(s, aid)["order_id"])
        got = self.settle(s, self.call_claimed(s, c))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "completed"))   # 約束の用件を続けて果たした
        notes = self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'apology'"
                                             " AND refs ->> 'task_id' = %s", (str(s["task"]),)))
        self.assertEqual(len(notes), 1)
        self.assertIn("約束した電話の頭で、お詫びを伝えました", notes[0]["summary"])
        self.assertIn("apology", notify.KINDS)

    def call_claimed(self, s, c):
        """予約した電話をかけ、目的のお客様と話して Q1 が取れた（締めはまだ）。"""
        from salescall.core import call_report, dial_exit, health
        from tests.support import SYSTEM, CLEAN
        from tests.test_gate import FakeDriver
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in ("answered", "in_conversation", "ending", "ended"):
            self.tx(s, lambda tx, st=st: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition, attempt_id)"
            " VALUES (%s, %s, %s, %s, 'Q1', 'confirmed', NULL, %s)", (s["tenant"], s["client"], s["task"], s["version"], c.attempt_id)))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep()))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        return c.attempt_id

    def test_a_refusal_with_the_mistake_ends_the_row_as_before(self):
        """「もうかけないで」と言われた通話で言ってはいけないことを言った ⇒ いままでどおり行を終え、お詫びの電話もかけない（発信禁止）。"""
        s = self.fresh(stage_cap=20)
        aid, got = self.promise_call(s, report=rep(dnc_=True))
        self.assertEqual(got["decision"].kind, "end")
        self.order(s, aid)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "end")   # お詫びの電話も書かない

    def test_without_a_promise_the_apology_call_is_as_before(self):
        """約束の無い通話で言ってはいけないことを言った ⇒ いままでどおり行を終え、承認でお詫びの電話（目的 apology）を書く。"""
        s = self.fresh(stage_cap=20)
        aid, got = self.promise_call(s, report=rep())
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "forbidden_speech"))
        self.order(s, aid)
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["kind"], p["purpose"], p["apology_reasons"]), ("call", "apology", []))


if __name__ == "__main__":
    unittest.main()
