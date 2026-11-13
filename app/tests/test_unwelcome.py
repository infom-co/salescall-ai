"""お客様から見て煙たがられないかけ方の場面の試験（✅ 2026-11-08 ユーザー＝計画書 7-3「お客様から見て煙たがられないかけ方」(1)(2)。0216。
DESIGN.md 5-2・7-2 の【0215】。持ち主 CL-2）。本物の門・出口・締めの処理を通す。時計を進める場面（(3) 幅の約束・(4) かけられなかった約束）は
tests/test_life.py の `CallsNotUnwelcome`。(5)(6) は test_apology・test_alternate・test_settle の該当の場面。
⚠️ 試験のデータは架空のものだけ。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from salescall.core import apology, attempts, business_rules, gate, next_action, notify, quality_stage, registry, scheduler
from tests.support import ADMIN, OPERATOR, callback, make_report, rules_raw, said
from tests.test_apology import ApologyBase
from tests.test_gate import ALL_DAY
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

    def promised_now(self, y):
        """その行の予定を、いまのお客様と約束したかけ直しにする（同じ番号へのほかの電話の間を待たない）。"""
        self.tx(y, lambda tx: next_action._append(tx, y["task"], call_at=tx.one("SELECT now() AS t")["t"], end_reason=None,
                                                  cause="試験：お客様と約束したかけ直し（間を待たない）", promised=True,
                                                  target=next_action.Target(y["contact"])))

    def notices(self, s, kind):
        return self.tx(s, lambda tx: [n["summary"] for n in tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = %s AND refs ->> 'client_id' = %s", (kind, str(s["client"])))])

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
        # 待つのは次の日の、かけてよい時間帯の始まりまで（次の日の0時にしない＝門の1日の回数の待ちと同じ gate.next_opening の1か所）
        self.put_client(s, "calling.hours", {k: [["09:00", "18:00"]] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")})
        opening = self.tx(y, lambda tx: gate.next_opening(tx, self.tomorrow(s), tenant_id=y["tenant"], client_id=y["client"],
                                                          business_id=y["business"]))
        self.assertEqual(self.tx(y, lambda tx: gate.pacing(tx, y["task"])).earliest, opening)
        self.put_client(s, "calling.hours", ALL_DAY)
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

    def test_a_client_whose_lines_all_were_seen_tells_the_contractor_and_a_new_line_calls(self):
        """依頼者の専用の回線が1本（L）で、ほかの回線が無い：業務X が L でかけた番号へ、業務Y は発信番号を替えられないので待ち、契約者に1回
        知らせる（line_none_to_switch。門と候補を出す処理が何度見ても、同じ依頼者には1日に1つ＝gate._no_line_to_switch の1か所）。共有の回線が
        替えられるあいだは知らせない（その回線が空けばかける）。回線を足すと、Y はその回線でかける。⛔ 0216 まで：知らせは専用の回線が無いとき
        だけで、Y は知らせの無いまま待ち続けた。"""
        s = self.ready()
        self.tx(s, lambda tx: registry.assign_line(tx, s["line"], s["client"]), OPERATOR)   # 依頼者の専用の回線 L
        y = self.sibling(s)                                                       # 業務Y（共有の回線 S を1本足す）
        self.settle(s, self.call(s))                                              # 業務X：L でかけ、お客様が出て話した
        self.promised_now(y)
        self.lease(y, line=s["line"])
        offered = lambda line: self.tx(y, lambda tx: [r["task_id"] for r in scheduler.due_tasks(tx, 10, line_id=line)])  # noqa: E731
        self.assertEqual(self.reason(y, line=s["line"]), "line_used_by_other_client")
        self.assertEqual(self.notices(s, "line_none_to_switch"), [])             # S なら替えられる：知らせない
        self.tx(s, lambda tx: registry.retire_line(tx, y["line"], reason="試験：端末を返した"), OPERATOR)   # 替えられる回線が無くなった
        for _ in range(2):
            self.assertEqual(self.reason(y, line=s["line"]), "line_used_by_other_client")
            self.assertEqual(offered(s["line"]), [])
        self.assertEqual(self.notices(s, "line_none_to_switch"), [gate.LINE_NONE_TO_SWITCH])   # 1回だけ
        self.assertNotIn(gate.OWN_LINE_MISSING, self.notices(s, "setting_missing"))   # 専用の回線はある（専用の発信番号が無い、ではない）
        new = self.w.line(s["tenant"])
        self.tx(s, lambda tx: registry.assign_line(tx, new, s["client"]), OPERATOR)   # 契約者が回線を足した
        self.lease(y, line=new)
        self.assertIsInstance(self.claim(y, line=new), gate.Claim)               # Y は足した回線でかける

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
        # 約束の時刻が来ても、承認するまで門が通さない（試験の近道：同じ約束の電話の予定を、いまにする）
        self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=tx.one("SELECT now() AS t")["t"], end_reason=None,
                                                  cause="試験：約束の時刻", promised=True, target=next_action.Target(s["contact"]),
                                                  apology="forbidden_speech"))
        self.assertEqual(self.reason(s), "apology_unapproved")
        seq = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
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
        # 会話へ渡す承認したお詫びの文は、約束の電話の頭のお詫びの欄（apology_text。お詫びだけの電話の apology_call ではない＝CL-1 の連絡帳 4-126 の (2)）
        self.assertEqual((c.apology_text, c.apology_call), (self.tx(s, lambda tx: apology.for_task(tx, s["task"]))["text"], None))
        got = self.settle(s, self.call_claimed(s, c, delivered=True))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "completed"))   # 約束の用件を続けて果たした
        notes = self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'apology'"
                                             " AND refs ->> 'task_id' = %s", (str(s["task"]),)))
        self.assertEqual(len(notes), 1)
        self.assertIn("約束した電話の頭で、お詫びを伝えました", notes[0]["summary"])
        self.assertIn("apology", notify.KINDS)

    def call_claimed(self, s, c, *, delivered, taken=True, report=None):
        """予約した電話をかけ、目的のお客様と話して（taken なら Q1 が取れた。締めはまだ）、会話の報告に、お詫びの文面を伝えたか（delivered）を書く。"""
        from salescall.core import call_report, dial_exit, health
        from tests.support import SYSTEM, CLEAN
        from tests.test_gate import FakeDriver
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in ("answered", "in_conversation", "ending", "ended"):
            self.tx(s, lambda tx, st=st: attempts.move(tx, c.attempt_id, st))
        if taken:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition, attempt_id)"
                " VALUES (%s, %s, %s, %s, 'Q1', 'confirmed', NULL, %s)", (s["tenant"], s["client"], s["task"], s["version"], c.attempt_id)))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, replace(report or rep(), apology_delivered=delivered)))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        return c.attempt_id

    def test_the_mark_stays_until_the_apology_is_delivered(self):
        """CL-1 の連絡帳 4-143 の頼み：言ってはいけないことのお詫びの印（予定の印 forbidden_speech）を外すのと、契約者への知らせ（「お客様と約束した電話の頭で、
        お詫びを伝えました」）は、会話の報告がお詫びを伝えた（apology_delivered）ときだけ（お詫びの電話の締めと同じ材料＝settle._apology_delivered。決めるのは
        next_action._forbidden_told の1か所）。会話の側は、代わりの方・前の電話でお話ししていない方にはお詫びを流さない ⇒ 目的のお客様と話せても、流していない
        電話のあとは印が残り、次に話せた電話の頭でお詫びを流す。⛔ 2026-11-13 まで：目的のお客様と話せたら外し、流していないのに契約者に「伝えました」と知らせた。"""
        import time
        s = self.fresh(stage_cap=20)
        self.put_client(s, "gate.same_contact_min_interval", 1)
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '1 day')::date AS d")["d"])
        aid, _ = self.promise_call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日の15時")))
        self.order(s, aid)                                                       # 契約者が案を承認
        notes = lambda: self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'apology'"   # noqa: E731
                                                     " AND refs ->> 'task_id' = %s", (str(s["task"]),)))
        for delivered, reasons, told in ((False, ["forbidden_speech"], 0), (True, [], 1)):
            with self.subTest(delivered=delivered):
                time.sleep(1.1)
                self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=tx.one("SELECT now() AS t")["t"], end_reason=None,
                                                          cause="試験：約束の時刻", promised=True, target=next_action.Target(s["contact"])))
                c = self.claim(s)
                self.assertIsInstance(c, gate.Claim, c)
                self.assertIn("forbidden_speech", c.apology)                     # 会話は頭でお詫び（流すかは会話の側が決める）
                got = self.settle(s, self.call_claimed(s, c, delivered=delivered, taken=False))   # 目的のお客様と話せた・Q1 はまだ
                self.assertEqual(got["decision"].kind, "call")
                self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"], reasons)
                self.assertEqual(len(notes()), told)

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


class OldPersonAfterAnotherFix(ApologyBase):
    """(5) 番号を「違う」で直したときの、前の担当の方へのお詫び・番号の聞き直しの回数（✅ 2026-11-08 本体の決定＝計画書 7-1 (32) にそろえる）：
    名前の直しと同じく、決めた回数まで前の担当の方へかけ、使い切ったら新しい担当の方へ。「同じ」担当の方の番号の直しは、前の番号へ1回のまま
    （計画書 7-1 (17)(18)）。決める所は next_action._after_former の1か所（前の番号へ1回で移すのは `_old_number_moves`＝同じ担当の方の期の予定だけ）。
    ⛔ 0201〜0215：番号の直しだけ、前の番号へ1回で新しい担当の方へ移った。"""

    def test_the_apology_to_the_old_person_counts_every_call_to_them(self):
        """「違う」で直す前に前の担当の方へかけたお詫びの電話も、決めた回数（retry.apology_max＝ここでは2）に数える（直したときから数え直さない＝
        attempts.in_purpose_count）。直したときに1本かけたあとでも、新しい番号へ移さない。2本目で使い切って、新しい担当の方へ引き継ぎ。"""
        import time
        from dataclasses import replace
        from salescall.core import row_fixes
        from tests import support
        from tests.support import fictional_jp_fixed
        s = self.fresh(stage_cap=20)
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "retry.apology_max", 2)
        aid = self.call(s, verify=None, items=())
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
        self.settle(s, aid)
        self.order(s, aid)
        unreached = replace(rep(("reception",), not_reached="absent"), apology_delivered=False)
        time.sleep(1.1)
        self.settle(s, self.call(s, report=unreached))                           # 1本目（直す前）：届かなかった
        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=False), OPERATOR)["contact_id"]
        plan = lambda: self.tx(s, lambda tx: next_action.current(tx, s["task"]))   # noqa: E731
        self.assertEqual((plan()["purpose"], plan()["contact_id"]), ("apology", s["contact"]))   # 前の担当の方へ（1本で移さない）
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], tx.one("SELECT now() AS t")["t"], contact_id=s["contact"],
                                                     cause="試験：別の時間帯を待たずに", promised=True, purpose="apology"))
        time.sleep(1.1)
        self.settle(s, self.call(s, report=unreached))                           # 2本目：使い切った
        p = plan()
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"]), ("call", "normal", new, False, True))


class MonthAhead(SettleBase):
    """(3) の書き足し：1か月以上先の次の回（✅ 2026-11-12 ユーザー＝計画書 7-3 (3)「「月末なら」と言った場合、何日か幅がありますよね。まず、その期間内で
    かけ直すことです。ピンポイントで31日問言われた場合は、その日の範囲で何回かかけ直します。どうしても、その日に繋がらなければ制限回数の範囲で、
    別の日にかけてもOKしかし、あくまでの優先は指定の範囲内。」。0220）。お客様が言った日時は、何か月先でもそのままかける。お客様が言った幅・日の中で
    話せないまま回が過ぎ、理由に合わせた次の回が1か月以上先になるときだけ（見分けは next_action._keep_promise の _next_round_far の1か所）、業務の設定
    callback.month_ahead に従う：かけない（初め）＝次の回へは回さず、時刻の決まっていない約束の方針の間隔で別の日に／契約者が認めたらかける＝次の回の
    予定を書いて伺う。⛔ 0217・0219（CL-2 の思い違い）：お客様が言った「来月の15日に」なども、初めの値で行を終えていた。
    時計を進めて、別の日にかかる・認めた行が翌月の回にかかる場面は tests/test_life.py の `CallsNotUnwelcome`。"""

    def choose(self, s, value):
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale, review_when,"
            " set_by) VALUES ('callback.month_ahead', 'business', %s, %s, %s, %s, 'decided', '試験', '試験', '試験')",
            (s["tenant"], s["client"], s["business"], f'"{value}"')), OPERATOR)

    def notices(self, s, kind):
        """その行の、その種類の知らせ（要約・急ぎの印）。"""
        return self.tx(s, lambda tx: [(n["summary"], n["urgent"]) for n in tx.all(
            "SELECT summary, urgent FROM salescall.notification_events WHERE kind = %s AND refs ->> 'task_id' = %s ORDER BY event_id",
            (kind, str(s["task"])))])

    def asking(self, s):
        from salescall.core import tasks
        return self.tx(s, lambda tx: tasks.month_ahead_asking(tx, s["task"]))

    def plan(self, s):
        return self.tx(s, lambda tx: next_action.current(tx, s["task"]))

    def day(self, s, t):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.jst(%s::timestamptz)::date AS d", (t,))["d"])

    def midnight(self, s, day):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.jst_day_start(%s::date) AS t", (day,))["t"])

    def next_month(self, s):
        """今日（日本時間）の翌月の1日。"""
        today = self.day(s, self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"]))
        return (today.replace(day=1) + timedelta(days=32)).replace(day=1)

    def passed(self, s, first, last, repeat):
        """試験：お客様が言った回（日本時間の first〜last の日。repeat＝お客様がその日を選んだ理由）に、話せないまま回が過ぎた ⇒ 約束を守る1か所
        （next_action._keep_promise）が次の動きを決め、決定を書く1か所（_write）で書く。（その回の幅, 決定, 書いたあとの予定）を返す。"""
        w = next_action.Window(self.midnight(s, last + timedelta(days=1)), None, None, self.midnight(s, first), repeat, day_said=True)

        def go(tx):
            d = next_action._keep_promise(tx, s["task"], w.until, w, next_action.Target(s["contact"]), now=tx.one("SELECT now() AS t")["t"])
            next_action._write(tx, s["task"], d)
            return d
        d = self.tx(s, go)
        return w, d, self.plan(s)

    def monthly_rounds(self):
        """月ぎめの理由の回（言葉, 回の始まりの日, 終わりの日, 理由）：来月の月末（最後の3日）・来月の終わりの日（「31日に」。その月に無ければ月の
        終わり）・来月の下旬。"""
        end = lambda s: (self.next_month(s) + timedelta(days=32)).replace(day=1) - timedelta(days=1)   # noqa: E731
        return (("月末なら", lambda s: end(s) - timedelta(days=2), end, "month:part:月末"),
                ("31日に", end, end, "month:day:31"),
                ("下旬なら", lambda s: self.next_month(s).replace(day=21), end, "month:part:下旬"))

    def test_a_date_the_callee_said_is_called_however_far_ahead(self):
        """(1) お客様が言った日時（「来月の15日に」「再来月の月末に」）は、業務の設定に関係なく、その日にかける（行を終えない・伺わない・知らせない）。
        ⛔ 0217・0219：初めの値で、終わりの理由 callback_month_ahead で行を終えていた（CL-2 の思い違い。2026-11-12 ユーザー：「1か月後にかけないと
        決めた覚えはありません。」）。"""
        first = self.next_month(self.ready())
        after = (first + timedelta(days=32)).replace(day=1)
        last = (after + timedelta(days=32)).replace(day=1) - timedelta(days=1)   # 再来月の終わりの日
        for setting in (None, "with_approval"):
            for words, day in (("来月の15日に", first.replace(day=15)), ("再来月の月末に", last - timedelta(days=2))):
                with self.subTest(setting=setting, words=words):
                    s = self.ready()
                    if setting is not None:
                        self.choose(s, setting)
                    self.settle(s, self.call(s, report=rep(callback=callback("later", time=words))))
                    p = self.plan(s)
                    self.assertEqual((p["kind"], p["promised"], p["month_ahead_ask"]), ("call", True, False))
                    self.assertEqual(self.day(s, next_action.window_of(p).since), day)   # お客様が言った日（回の始まり）にかける
                    self.assertIsNone(self.asking(s))
                    self.assertEqual(self.notices(s, "month_ahead_approval"), [])

    def test_a_monthly_round_unanswered_goes_to_another_day_by_default(self):
        """(2)(3) 初めの値（かけない）：月ぎめの理由の「月末なら」「31日に」「下旬なら」の回に話せないまま過ぎた ⇒ 次の回（次の月末・翌月の同じ日・
        次の下旬）へは回さず、時刻の決まっていない約束の方針の間隔（ここでは1時間おき）で、回が過ぎてから＝別の日にかける（約束したかけ直しのまま・
        幅は外す・行を終えない・伺わない）。理由の分からない日だけの約束が過ぎたときと同じ道（next_action._window_over）。その回に一度もかけられ
        なかったお詫びも引き継ぐ。"""
        for words, first, last, repeat in self.monthly_rounds():
            with self.subTest(words=words):
                s = self.ready()
                w, d, p = self.passed(s, first(s), last(s), repeat)
                self.assertEqual((d.kind, d.promised, d.window, d.month_ahead_after), ("call", True, None, None))
                self.assertEqual(d.at, w.until + timedelta(hours=1))             # 回の終わりから、方針の間隔（1時間）
                self.assertIn("1か月以上先のため、次の回へは回さずに別の日に", d.rule)
                self.assertEqual((p["kind"], p["promised"], p["window_until"], p["month_ahead_ask"]), ("call", True, None, False))
                self.assertEqual(tuple(p["apology_reasons"]), ("missed_promise",))
                self.assertIsNone(self.asking(s))
                self.assertEqual(self.notices(s, "month_ahead_approval"), [])

    def test_a_weekly_round_unanswered_goes_to_the_next_week_as_before(self):
        """(4)「金曜なら」（週ごとの理由）の回に話せないまま過ぎた ⇒ いまどおり次の金曜（1か月より手前）。業務の設定に関係なく、伺わない。"""
        for setting in (None, "with_approval"):
            with self.subTest(setting=setting):
                s = self.ready()
                if setting is not None:
                    self.choose(s, setting)
                friday = self.next_month(s) + timedelta(days=(4 - self.next_month(s).weekday()) % 7)
                w, d, p = self.passed(s, friday, friday, "week")
                self.assertEqual((d.kind, d.window.round, self.day(s, d.window.since), d.month_ahead_after),
                                 ("call", 1, friday + timedelta(days=7), None))
                self.assertFalse(p["month_ahead_ask"])
                self.assertEqual(self.notices(s, "month_ahead_approval"), [])

    def test_with_approval_the_next_round_is_asked_and_a_refusal_goes_to_another_day(self):
        """(5) 設定「契約者が認めたらかける」：次の回（次の月末）の予定を、伺う印を付けて書き、契約者に知らせる（話せなかった回と次の回の日が分かる文。
        急ぎでない）。認めたら伺いは終わり（その時刻にかける）・認めなければ、行を終えずに、次の回へは回さず、別の日に時刻の決まっていない約束の方針で
        かける（⛔ 0217・0219：行を終えていた）。答えられるのは管理者か契約者だけで、伺っている行の、その時刻だけ（データベースの守り）。"""
        from salescall.core import row_fixes, tasks
        from salescall.core.access import Rejected
        rows = []
        for _ in range(2):
            s = self.ready()
            self.choose(s, "with_approval")
            words, first, last, repeat = self.monthly_rounds()[0]
            w, d, p = self.passed(s, first(s), last(s), repeat)
            self.assertEqual((d.kind, d.window.round, d.month_ahead_after), ("call", 1, w))
            self.assertEqual((p["kind"], p["promised"], p["month_ahead_ask"]), ("call", True, True))
            for read in (tasks.month_ahead_asking, tasks.month_ahead_waiting, tasks.month_ahead_answerable):
                self.assertEqual(self.tx(s, lambda tx, s=s, read=read: read(tx, s["task"])), p["call_at"])
            got = self.notices(s, "month_ahead_approval")
            self.assertEqual([u for _, u in got], [False])                     # 伺いの知らせは急ぎでない
            label = lambda a, b: f"{a.month}月{a.day}日〜{b.month}月{b.day}日"   # noqa: E731
            nxt_first, nxt_last = self.day(s, d.window.since), self.day(s, d.window.until - timedelta(microseconds=1))
            self.assertIn(f"お約束の回（{label(first(s), last(s))}）にお話しできず", got[0][0])
            self.assertIn(f"次の回（{label(nxt_first, nxt_last)}）は1か月以上先です", got[0][0])
            self.assertIn("認めなければ、次の回へは回さず、別の日に時刻の決まっていない約束の方針でかけます", got[0][0])
            rows.append((s, p))
        (a, pa), (b, pb) = rows
        with self.assertRaises(Rejected):                                        # システムの処理は答えられない
            self.tx(a, lambda tx: tx.execute(
                "INSERT INTO salescall.task_month_ahead_answers (tenant_id, client_id, task_id, call_at, approved, answered_by)"
                " VALUES (%s, %s, %s, %s, true, salescall.cur_actor())", (a["tenant"], a["client"], a["task"], pa["call_at"])))
        got = self.tx(a, lambda tx: row_fixes.answer_month_ahead(tx, a["task"], approve=True), OPERATOR)
        self.assertEqual((got["approved"], got["next"]["seq"]), (True, pa["seq"]))   # 予定はそのまま（次の回の時刻にかける）
        self.assertIsNone(self.asking(a))
        self.assertIsNone(self.tx(a, lambda tx: tasks.month_ahead_waiting(tx, a["task"])))
        with self.assertRaises(row_fixes.FixInvalid):                            # もう伺っていない
            self.tx(a, lambda tx: row_fixes.answer_month_ahead(tx, a["task"], approve=False), OPERATOR)
        self.refused_goes_to_another_day(b)

    def refused_goes_to_another_day(self, s):
        """契約者が「認めない」と答えた ⇒ 行を終えずに、答えたときから、時刻の決まっていない約束の方針の間隔（1時間）で別の日に（幅は外す・約束の
        かけ直しのまま・伺いは終わり）。"""
        from salescall.core import row_fixes
        got = self.tx(s, lambda tx: row_fixes.answer_month_ahead(tx, s["task"], approve=False), OPERATOR)
        p = got["next"]
        self.assertEqual((p["kind"], p["promised"], p["window_until"], p["month_ahead_ask"]), ("call", True, None, False))
        self.assertTrue(timedelta(minutes=59) <= p["call_at"] - p["decided_at"] <= timedelta(hours=1, minutes=1), p)
        self.assertIn("次の回へは回さず、別の日に", p["cause"])
        self.assertIsNone(self.asking(s))

    def test_a_plan_that_keeps_the_asked_time_is_still_asked_once(self):
        """(6) 伺っている予定を、同じ時刻のまま書き直す（門で止められたあと・番号や名前を直したあとの決め直し）⇒ 伺いを続ける（印を引き継ぐ・知らせは
        その時刻ごとに1回）。時刻が変われば伺いは終わる。お客様が言った回そのものの決め直しは、何か月先でも伺わない（次の回へ移っていない）。"""
        s = self.ready()
        self.choose(s, "with_approval")
        words, first, last, repeat = self.monthly_rounds()[0]
        w, _, p = self.passed(s, first(s), last(s), repeat)
        again = lambda at, far=None: self.tx(s, lambda tx: next_action._write(tx, s["task"], replace(next_action._call(   # noqa: E731
            at, True, "試験：いまの予定の時刻のまま決め直す", next_action.Target(s["contact"]), None, next_action.window_of(p)),
            month_ahead_after=far)))
        again(p["call_at"])
        self.assertEqual(self.asking(s), p["call_at"])
        again(p["call_at"], w)                                                   # 同じ次の回を、もう一度決めた（時刻の表し方が違っても1回）
        self.assertEqual(len(self.notices(s, "month_ahead_approval")), 1)
        again(p["call_at"] + timedelta(hours=1))                                 # 回の中の別の時刻へ
        self.assertIsNone(self.asking(s))
        t = self.ready()
        self.choose(t, "with_approval")
        self.settle(t, self.call(t, report=rep(callback=callback("later", time="再来月の月末に"))))
        q = self.plan(t)
        self.tx(t, lambda tx: next_action._write(tx, t["task"], next_action._call(
            q["call_at"] + timedelta(hours=1), True, "試験：お客様が言った回の中の決め直し", next_action.Target(t["contact"]), None,
            next_action.window_of(q))))
        self.assertIsNone(self.asking(t))
        self.assertEqual(self.notices(t, "month_ahead_approval"), [])

    def test_a_month_ahead_set_by_the_contractors_policy_is_called_as_set(self):
        """(7) 契約者が設定した再発信の方針の間隔（40日後）で1か月以上先になった電話は、業務の設定に関係なく、そのままかける（伺わない・知らせない）。
        ふつうの電話の結果ごとの方針も、時刻の決まっていない約束（「あとで」）の方針も同じ。✅ 2026-11-12 ユーザー＝計画書 7-3 (3)「契約者が意図的に
        設定したら、かけて良い。」。⛔ 0217：初めの値で行を終えた・「契約者が認めたらかける」では答えを待った。"""
        forty = rules_raw(retry={k: {"action": "call", "max_times": 3, "interval": {"days": 40}}
                                 for k in ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")})
        for setting, later in ((None, False), ("with_approval", False), (None, True)):
            with self.subTest(setting=setting, later=later):
                u = self.ready(rules=forty)
                if setting is not None:
                    self.choose(u, setting)
                self.settle(u, self.call(u, report=rep(callback=callback("later")) if later else rep()))
                p = self.plan(u)
                self.assertEqual((p["kind"], p["promised"]), ("call", later))
                self.assertGreaterEqual(p["call_at"] - p["decided_at"], timedelta(days=39))   # 1か月以上先
                self.assertIsNone(self.asking(u))                                # 伺わない
                self.assertEqual(self.notices(u, "month_ahead_approval"), [])

    def asked(self, s, at):
        """試験の近道：1か月以上先の次の回へ回し、契約者に伺う予定（その時刻 at を含む次の回。話せなかった回は、その30日前）を、決定を書く1か所で
        書く（決定の値 month_ahead_after を持たせるのは、ふだんは next_action._keep_promise の1か所）。"""
        def write(tx):
            day = tx.one("SELECT salescall.jst(%s::timestamptz)::date AS d", (at,))["d"]
            nxt = next_action.Window(at + timedelta(days=1), None, None, at - timedelta(hours=1), f"month:day:{day.day}", round=1, day_said=True)
            passed = replace(nxt, until=nxt.until - timedelta(days=30), since=nxt.since - timedelta(days=30), round=0)
            d = next_action._call(at, True, "試験：1か月以上先の次の回", next_action.Target(s["contact"]), None, nxt)
            return next_action._write(tx, s["task"], replace(d, month_ahead_after=passed))
        self.tx(s, write)

    def due_and_asked(self, ahead=timedelta(0)):
        """試験の近道：契約者に伺っていて答えの無い、1か月以上先の次の回の予定の時刻が来た行（ahead＝約束の時刻まで、あとどれだけか）。"""
        s = self.ready()
        self.choose(s, "with_approval")
        at = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"]) + ahead
        self.asked(s, at)
        return s, at

    def test_when_the_time_comes_without_an_answer_the_call_goes_with_one_urgent_notice(self):
        """(5) 答えの無いまま約束の時刻が来た ⇒ 待たずにかける（門が通す）。通したところで、契約者に緊急の知らせを1回（行とその時刻ごとに。予約を
        取り消してまた通しても増えない）。かけた・かけている予定には、もう答えられない。予約を取り消して、まだかけていなければ答えられる。
        ✅ 2026-11-12 ユーザー＝計画書 7-3 (3)「契約者に緊急の通知をして、約束の時間にかける」。⛔ 0217：門が待ち続け、行がかけられないまま残った。"""
        from salescall.core import row_fixes, tasks
        s, at = self.due_and_asked()
        self.assertEqual(self.tx(s, lambda tx: (tasks.month_ahead_asking(tx, s["task"]), tasks.month_ahead_waiting(tx, s["task"]),
                                                tasks.month_ahead_answerable(tx, s["task"]))), (at, None, at))
        self.assertIsNone(self.tx(s, lambda tx: gate.waits_unknown(tx, s["task"])))   # 候補にも出す
        self.assertEqual(self.notices(s, "month_ahead_unanswered"), [])         # 見守り役の仕事がまだ回っていない
        c = self.claim(s)                                                        # 門が通す：出しそびれていても、知らせの無いままかけない
        self.assertIsInstance(c, gate.Claim, c)
        got = self.notices(s, "month_ahead_unanswered")
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0][1])                                               # 緊急
        self.assertIn(f"お約束の次の回（{next_action.calling_hours.label(at)}から）は1か月以上先のため伺っていましたが、お答えが無いので、"
                      "約束の時刻にお約束どおり", got[0][0])
        self.assertIsNone(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])))   # 予約した＝かけている
        with self.assertRaises(row_fixes.FixInvalid):
            self.tx(s, lambda tx: row_fixes.answer_month_ahead(tx, s["task"], approve=False), OPERATOR)
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))           # 予約を使わずに取り消した：まだかけていない
        self.assertEqual(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])), at)
        self.assertIsInstance(self.claim(s), gate.Claim)                         # また通しても、知らせは1回のまま
        self.assertEqual(len(self.notices(s, "month_ahead_unanswered")), 1)

    def test_the_urgent_notice_goes_five_minutes_before_and_an_answer_in_between_is_followed(self):
        """(5) の知らせの時刻：約束の時刻の5分前（gate.MONTH_AHEAD_NOTICE）を過ぎたら、見守り役の仕事の候補（gate.month_ahead_notice_rows）に入り、
        緊急の知らせを1回出す（gate.month_ahead_unanswered の1か所。門が通すときも同じ所を通るので増えない）。5分より前は出さない。知らせのあと、
        かけるまでに契約者が「認めない」と答えれば、そのとおり別の日へ（行を終えない）。✅ 2026-11-12 ユーザー：「時刻は5分前」。"""
        self.assertEqual(gate.MONTH_AHEAD_NOTICE, timedelta(minutes=5))
        early, _ = self.due_and_asked(timedelta(minutes=6))                     # 6分前：まだ出さない
        self.assertNotIn(early["task"], self.tx(early, gate.month_ahead_notice_rows))
        self.assertFalse(self.tx(early, lambda tx: gate.month_ahead_unanswered(tx, early["task"])))
        self.assertEqual(self.notices(early, "month_ahead_unanswered"), [])
        s, at = self.due_and_asked(timedelta(minutes=4))                         # 4分前：出す
        self.assertIn(s["task"], self.tx(s, gate.month_ahead_notice_rows))
        for _ in range(2):                                                       # 何回見ても1回
            self.tx(s, lambda tx: gate.month_ahead_unanswered(tx, s["task"]))
        got = self.notices(s, "month_ahead_unanswered")
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0][1])                                               # 緊急
        self.assertIsInstance(self.claim(s), gate.Denied)                        # 約束の時刻の前はかけない
        self.refused_goes_to_another_day(s)                                      # 知らせを見て止めた
        self.assertEqual(self.tx(s, gate.month_ahead_notice_rows), [])          # 伺っていない予定は候補に入らない

    def test_after_the_time_a_refusal_goes_to_another_day_only_before_it_is_called(self):
        """(5) の境目：約束の時刻のあとでも、まだかけていなければ「認めない」で別の日へ（行を終えない）。かけた予定には答えられない（`FixInvalid`。
        データベースの守りも同じ1か所＝salescall.task_month_ahead_answerable）。"""
        from salescall.core import row_fixes
        from salescall.core.access import Rejected
        s, _ = self.due_and_asked()
        self.refused_goes_to_another_day(s)
        t, at = self.due_and_asked()
        self.call(t, ["dialing"])                                                # かけた（発信まで進んだ）
        with self.assertRaises(row_fixes.FixInvalid):
            self.tx(t, lambda tx: row_fixes.answer_month_ahead(tx, t["task"], approve=False), OPERATOR)
        with self.assertRaises(Rejected):                                        # データベースも答えを入れない
            self.tx(t, lambda tx: tx.execute(
                "INSERT INTO salescall.task_month_ahead_answers (tenant_id, client_id, task_id, call_at, approved, answered_by)"
                " VALUES (%s, %s, %s, %s, false, salescall.cur_actor())", (t["tenant"], t["client"], t["task"], at)), OPERATOR)
        self.assertEqual(self.plan(t)["kind"], "call")


class UrgentNotices(SettleBase):
    """知らせの急ぎの印（0219）：緊急かは知らせの種類ごとに notify.URGENT の1か所で決め、台帳の列 urgent に残す（管理者あての写しも同じ）。"""

    def test_only_the_urgent_kinds_are_marked_urgent_in_both_copies(self):
        self.assertEqual(notify.URGENT, frozenset({"month_ahead_unanswered"}))   # ほかの種類を勝手に緊急にしない
        self.assertLessEqual(notify.URGENT, set(notify.KINDS))
        s = self.ready()
        for kind in ("month_ahead_unanswered", "month_ahead_approval"):
            self.tx(s, lambda tx, kind=kind: notify.emit(tx, kind, key=f"試験:{kind}:{s['task']}", summary="試験の知らせ", admin=True,
                                                         refs={"task_id": s["task"]}))
            read = lambda tx, kind=kind: [r["urgent"] for r in tx.all(   # noqa: E731
                "SELECT urgent FROM salescall.notification_events WHERE event_key IN (%s, %s)",
                (f"試験:{kind}:{s['task']}", f"admin:試験:{kind}:{s['task']}"))]
            got = self.tx(s, read) + self.w.a.in_system(ADMIN, read)          # 契約者の台帳・管理者あての写し（全体の取引で読む）
            self.assertEqual(got, [kind in notify.URGENT] * 2, kind)


class Guards(SettleBase):
    """0216 のデータベースの守り。"""

    def test_a_recovering_deadline_is_only_on_an_exact_promise(self):
        """取り戻す電話の期限（次の予定の列 window_recover_until）は、時刻ちょうどの約束（お客様が指定した時刻と幅を持つ、かけ直す予定）にだけ
        付けられる（データベースの守り task_next_actions_window_recover）。"""
        from salescall.core.access import Rejected
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=1), (0, 24 * 3600), None, now - timedelta(hours=1), "day",
                               recover_until=now + timedelta(hours=2))
        write = lambda callee: self.tx(s, lambda tx: next_action._append(   # noqa: E731
            tx, s["task"], call_at=now, end_reason=None, cause="試験：取り戻す電話", promised=True,
            target=next_action.Target(s["contact"]), callee_at=callee, window=w))
        with self.assertRaises(Rejected):
            write(None)                                                          # 時刻ちょうどの約束でない
        write(now)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["window_recover_until"], w.recover_until)

    def test_the_ask_mark_is_only_on_a_call(self):
        """契約者に伺う印（次の予定の列 month_ahead_ask。0219）は、かけ直す予定にだけ付けられる（データベースの守り task_next_actions_month_ahead_ask）。"""
        from salescall.core.access import Rejected
        s = self.ready()
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=None, end_reason="callback_unspecified_time", cause="試験：終える",
                                                      month_ahead_ask=True))


if __name__ == "__main__":
    unittest.main()
