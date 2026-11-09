"""お客様から見て煙たがられないかけ方の場面の試験（✅ 2026-11-08 ユーザー＝計画書 7-3「お客様から見て煙たがられないかけ方」(1)(2)。0216。
DESIGN.md 5-2・7-2 の【0215】。持ち主 CL-2）。本物の門・出口・締めの処理を通す。時計を進める場面（(3) 幅の約束・(4) かけられなかった約束）は
tests/test_life.py の `CallsNotUnwelcome`。(5)(6) は test_apology・test_alternate・test_settle の該当の場面。
⚠️ 試験のデータは架空のものだけ。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from salescall.core import apology, attempts, business_rules, gate, next_action, notify, quality_stage, registry, scheduler
from tests.support import OPERATOR, callback, make_report, rules_raw, said
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
    """(3) の書き足し：1か月以上先のかけ直し（✅ 2026-11-08 ユーザー＝計画書 7-3 (3)「1か月先に電話を入れるなんてことは通常ありえません。1か月以上先の
    予定がある場合、契約者が認めた場合のみかけます。設定で、1か月以上先の架電はかけないがデフォルト。（選択式）」。0217）。決めるのは決定を書く
    1か所（next_action._write の _month_ahead）、1か月以上先かはデータベースの salescall.month_ahead の1か所。⛔ 前は、何か月先でもかけていた。
    時計を進めて、認めた行がその時刻にかかる場面は tests/test_life.py の `CallsNotUnwelcome`。"""

    def days(self, s):
        """今日（日本時間）の翌月の同じ日（その月に無ければ月の終わり）と、その前の日。"""
        r = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now())::date + interval '1 month')::date AS m"))
        return r["m"], r["m"] - timedelta(days=1)

    def said(self, s, day):
        """お客様が「（月）（日）なら」と日だけを言った約束の通話を締め、そのあとの予定を返す。"""
        aid = self.call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日なら")))
        self.settle(s, aid)
        return self.tx(s, lambda tx: next_action.current(tx, s["task"]))

    def choose(self, s, value):
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale, review_when,"
            " set_by) VALUES ('callback.month_ahead', 'business', %s, %s, %s, %s, 'decided', '試験', '試験', '試験')",
            (s["tenant"], s["client"], s["business"], f'"{value}"')), OPERATOR)

    def test_a_promise_a_month_ahead_ends_by_default_and_one_before_is_called_as_before(self):
        """初めの値（かけない）：お客様が翌月の同じ日を言った ⇒ 予定を書かずに、終わりの理由 callback_month_ahead で行を終える（契約者・依頼者に
        分かる文つき）。その前の日（1か月より手前）は、いまどおり約束の日にかける。再発信の方針の間隔で決めた時刻（40日後）も同じ決まり。"""
        s = self.ready()
        month, before = self.days(s)
        p = self.said(s, month)
        self.assertEqual((p["kind"], p["end_reason"]), ("end", next_action.MONTH_AHEAD))
        self.assertIn("1か月以上先", p["cause"])
        t = self.ready()
        p = self.said(t, before)
        self.assertEqual((p["kind"], p["promised"], next_action.window_of(p).since.astimezone(next_action.calling_hours.JST).date()),
                         ("call", True, before))
        u = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 3, "interval": {"days": 40}}
                                              for k in ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")}))
        self.settle(u, self.call(u, report=rep()))
        self.assertEqual(self.tx(u, lambda tx: next_action.current(tx, u["task"]))["end_reason"], next_action.MONTH_AHEAD)

    def test_with_approval_the_plan_waits_for_the_contractor_who_approves_or_refuses(self):
        """設定「契約者が認めたらかける」：予定はそのまま書き、契約者に知らせ、答えるまで門が待つ（候補にも出さない）。認めたら待たない（その時刻に
        かかる）・認めなければ行を終える。答えられるのは管理者か契約者だけで、待っている行の、その時刻だけ（データベースの守り）。"""
        from salescall.core import row_fixes, tasks
        from salescall.core.access import Rejected
        rows = []
        for _ in range(2):
            s = self.ready()
            self.choose(s, "with_approval")
            month, _ = self.days(s)
            p = self.said(s, month)
            self.assertEqual((p["kind"], p["promised"]), ("call", True))
            self.assertEqual(self.tx(s, lambda tx, s=s: tasks.month_ahead_waiting(tx, s["task"])), p["call_at"])
            self.assertEqual(self.tx(s, lambda tx, s=s: gate.waits_unknown(tx, s["task"])), "month_ahead_approval")   # 候補に出さない
            self.assertEqual(self.tx(s, lambda tx, s=s: [n["kind"] for n in tx.all(
                "SELECT kind FROM salescall.notification_events WHERE refs ->> 'task_id' = %s", (str(s["task"]),))]), ["month_ahead_approval"])
            rows.append((s, p))
        (a, pa), (b, pb) = rows
        with self.assertRaises(Rejected):                                        # システムの処理は答えられない
            self.tx(a, lambda tx: tx.execute(
                "INSERT INTO salescall.task_month_ahead_answers (tenant_id, client_id, task_id, call_at, approved, answered_by)"
                " VALUES (%s, %s, %s, %s, true, salescall.cur_actor())", (a["tenant"], a["client"], a["task"], pa["call_at"])))
        got = self.tx(a, lambda tx: row_fixes.answer_month_ahead(tx, a["task"], approve=True), OPERATOR)
        self.assertEqual((got["approved"], got["next"]["seq"]), (True, pa["seq"]))   # 予定はそのまま（その時刻にかける）
        self.assertIsNone(self.tx(a, lambda tx: tasks.month_ahead_waiting(tx, a["task"])))
        self.assertIsNone(self.tx(a, lambda tx: gate.waits_unknown(tx, a["task"])))
        with self.assertRaises(row_fixes.FixInvalid):                            # もう待っていない
            self.tx(a, lambda tx: row_fixes.answer_month_ahead(tx, a["task"], approve=False), OPERATOR)
        got = self.tx(b, lambda tx: row_fixes.answer_month_ahead(tx, b["task"], approve=False), OPERATOR)
        self.assertEqual((got["next"]["kind"], got["next"]["end_reason"]), ("end", next_action.MONTH_AHEAD))


    def test_a_month_ahead_is_counted_from_the_day_the_time_was_first_decided(self):
        """1か月以上先かは、その時刻へかける予定をその行で初めて決めた日から数える（データベースの salescall.month_ahead）：40日前に決めた5日後の
        予定は、いまから見れば1か月より手前でも、契約者の答えを待つ（同じ時刻のまま書き直しても同じ）。その日になって数え直すと、答えを待たずに
        かかってしまう。"""
        from salescall.core import tasks
        s = self.ready()
        self.choose(s, "with_approval")
        at = self.tx(s, lambda tx: tx.one("SELECT now() + interval '5 days' AS t")["t"])
        write = lambda: self.tx(s, lambda tx: next_action._write(tx, s["task"], next_action._call(   # noqa: E731
            at, True, "試験：お客様と約束したかけ直し", next_action.Target(s["contact"]))))
        write()
        self.assertIsNone(self.tx(s, lambda tx: tasks.month_ahead_waiting(tx, s["task"])))   # いま決めた5日後：1か月より手前

        self.back_40_days(s, at)
        self.assertEqual(self.tx(s, lambda tx: tasks.month_ahead_waiting(tx, s["task"])), at)   # 40日前から見れば1か月以上先
        write()                                                                  # 同じ時刻のまま書き直す（番号の直しなど）
        self.assertEqual(self.tx(s, lambda tx: tasks.month_ahead_waiting(tx, s["task"])), at)


    def back_40_days(self, s, at):
        """試験：その時刻の予定を40日前に決めたことにする（持ち主の取引の中だけ仕掛けを外す）。"""
        from tests.support import SYSTEM, owner_access

        def back(tx):
            tx.execute("ALTER TABLE salescall.task_next_actions DISABLE TRIGGER USER")
            tx.execute("UPDATE salescall.task_next_actions SET decided_at = decided_at - interval '40 days' WHERE task_id = %s AND call_at = %s",
                       (s["task"], at))
            tx.execute("ALTER TABLE salescall.task_next_actions ENABLE TRIGGER USER")
        owner_access().in_system(SYSTEM, back)

    def test_when_the_time_comes_the_gate_waits_for_the_answer(self):
        """答えないまま予定の時刻が来た ⇒ 門は month_ahead_approval で断る（tasks.month_ahead_waiting の1か所）。契約者が認めたら、門が通す。
        （試験の近道：いまの時刻の予定を40日前に決めたことにする＝決めた日から1か月以上先の予定の時刻が来た）"""
        from salescall.core import row_fixes
        s = self.ready()
        self.choose(s, "with_approval")
        at = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: next_action._write(tx, s["task"], next_action._call(at, True, "試験：お客様と約束したかけ直し",
                                                                                next_action.Target(s["contact"]))))
        self.back_40_days(s, at)
        self.assertEqual(self.reason(s), "month_ahead_approval")
        self.tx(s, lambda tx: row_fixes.answer_month_ahead(tx, s["task"], approve=True), OPERATOR)
        self.assertNotIsInstance(self.claim(s), gate.Denied)


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


if __name__ == "__main__":
    unittest.main()
