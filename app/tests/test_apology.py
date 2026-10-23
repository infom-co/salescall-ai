"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from salescall.core import apology, attempts, fix_requests, gate, next_action, quality_stage, reward, stops
from salescall.core.access import Rejected
from salescall.core.attempts import ForbiddenFinding, VerificationSummary
from salescall.core.call_report import Cut
from tests.support import ADMIN, OPERATOR, SYSTEM, owner_access, said
from tests.test_approval import TAKEN, Helpers
from tests.test_settle import rep

TEXT = ("先ほどのお電話で、誤って「初月は無料です」というご案内をしてしまいましたが、正しくは「初月は半額です」となります。"
        "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。")


class Apology(Helpers):
    def incident(self, s):
        """言ってはいけないことを言った通話（その行は終わり、業務は止まる）。その試行を返す。"""
        self.verified_call(s, said(), items=TAKEN)
        return self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s"
                                            " ORDER BY claimed_at DESC LIMIT 1", (s["task"],)))["attempt_id"]

    def draft(self, s, aid):
        return self.tx(s, lambda tx: tx.one("SELECT order_id FROM salescall.apology_orders WHERE source_attempt_id = %s",
                                            (aid,)))

    def order(self, s, aid, actor=OPERATOR):
        """システムが作った案を、契約者が承認する。"""
        return self.tx(s, lambda tx: apology.approve(tx, self.draft(s, aid)["order_id"]), actor)

    def apology_call(self, s, **report):
        aid = self.call(s, report=replace(rep(), **report))
        return self.settle(s, aid)

    def results(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.task_results WHERE task_id = %s",
                                            (s["task"],)))["n"]

    def test_the_system_prepares_and_only_a_person_approves(self):
        s = self.fresh(stage_cap=20)
        aid = self.incident(s)
        notice = self.tx(s, lambda tx: tx.one("SELECT summary, refs FROM salescall.notification_events"
                                              " WHERE event_key = %s", (f"forbidden_speech:{aid}",)))
        self.assertIn(TEXT, notice["summary"])                         # 案は、システムが作って知らせる
        admin = self.w.a.in_system(ADMIN, lambda tx: tx.all(           # 管理者にも知らせる（確かめ・承認のため。全体の取引で読む）
            "SELECT origin_tenant FROM salescall.notification_events WHERE event_key = %s AND for_admin",
            (f"admin:forbidden_speech:{aid}",)))
        self.assertEqual([r["origin_tenant"] for r in admin], [s["tenant"]])
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r for r in fix_requests.open_requests(tx, "CL-1")
                                                        if r["kind"] == "forbidden_speech"])
        self.assertTrue(asked)                                         # 会話の不具合：止めずに会話の担当へ修正依頼
        version = self.tx(s, lambda tx: tx.one("SELECT version_id FROM salescall.attempts WHERE attempt_id = %s", (aid,)))
        keys = self.w.a.in_system(SYSTEM, lambda tx: [r["event_key"] for r in tx.all(
            "SELECT event_key FROM salescall.fix_requests WHERE kind = 'forbidden_speech' AND refs->>'attempt_id' = %s", (str(aid),))])
        self.assertEqual([k.split("#")[0] for k in keys], [f"fix:forbidden_speech:{version['version_id']}"])   # 同じ不具合は、業務設定の版ごとに1つ（見直し13）
        self.assertNotIn(TEXT, asked[0]["summary"])                    # 修正依頼に、お客様あての文面を入れない
        self.assertEqual(int(notice["refs"]["apology_order_id"]), self.draft(s, aid)["order_id"])
        self.assertEqual(self.tx(s, lambda tx: apology.for_task(tx, s["task"])), None)   # 承認するまで、かけない
        self.assertEqual(self.reason(s), "task_ended")
        with self.assertRaises(Rejected) as e:
            self.order(s, aid, actor=SYSTEM)                          # システムは承認しない（契約者が確認して承認する）
        self.assertEqual(e.exception.constraint, "apology_approval_by_person")
        self.order(s, aid)
        self.assertEqual(self.tx(s, lambda tx: apology.for_task(tx, s["task"]))["text"], TEXT)

    def test_improper_speech_uses_the_other_script(self):
        """正しい案内の無いこと（失礼な言葉など）を言ったときは、別の文面（2026-10-16 ユーザー）。"""
        s = self.fresh(stage_cap=20)
        self.verified_call(s, VerificationSummary(2, 0, 1, (ForbiddenFinding("F2", "失礼な言葉"),)), items=TAKEN)
        aid = self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s", (s["task"],)))
        notice = self.tx(s, lambda tx: tx.one("SELECT summary FROM salescall.notification_events WHERE event_key = %s",
                                              (f"forbidden_speech:{aid['attempt_id']}",)))
        self.assertIn("先ほどのお電話で、「失礼な言葉」といった不適切な発言をしてしまい、大変失礼いたしました。"
                      "心よりお詫び申し上げます。誠に申し訳ございません！", notice["summary"])   # 何を言ってしまったかを伝える（2026-10-22 ユーザーの文）
        self.assertIsNotNone(self.draft(s, aid["attempt_id"]))
        with self.assertRaises(ValueError):                            # 数と中身が合わない検証の結果は作れない
            VerificationSummary(1, 0, 1, ())

    def test_both_kinds_in_one_apology(self):
        got = apology.text([{"wrong": "A", "correct": "a"}], improper=["B", "C"])
        self.assertEqual(got, "先ほどのお電話で、誤って「A」というご案内をしてしまいましたが、正しくは「a」となります。"
                              "また、「B」「C」といった不適切な発言もしてしまいました。"
                              "不正確なご案内と不適切な発言をしてしまいまして、誠に申し訳ございませんでした。")

    def test_two_mistakes_in_one_apology_and_a_later_day(self):
        got = apology.text([{"wrong": "A", "correct": "a"}, {"wrong": "B", "correct": "b"}])
        self.assertEqual(got, "先ほどのお電話で、誤って「A」というご案内をしてしまいましたが、正しくは「a」となります。"
                              "また、誤って「B」というご案内をしてしまいましたが、正しくは「b」となります。"
                              "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。")
        later = apology.text([{"wrong": "A", "correct": "a"}], same_day=False)
        self.assertTrue(later.startswith("先日のお電話で、"))          # 翌日以降にかけ直したとき

    def test_delivered_ends_the_row_without_changing_the_result(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        self.assertEqual(self.business_stops(s), [])                   # 本番：1件だけなら業務は止めない（2026-10-16 ユーザー）
        self.tx(s, lambda tx: stops.stop(tx, scope="business", cause="forbidden_speech", reason="試験：品質の停止",
                                         tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        claim = self.claim(s)
        self.assertIsInstance(claim, gate.Claim)                       # 業務の品質の停止では、お詫びは止めない
        self.assertEqual(claim.purpose, "apology")
        self.tx(s, lambda tx: attempts.release(tx, [claim.attempt_id]))   # 予約は戻し、下で出口を通してかける
        before = self.results(s)
        got = self.apology_call(s, apology_delivered=True)
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "apology_done"))
        self.assertEqual(self.results(s), before)                      # 業務の結果は変えない
        notices = self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'apology'"
                                               " AND refs ->> 'task_id' = %s", (str(s["task"]),)))
        self.assertEqual([n["summary"] for n in notices], ["お詫びを伝えました。"])
        self.assertEqual(self.tx(s, lambda tx: quality_stage.used(tx, s["version"])), 1)    # 段の件数に数えない
        m = self.tx(s, lambda tx: reward.month(tx))
        self.assertEqual(m["breakdown"]["dial"]["count"], 1)           # 報酬にも数えない（こちらの誤りのため）

    def test_an_apology_waits_until_the_call_is_judged_and_is_not_made_if_the_customer_refused(self):
        """言ってはいけないことを言い、しかも断ったかを決められない（判定待ち）通話：お詫びを承認しても、すぐかけない。録音から作り
        直した報告で「断っていた」と分かれば、お詫びもかけず、発信禁止に入れる（⛔ 2026-10-22 の見直し：前は承認で判定待ちの予定を
        「いまお詫び」に替え、断ったかもしれないお客様へかけ、判定し直す道も消していた）。"""
        from salescall.core import call_report, dnc, next_action, settle
        from tests.support import said
        s = self.fresh(stage_cap=20)
        aid = self.call(s, report=rep(cut=None), verify=None)                   # 切れ方の材料が無い＝断ったかを決められない
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
        self.assertEqual(self.settle(s, aid)["state"], "unknown")
        self.order(s, aid)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "hold")   # 判定待ちのまま
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(dnc_=True), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        self.assertIsNotNone(self.tx(s, lambda tx: dnc.lookup(tx, s["phone"])))
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["kind"], cur["purpose"]), ("end", None))       # 断った：お詫びもかけない

    def test_an_apology_to_another_person_is_not_delivered(self):
        """判定なしで繋いだ方に伝えたが、確かめ直しで別の方だった：伝えていない側に倒す（目的のお客様へは届いていない。
        2026-10-22 の見直しで、壊しても落ちる試験を足した）。"""
        from salescall.core import target_recheck
        from salescall.core.call_report import Connect, Said
        from tests.test_settle import CLEAN
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        report = replace(rep(), apology_delivered=True, connects=(Connect(Said(3, "はい、そうです"), ("ai",), True),))
        aid = self.call(s, report=report, verify=None)
        self.tx(s, lambda tx: target_recheck.record(tx, aid, "not_target", evidence="試験：別の方", checked_by="試験"))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        got = self.settle(s, aid)
        self.assertEqual((got["delivered"], got["decision"].kind, got["decision"].target.purpose), (False, "call", "apology"))

    def test_not_delivered_tries_another_band_up_to_the_limit(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, apology_delivered=False)
        self.assertEqual((got["decision"].kind, got["decision"].target.purpose), ("call", "apology"))   # 別の時間帯に
        t = self.fresh(stage_cap=20)
        self.put_client(t, "retry.apology_max", 1)
        self.order(t, self.incident(t))
        got = self.apology_call(t)                                     # 報告に無い ⇒ 伝えていない側に倒す
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "apology_unreached"))

    def test_a_cut_apology_call_is_called_back_at_once(self):
        """お詫びの電話が途中で切れた：ふつうの電話と同じく、復旧次第すぐかけ直す（続けて2回まで。⛔ 2026-10-22 の見直し14：前は
        お詫びの電話だけ、別の時間帯に回していた）。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"),
                        report=replace(rep(cut=None), apology_delivered=False))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.promised), ("call", "apology", True))
        c = self.claim(s)                                                # すぐ（別の時間帯を待たない）
        self.assertEqual((type(c).__name__, getattr(c, "purpose", None)), ("Claim", "apology"))

    def test_refusal_ends_and_registers_dnc(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, refusals=rep(dnc_=True).refusals)
        self.assertEqual(got["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"
                                                      " WHERE phone_e164 = %s", (s["phone"],)))["n"], 1)

    def test_refusal_is_decided_in_the_one_place(self):
        """お詫びの電話の拒否も、判定と同じ1か所（judge.refusal）で決める（2026-10-16 の見直し J6）。
        何も言わずに切った ⇒ 断った（発信禁止）。断ったかを決められない（切れ方の材料が無い）⇒ もうかけない。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, cut=Cut("ai", False, False))       # AIが話している途中で、何も言わずに切った
        self.assertEqual(got["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"
                                                      " WHERE phone_e164 = %s", (s["phone"],)))["n"], 1)
        t = self.fresh(stage_cap=20)
        self.order(t, self.incident(t))
        got = self.apology_call(t, cut=None)                            # 断ったかを決められない：何も決めずに残す
        self.assertTrue(got.get("held"))
        self.assertEqual(self.tx(t, lambda tx: next_action.current(tx, t["task"]))["kind"], "hold")
        unsettled = self.tx(t, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.attempts a WHERE a.task_id = %s AND a.purpose = 'apology'"
            " AND salescall.refusal_undecided(a.attempt_id)", (t["task"],)))["n"]
        self.assertEqual(unsettled, 1)                                   # 同じお客様のほかの行は、決まるまでかけない

    def test_other_stops_still_stop_it(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        self.tx(s, lambda tx: stops.stop(tx, scope="business", cause="manual", reason="試験：管理者か契約者が押した緊急停止",
                                         tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]), OPERATOR)
        self.assertEqual(self.reason(s), "stopped")                    # 管理者か契約者が押した緊急停止では止める

    def test_a_quality_stop_lets_the_apology_call_through(self):
        """品質の停止（言ってはいけないこと）でも、お詫びの電話はかける（決まった文面だけ＝業務の話し方を使わない。2026-10-16 ユーザー
        確認・2026-10-23 ユーザー「OK」。決まりはデータベースの表 salescall.stop_lets_through の1か所）。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        self.tx(s, lambda tx: stops.stop(tx, scope="business", cause="forbidden_speech", reason="試験：品質",
                                         tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        claim = self.claim(s)
        self.assertIsInstance(claim, gate.Claim, claim)
        self.assertEqual(claim.purpose, "apology")

    def test_frozen(self):
        s = self.fresh(stage_cap=20)
        self.incident(s)                                               # 案だけ（承認していない）
        own = owner_access()
        for sql in ("""UPDATE salescall.apology_orders SET pairs = '[{"wrong": "x", "correct": "y"}]'""",
                    "DELETE FROM salescall.apology_orders"):
            with self.subTest(sql=sql), self.assertRaises(Rejected) as e:
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))
            self.assertNotEqual(e.exception.sqlstate, "23503")         # 参照のつながりではなく、変更不可の仕掛けで断る

if __name__ == "__main__":
    unittest.main()
