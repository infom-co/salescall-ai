"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import time
import unittest
import unittest.mock
from dataclasses import replace
from datetime import timedelta

from salescall.core import apology, attempts, call_report, fix_requests, gate, next_action, quality_stage, recordings, reward, settle, stops
from salescall.core import dnc, health, tasks
from salescall.core.access import Rejected
from salescall.core.attempts import ForbiddenFinding, VerificationSummary
from salescall.core.call_report import Cut
from tests.support import ADMIN, OPERATOR, SYSTEM, owner_access, said
from tests.test_approval import TAKEN, Helpers
from tests.test_settle import rep

TEXT = ("先ほどのお電話で、誤って「初月は無料です」というご案内をしてしまいましたが、正しくは「初月は半額です」となります。"
        "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。")


class ApologyBase(Helpers):
    """お詫びの試験の土台（試験は持たない）。"""
    def incident(self, s):
        """言ってはいけないことを言った通話（その行は終わり、業務は止まる）。その試行を返す。"""
        self.verified_call(s, said(), items=TAKEN)
        return self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s"
                                            " ORDER BY claimed_at DESC LIMIT 1", (s["task"],)))["attempt_id"]

    def draft(self, s, aid):
        return self.tx(s, lambda tx: tx.one("SELECT order_id FROM salescall.apology_orders WHERE source_attempt_id = %s",
                                            (aid,)))

    def recorded(self, s, order_id, *, voice=None, same_day=None):
        """案の文の録音の組を作る（録音を作るのは CL-1 の録音の道具。ここでは試験の音声の参照）。声は誤った通話の声（voice で
        替えれば、データベースが断る）。same_day＝同じ日の部分の文（替えれば、データベースが断る）。"""
        def work(tx):
            o = tx.one("SELECT o.text_same_day, o.text_later, t.voice_name FROM salescall.apology_orders o"
                       " JOIN salescall.attempts a ON a.attempt_id = o.source_attempt_id"
                       " JOIN salescall.voice_types t ON t.code = a.voice_type WHERE o.order_id = %s", (order_id,))
            parts = [recordings.Part(apology.PARTS[True], same_day or o["text_same_day"], "test://sorry/same", "a" * 64),
                     recordings.Part(apology.PARTS[False], o["text_later"], "test://sorry/later", "b" * 64)]
            return recordings.create_set(tx, unit="apology", voice_name=voice or o["voice_name"], parts=parts, order_id=order_id)
        return self.tx(s, work, SYSTEM)

    def order(self, s, aid, actor=OPERATOR):
        """システムが作った案を、契約者が録音を聞いて承認する（録音の組の承認と同じ取引）。"""
        order_id = self.draft(s, aid)["order_id"]
        set_id = self.recorded(s, order_id)
        return self.tx(s, lambda tx: apology.approve(tx, order_id, set_id), actor)

    def apology_call(self, s, **report):
        aid = self.call(s, report=replace(rep(), **report))
        return self.settle(s, aid)

    def results(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.task_results WHERE task_id = %s",
                                            (s["task"],)))["n"]


class Apology(ApologyBase):

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
            self.order(s, aid, actor=SYSTEM)                          # システムは承認しない（契約者が録音を聞いて承認する）
        self.assertEqual(e.exception.constraint, "recording_approval_by_person")   # 録音の承認と同じ取引で断る
        order_id = self.draft(s, aid)["order_id"]
        heard = self.recorded(s, order_id)
        self.tx(s, lambda tx: recordings.approve(tx, heard), OPERATOR)  # 録音は契約者が聞いて承認したが…
        with self.assertRaises(Rejected) as e:                        # …お詫びの電話の承認は、システムはしない
            self.tx(s, lambda tx: apology.approve(tx, order_id, heard), SYSTEM)
        self.assertEqual(e.exception.constraint, "apology_approval_by_person")
        self.order(s, aid)
        got = self.tx(s, lambda tx: apology.for_task(tx, s["task"]))
        self.assertEqual(got["text"], TEXT)
        self.assertEqual((got["recording"]["part"], got["recording"]["audio_ref"]), ("apology_same_day", "test://sorry/same"))

    def test_the_apology_is_recorded_and_the_approval_binds_the_recording(self):
        """お詫びの電話の文は録音で流し、契約者は録音を聞いて承認する（CL-1 の連絡帳 4-40 の 4。ユーザー「OK」。0131）：
        録音の文は案の文と同じだけ・声は誤った通話の声だけ・2つそろってから承認・承認には承認した録音の組を必ず結ぶ。"""
        s = self.fresh(stage_cap=20)
        aid = self.incident(s)
        order_id = self.draft(s, aid)["order_id"]
        texts = self.tx(s, lambda tx: tx.one("SELECT text_same_day, text_later FROM salescall.apology_orders WHERE order_id = %s",
                                             (order_id,)))
        self.assertEqual(texts["text_same_day"], TEXT)
        self.assertEqual(texts["text_later"], TEXT.replace("先ほど", "先日", 1))
        with self.assertRaises(Rejected) as e:                        # 案と違う文の録音は、組に入れられない
            self.recorded(s, order_id, same_day=TEXT + "以上です。")
        self.assertEqual(e.exception.constraint, "apology_recording_text")
        other = self.tx(s, lambda tx: tx.one(
            "SELECT v.voice_name FROM salescall.voices v WHERE v.voice_name <> (SELECT t.voice_name FROM salescall.attempts a"
            " JOIN salescall.voice_types t ON t.code = a.voice_type WHERE a.attempt_id = %s) ORDER BY 1 LIMIT 1", (aid,)))["voice_name"]
        with self.assertRaises(Rejected) as e:                        # 誤った通話と別の声の録音は作れない
            self.recorded(s, order_id, voice=other)
        self.assertEqual(e.exception.constraint, "apology_recording_voice")
        half = self.tx(s, lambda tx: tx.one(
            "SELECT o.text_same_day, t.voice_name FROM salescall.apology_orders o JOIN salescall.attempts a"
            " ON a.attempt_id = o.source_attempt_id JOIN salescall.voice_types t ON t.code = a.voice_type WHERE o.order_id = %s",
            (order_id,)))
        partial = self.tx(s, lambda tx: recordings.create_set(
            tx, unit="apology", voice_name=half["voice_name"], order_id=order_id,
            parts=[recordings.Part(apology.PARTS[True], half["text_same_day"], "test://sorry/x", "c" * 64)]), SYSTEM)
        with self.assertRaises(Rejected) as e:                        # 同じ日・あとの日の2つがそろうまで承認しない
            self.tx(s, lambda tx: apology.approve(tx, order_id, partial), OPERATOR)
        self.assertEqual(e.exception.constraint, "apology_recording_complete")
        with self.assertRaises(Rejected) as e:                        # 録音の組を結ばない承認は無い
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.apology_approvals (tenant_id, client_id, order_id, approved_by)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor())", (order_id,)), OPERATOR)
        self.assertIn(e.exception.constraint, ("apology_approvals_recorded", "apology_approval_recording"))
        with self.assertRaises(Rejected) as e:                        # 聞いて承認していない録音の組も結べない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.apology_approvals (tenant_id, client_id, order_id, approved_by, set_id)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor(), %s)", (order_id, partial)),
                OPERATOR)
        self.assertEqual(e.exception.constraint, "apology_approval_recording")
        self.order(s, aid)
        got = self.tx(s, lambda tx: apology.for_task(tx, s["task"]))
        self.assertEqual(got["recording"]["part"], apology.PARTS[True])
        voice = self.tx(s, lambda tx: recordings.apology_voice(tx, s["task"]))
        self.assertEqual(voice, half["voice_name"])                   # お詫びの電話は、録音と同じ声で話す（門の choose_voice）
        checked = self.w.a.in_system(SYSTEM, lambda tx: {r["conname"]: r["convalidated"] for r in tx.all(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname IN ('apology_orders_texts', 'apology_approvals_recorded')")})
        self.assertEqual(checked, {"apology_orders_texts": True, "apology_approvals_recorded": True})   # 0131 より前の、文の無い案・
        # 録音の無い承認は残っていない（0137 が確かめた）＝流す文も録音も空にならない

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

    def test_the_next_band_waits_the_shortest_interval_after_the_last_apology_call(self):
        """届かなかったお詫びの電話の、別の時間帯へのかけ直しも、前のお詫びの電話から、同じお客様への最短の間隔（設定
        gate.same_contact_min_interval。依頼者ごと）をあける。あけた時刻を決めるのは門の数え方の1か所（gate.same_contact_free_at）で、次の動きは
        それを読む（⛔ 見直し23 の (8)：前は次の動きが同じ設定を別に読んでいた）。お詫びの電話は約束したかけ直しと同じく門の最短の間隔では
        止めないので、ここであけないと、すぐ次の時間帯の始まりにかけ直す。"""
        s = self.fresh(stage_cap=20)
        self.put_client(s, "gate.same_contact_min_interval", 3 * 86400)   # 試験：どの時間帯の始まりよりも長い間隔
        self.order(s, self.incident(s))
        got = self.apology_call(s, apology_delivered=False)
        ended = self.tx(s, lambda tx: tx.one("SELECT max(ended_at) AS t FROM salescall.attempts WHERE task_id = %s"
                                             " AND purpose = 'apology'", (s["task"],)))["t"]
        self.assertEqual((got["decision"].kind, got["decision"].target.purpose), ("call", "apology"))
        self.assertGreaterEqual(got["decision"].at, ended + timedelta(days=3))

    def test_an_apology_call_not_passed_on_is_not_tried_again_the_same_day(self):
        """お詫びの電話で、受付の方に「席を外しております」と言われ、また掛けてよいとは言われなかった：ふつうの電話と同じく、同じ日には
        かけ直さない（材料は判定の中身から＝next_action.unreached_of の1か所。2026-10-22 ユーザー：「同じ日に2回以上、その言葉がなく
        かけるのであれば煙たがられますよ」。見直し16b：前は、お詫びの電話の締めだけ会話の報告から直に読み、この場面の試験が無かった）。"""
        for again, same_day in ((True, True), (None, False)):
            with self.subTest(again=again):
                s = self.fresh(stage_cap=20)
                hour = self.tx(s, lambda tx: tx.one("SELECT extract(hour FROM salescall.jst(now()))::int AS h")["h"])
                if not 7 <= hour <= 20:
                    self.skipTest("日本時間の夜・早朝：今日のうちに別の時間帯が無い")
                self.put_client(s, "calling.ng_hours", {k: [] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")})
                self.put_client(s, "retry.time_band_boundaries", [hour + 1])   # 今日のうちに、まだ試していない時間帯がある
                self.order(s, self.incident(s))
                from salescall.core.call_report import Callback, Said
                cb = Callback("absent", None, None, None, Said(1, "あいにく岡田は席を外しております。"), again=again, back_today=again)
                report = replace(rep(("reception",), callback=cb, not_reached="absent"), apology_delivered=False)
                d = self.settle(s, self.call(s, report=report))["decision"]
                tomorrow = self.tx(s, lambda tx: tx.one(
                    "SELECT (date_trunc('day', salescall.jst(now())) + interval '1 day') AT TIME ZONE 'Asia/Tokyo' AS t")["t"])
                self.assertEqual((d.kind, d.target.purpose), ("call", "apology"))
                self.assertEqual(d.at < tomorrow, same_day, d.at)

    def test_a_cut_apology_call_is_called_back_at_once(self):
        """お詫びの電話が途中で切れた：ふつうの電話と同じく、復旧次第すぐかけ直す（続けて2回まで。⛔ 2026-10-22 の見直し14：前は
        お詫びの電話だけ、別の時間帯に回していた）。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"),
                        report=replace(rep(cut=None), apology_delivered=False))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.promised), ("call", "apology", True))
        # 決め手は「途中で切れた」の決まり（届かなかったときの「別の時間帯に」ではない。⛔ 見直し17b：かけてよい時間帯が1日じゅうの
        # 試験では、別の時間帯もすぐになるので、決まりを取り違えても気づかなかった）
        self.assertIn("途中で切れた", d.rule)
        c = self.claim(s)                                                # すぐ（別の時間帯を待たない）
        self.assertEqual((type(c).__name__, getattr(c, "purpose", None)), ("Claim", "apology"))

    def test_the_claim_tells_whether_the_wrong_call_was_today_by_the_same_rule_as_the_text(self):
        """お詫びの電話の予約証の apology_when（同じ日か）は、お詫びの文（先ほど／先日＝for_task）と同じく、誤った通話（案のもとの通話）の
        日で決める（core/apology.when の1か所。CL-1 の連絡帳 4-99）。今日、お詫びの電話が途中で切れていても、誤った通話が前の日なら later。"""
        s = self.fresh(stage_cap=20)
        aid = self.incident(s)
        self.order(s, aid)
        c = self.claim(s)
        self.assertEqual((c.purpose, c.apology_when), ("apology", "same_day"))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))

        def back(tx):   # 試験：誤った通話を前の日にする（持ち主の取引の中だけ仕掛けを外す）
            tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
            tx.execute("UPDATE salescall.attempts SET claimed_at = claimed_at - interval '1 day',"
                       " ended_at = ended_at - interval '1 day' WHERE attempt_id = %s", (aid,))
            tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
        owner_access().in_system(SYSTEM, back)
        cut = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"),
                        report=replace(rep(cut=None), apology_delivered=False))   # 今日のお詫びの電話が途中で切れた
        self.settle(s, cut)
        c = self.claim(s)
        text = self.tx(s, lambda tx: apology.for_task(tx, s["task"]))["text"]
        self.assertEqual((c.purpose, c.apology_when, text[:2]), ("apology", "later", "先日"))

    def test_an_apology_call_that_did_not_ring_through_no_fault_of_the_number_is_made_again_as_planned(self):
        """お詫びの電話が、番号のせいでなく鳴らなかった（こちらのせい＝こちらのプログラムの不具合で終えた。0136）：ふつうの電話と同じ
        1か所（next_action._line_fault_again）で、かけた予定のまま（お詫びの印も）、復旧次第かけ直す（それまでは門が待たせる＝
        行の欄の「復旧待ち」。17章の R-5）。届かなかった回数にも数えない（⛔ 見直し19a の壊し試し：お詫びの電話のこの道を外しても落ちる
        試験が無かった）。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        aid = self.call(s, ("dialing", "ringing"))
        self.assertTrue(self.tx(s, lambda tx: attempts.end_for_trouble(tx, aid, "ai_malfunction")))
        self.tx(s, lambda tx: health.after_call(tx, aid))                      # 電話が終わった所（回線ごとの処理）と同じく
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "ours")
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.promised), ("call", "apology", True))
        self.assertIn("こちらのせい", d.rule)
        self.assertEqual(self.reason(s), "awaiting_recovery")                 # 復旧するまで待つ
        self.w.talked(s)                                                       # 同じ契約者で、こちらの不具合なしに話し終えた
        self.assertIsInstance(self.claim(s), gate.Claim)                       # 復旧次第すぐ（お詫びの印のまま）

    def test_an_apology_call_waits_for_the_voice_the_apology_was_recorded_in(self):
        """お詫びの電話は、契約者が聞いて承認したお詫びの録音と同じ声だけで話す（gate.choose_voice の1か所。0131）：その声で話せなく
        なったら、話してよいほかの声があっても、その声では話さずに待つ（⛔ 見直し19a の壊し試し：声の縛りを外しても落ちる試験が
        無かった）。"""
        s = self.fresh(stage_cap=20)
        aid = self.incident(s)
        self.order(s, aid)
        voice = self.tx(s, lambda tx: tx.one("SELECT t.voice_name FROM salescall.attempts a JOIN salescall.voice_types t"
                                             " ON t.code = a.voice_type WHERE a.attempt_id = %s", (aid,)))["voice_name"]
        with self.w.common_missing(voice, recordings.COMMON_PARTS[0]):    # その声の共通の一言（システム）が欠けた
            others = self.tx(s, lambda tx: recordings.ready_voices(tx, s["version"]))
            self.assertTrue(others and voice not in others)                    # ほかの声なら話せる
            c = self.claim(s)
            self.assertIsInstance(c, gate.Denied, c)

    def test_an_apology_call_is_neither_counted_in_the_stage_nor_measured(self):
        """お詫びの電話（決まった文面だけ）は、品質の段の件数にも、誤判定率の抜き取りで測る通話にも入れない（決めるのは
        salescall.fixed_text_kind の1か所。門・候補を出す処理と同じ。⛔ 見直し17b：前は purpose を直書きしていた）。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        before = self.tx(s, lambda tx: quality_stage.used(tx, s["version"]))
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"),
                        report=replace(rep(), apology_delivered=True))
        self.settle(s, aid)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.used(tx, s["version"])), before)
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.call_to_measure(%s) AS m", (aid,))["m"]))

    def test_refusal_ends_and_registers_dnc(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, refusals=rep(dnc_=True).refusals)
        self.assertEqual(got["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"
                                                      " WHERE phone_e164 = %s", (s["phone"],)))["n"], 1)

    def test_a_disagreed_refusal_in_an_apology_call_waits_for_the_recording_check(self):
        """お詫びの電話の断りも、ふつうの通話と同じ1か所で保留にする（2026-10-30 管理者のご決定 (6)）：2つ目の判定とそろわなければ、
        発信禁止を登録せず、行は判定待ち。お詫びの電話は締めずにおき、録音の確かめでそろったら締める。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, refusals=rep(dnc_=True).refusals, irreversible_check="disagreed")
        self.assertEqual((got.get("held"), got.get("waiting")), (True, "recording"))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "hold")
        count = lambda: self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"   # noqa: E731
                                                     " WHERE phone_e164 = %s", (s["phone"],)))["n"]
        self.assertEqual(count(), 0)
        aid = self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND purpose = 'apology'",
                                           (s["task"],)))["attempt_id"]
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, replace(rep(), refusals=rep(dnc_=True).refusals,
                                                                        irreversible_check="agreed"), evidence="試験：録音から"))
        self.tx(s, settle.rejudge_held)
        self.assertEqual(count(), 1)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "do_not_call")

    def test_revoking_an_apology_call_refusal_puts_the_row_back(self):
        """お詫びの電話で断られた（そろった）⇒ 発信禁止・行は終わり。そのあと依頼者からの正式な求めで、その通話から入れた発信禁止を取り
        消した ⇒ 断りを外して、お詫びの電話の次の動きを決め直す＝行は「お詫びの電話の断り」で終わったままにしない（2026-10-30 (7)。
        ⛔ 見直し24a の (5)：前は、締めてあるお詫びの電話の締め直しが何もせず、行が終わったままだった）。伝えたかの記録はそのまま。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, refusals=rep(dnc_=True).refusals, apology_delivered=True)
        self.assertEqual(got["decision"].end_reason, "do_not_call")
        aid = self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND purpose = 'apology'",
                                           (s["task"],)))["attempt_id"]
        self.tx(s, lambda tx: dnc.revoke_source(tx, aid, "依頼者からの正式な求め（試験）"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], "apology_done")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.apology_outcomes WHERE attempt_id = %s",
                                                      (aid,)))["n"], 1)

    def test_refusal_is_decided_in_the_one_place(self):
        """お詫びの電話の拒否も、判定と同じ1か所（judge.refusal）で決める（2026-10-16 の見直し J6）。
        断りの言葉のあとに切った ⇒ 断った（発信禁止。2026-10-30 管理者の指摘：断ったのは営業拒否の言葉があったときだけ）。断ったかを
        決められない（切れ方の材料が無い）⇒ もうかけない。"""
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, cut=Cut("ai", False, True))        # AIが話している途中で、断りの言葉のあとに切った
        self.assertEqual(got["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"
                                                      " WHERE phone_e164 = %s", (s["phone"],)))["n"], 1)
        u = self.fresh(stage_cap=20)
        self.order(u, self.incident(u))
        got = self.apology_call(u, cut=Cut("ai", False, False))       # 断りの言葉が無いまま、何も言わずに切った：断ったとは決めない
        self.assertNotEqual(got["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(u, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"
                                                      " WHERE phone_e164 = %s", (u["phone"],)))["n"], 0)
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



class ApologyAfterDialFix(ApologyBase):
    """言ってはいけないことのあとのお詫びの電話が番号のせいでかけられずに終えた行を、番号を直して戻すとき（✅ 2026-11-06 ユーザー＝計画書
    7-1 (12)：「言ってはいけないことを言った同じ相手なら、新しい番号へお詫びの電話をかけます。違う相手の場合は、お詫びなしで、どこまで
    引き継がれているのかを確認しつつ、続きからか、途中から営業を続けます。」。戻す形は core/next_action._restart の1か所・同じ相手かは
    `_target_changed_since` の1か所）。"""

    def ended_apology(self, s, *, items=TAKEN, report=None, other_person=False, end="number_unusable"):
        """言ってはいけないことを言った通話（items・report の結果）→ お詫びを承認 →（other_person なら、お呼びする方を直す）→ お詫びの電話が
        番号のせいで終わった（鳴らない電話が続いた＝end の初めの値。apology_unreached＝決めた回数かけても届かなかった）。お詫びの予定の
        番号（seq）を返す。"""
        from salescall.core import row_fixes
        aid = self.call(s, verify=None, items=items, **({"report": report} if report is not None else {}))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
        self.settle(s, aid)
        self.order(s, aid)
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["kind"], cur["purpose"]), ("call", "apology"))
        if other_person:
            self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部"), OPERATOR)
        self.tx(s, lambda tx: next_action.end(tx, s["task"], end, cause="試験：お詫びの電話が終わった"))
        return cur["seq"]

    def fixed(self, s):
        from salescall.core import row_fixes
        from tests.support import fictional_jp_fixed
        return self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)["contact_id"]

    def plan(self, s):
        return self.tx(s, lambda tx: next_action.current(tx, s["task"]))

    def test_the_same_person_gets_the_apology_on_the_new_number(self):
        """言ってはいけないことを言った相手と同じ（お呼びする方を直していない）⇒ 新しい番号へ、お詫びの電話（承認した案・文・録音のまま）。
        業務を終えていても同じ（お詫びは営業の電話ではない）。"""
        s = self.fresh(stage_cap=20)
        self.ended_apology(s)
        before = self.tx(s, lambda tx: apology.for_task(tx, s["task"]))
        new = self.fixed(s)
        p = self.plan(s)
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"], p["apology_reasons"]),
                         ("call", "apology", new, True, False, []))
        self.assertEqual(self.tx(s, lambda tx: apology.for_task(tx, s["task"]))["order_id"], before["order_id"])

    def test_another_person_gets_a_sales_call_that_checks_the_handover_without_an_apology(self):
        """お呼びする方を直した（違う相手）・業務が続いていた（必須の質問が取れていない）⇒ お詫びなしで、引き継ぎを確かめる印を付けた
        ふつうの電話を、新しい番号へすぐ。予約証にも印が写る（会話の処理が、どこまで引き継がれているかを確かめる＝CL-1）。"""
        s = self.fresh(stage_cap=20)
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.ended_apology(s, items=(), other_person=True)
        new = self.fixed(s)
        p = self.plan(s)
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"], p["apology_reasons"]),
                         ("call", "normal", new, False, True, []))
        time.sleep(1.1)                                                  # 同じお客様への最短の間隔（試験は1秒）のあと
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual((c.purpose, c.handover, c.apology), ("normal", True, ()))

    def test_choosing_another_person_when_fixing_the_number_counts_as_another_person(self):
        """✅ 2026-11-07 ユーザー（伺い「相手の違い」に「番号を直す画面で選ぶ」＝計画書 7-1 (19)）：お呼びする方の欄は直さず、番号を
        直すときに契約者が「担当の方は違う」を選んだ ⇒ 違う相手（お詫びなしで、引き継ぎを確かめる営業の電話）。「同じ」を選べば、
        新しい番号へお詫び（next_action._target_changed_since の1か所。⛔ 前は欄を直したかだけで決め、番号だけ直すと同じ相手にした）。"""
        from salescall.core import row_fixes
        from tests.support import fictional_jp_fixed
        for same, want in ((False, ("normal", True)), (True, ("apology", False))):
            with self.subTest(same_person=same):
                s = self.fresh(stage_cap=20)
                self.ended_apology(s, items=())
                new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=same),
                              OPERATOR)["contact_id"]
                p = self.plan(s)
                self.assertEqual((p["kind"], p["contact_id"], (p["purpose"], p["handover"])), ("call", new, want))

    def test_an_apology_to_the_old_person_after_another_person_fix_goes_on_as_a_handover(self):
        """「担当の方は違う」で番号を直したとき、まだかけていないお詫びの電話は、前の番号で前の担当の方へ1回（計画書 7-1 (17)）。
        (1) その電话で受付の方と話してお詫びが届かなかった ⇒ 新しい番号の新しい担当の方へは、お詫びなしで引き継ぎを確かめる電話（基準は
        言ってはいけないことを言った通話＝`_carried_since`。そのあとに前の番号で話した通話ではない。0202 で、外しても落ちない壊し試しを
        見つけて足した）。(2) 前の担当の方がお詫びの電話で「もうかけないで」と断った ⇒ 行を終えず同じ形。止めるのは前の担当の方の番号だけ
        （計画書 7-1 (22)。お詫びの電話の締めも `_former_refused`・`dnc.refusal_reach` の同じ1か所）。"""
        from salescall.core import row_fixes
        from tests.support import fictional_jp_fixed
        for refused in (False, True):
            with self.subTest(refused=refused):
                s = self.fresh(stage_cap=20)
                self.put_client(s, "gate.same_contact_min_interval", 1)
                aid = self.call(s, verify=None, items=())
                self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
                self.settle(s, aid)
                self.order(s, aid)
                new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=False),
                              OPERATOR)["contact_id"]
                p = self.plan(s)
                self.assertEqual((p["kind"], p["purpose"], p["contact_id"]), ("call", "apology", s["contact"]))   # 前の番号で1回
                time.sleep(1.1)                                                  # 同じお客様への最短の間隔（試験は1秒）のあと
                report = rep(dnc_=True) if refused else replace(rep(("reception",), not_reached="absent"), apology_delivered=False)
                ap = self.call(s, report=report)
                self.settle(s, ap)
                p = self.plan(s)
                self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"], p["apology_reasons"]),
                                 ("call", "normal", new, False, True, []))
                if refused:
                    new_phone = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                                             (new,)))["phone_e164"]
                    self.assertEqual((self.tx(s, lambda tx: dnc.lookup(tx, s["phone"])) is not None,
                                      self.tx(s, lambda tx: dnc.lookup(tx, new_phone)) is not None), (True, False))

    def test_the_handover_mark_stays_until_the_target_is_reached(self):
        """引き継ぎを確かめる電話に出なかった ⇒ 次の電話にも印を付ける（まだ確かめていない）。目的のお客様と話せたら外す
        （`next_action._handover_due` の1か所＝いまの担当の期の通話で、目的のお客様とまだ話していない。0203）。"""
        s = self.fresh(stage_cap=20)
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 9)       # 試験：同じお客様へ今日かける回数を使い切らない
        self.ended_apology(s, items=(), other_person=True)
        self.fixed(s)
        time.sleep(1.1)                                                  # 同じお客様への最短の間隔（試験は1秒）のあと
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual((d.kind, self.plan(s)["handover"]), ("call", True))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        from tests import support
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=d.target.contact_id, cause="試験：いまかける",
                                                    promised=False))
        self.assertTrue(self.plan(s)["handover"])                         # 予定を書くたびに求める（まだ目的のお客様と話していない）
        time.sleep(1.1)
        d = self.settle(s, self.call(s, report=rep(callback=support.callback("later", time="あとで"))))["decision"]
        self.assertEqual((d.kind, self.plan(s)["handover"]), ("call", False))

    def test_a_customer_who_ended_the_business_gets_no_sales_call_from_another_person(self):
        """違う相手でも、言ってはいけないことを言った通話で業務を終えていた（目的を果たした＝会社としての答え）なら、営業の電話にしない
        （計画書 7-1 (2)「業務を終えた行は戻しません」）。前の担当の方が用件を断った（その方個人の答え）なら、お呼びする方が替わったあとは、新しい
        担当の方へ引き継ぎを確かめる営業の電話（✅ 2026-11-07 ユーザーの新しい決定：直したあとに断ったとき＝計画書 7-1 (24) とそろえる。
        next_action.answer_still_ends の1か所）。"""
        from tests.test_judge import goal_refused
        for name, kw, want in (("completed", {}, ("end", None)), ("declined", {"items": (), "report": goal_refused(rep(), ("Q1",))}, ("call", True))):
            with self.subTest(name=name):
                s = self.fresh(stage_cap=20)
                seq = self.ended_apology(s, other_person=True, **kw)
                state = self.tx(s, lambda tx: apology.source_call(tx, s["task"]))["state"]
                self.assertEqual(state, name)
                new = self.fixed(s)
                p = self.plan(s)
                if want[0] == "end":
                    self.assertEqual((p["kind"], p["end_reason"]), ("end", "number_unusable"))
                    self.assertNotIn("normal", [r["purpose"] for r in self.tx(s, lambda tx: tx.all(
                        "SELECT purpose FROM salescall.task_next_actions WHERE task_id = %s AND seq > %s", (s["task"], seq)))])
                else:
                    self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["handover"], p["apology_reasons"]),
                                     ("call", "normal", new, True, []))

    def kept_old_promise_forbidden(self, s, **kw):
        """「違う」・「約束も守る」で直し、前の番号の約束の電話で、前の担当の方（田中様）に言ってはいけないことを言った ⇒ お詫びの電話（前の番号）。
        新しい担当の方の番号を返す。"""
        from datetime import timedelta
        from salescall.core import row_fixes
        from tests import support
        from tests.support import fictional_jp_fixed
        self.put_client(s, "gate.same_contact_min_interval", 1)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：約束", promised=True, window=w))
        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=False, keep_old_promise=True),
                      OPERATOR)["contact_id"]
        aid = self.call(s, verify=None, **kw)
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
        self.settle(s, aid)
        self.order(s, aid)
        p = self.plan(s)
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"]), ("call", "apology", s["contact"]))
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        time.sleep(1.1)
        return new

    def test_an_apology_to_the_old_person_never_goes_to_the_new_person(self):
        """指摘2・5・6：「約束も守る」の約束の電話で前の担当の方（田中様）に言ってはいけないことを言った。田中様へのお詫びは田中様個人のこと
        （✅ 2026-11-07 本体の決定＝計画書 7-1 (12)(22)(24) にそろえる）：お詫びが届いても・断られても・届かずに番号を直しても、新しい担当の方
        （鈴木様）へはお詫びなしで、引き継ぎを確かめる営業の電話。田中様が用件を断った（declined＝その方個人の答え）通話でも同じ。言ってはいけない
        ことを言った通話が会社としての答え（目的を果たした）なら、行を終える。
        ⛔ 0202：(指摘2) 元の通話が「違う」の直しより後なので同じ相手とみなし、お詫びが鈴木様へ行った・(指摘5) 用件の断りを業務を終えた結果と
        みなし、鈴木様へかからなかった・(指摘6) お詫びが届いたときだけ、行がお詫び済みで終わった。"""
        from salescall.core import row_fixes
        from tests.support import fictional_jp_fixed
        from tests.test_judge import goal_refused
        handed = lambda s, new: (lambda p: (p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"],   # noqa: E731
                                            p["apology_reasons"]))(self.plan(s)) == ("call", "normal", new, False, True, [])
        for case, kw in (("incomplete", dict(items=())), ("declined", dict(items=(), report=goal_refused(rep(), ("Q1",))))):
            for after in ("delivered", "refused", "unusable"):
                with self.subTest(case=case, after=after):
                    s = self.fresh(stage_cap=20)
                    new = self.kept_old_promise_forbidden(s, **kw)
                    if after == "unusable":                                       # お詫びの番号が鳴らずに終わり、番号を「同じ」で直す
                        self.tx(s, lambda tx: next_action.end(tx, s["task"], "number_unusable", cause="試験：お詫びの番号が鳴らない"))
                        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True),
                                      OPERATOR)["contact_id"]
                    else:
                        report = replace(rep(), apology_delivered=True) if after == "delivered" else rep(dnc_=True)
                        self.settle(s, self.call(s, report=report))
                    self.assertTrue(handed(s, new), self.plan(s))
        s = self.fresh(stage_cap=20)                                              # 会社としての答え（目的を果たした）：行を終える
        self.kept_old_promise_forbidden(s, items=TAKEN)
        self.settle(s, self.call(s, report=replace(rep(), apology_delivered=True)))
        p = self.plan(s)
        self.assertEqual((p["kind"], p["end_reason"]), ("end", "apology_done"))

    def test_only_a_sales_call_carries_the_handover_mark(self):
        """引き継ぎを確かめる印を持てるのは、ふつうの電話・続きの電話の予定だけ（データベースが守る＝0194 の task_next_actions_handover_calls）。"""
        from salescall.core.access import Rejected
        s = self.fresh(stage_cap=20)
        with self.assertRaises(Rejected) as e:   # 印は予定を書く1か所が求めるので、試験は表へ直に書く
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, cause, promised, contact_id,"
                "                                         purpose, handover)"
                " VALUES (%s, %s, %s, 'call', now(), '試験', true, %s, 'apology', true)",
                (s["tenant"], s["client"], s["task"], s["contact"])))
        self.assertEqual(e.exception.constraint, "task_next_actions_handover_calls")

    def test_after_a_name_fix_the_apology_stays_with_the_person_who_heard_it(self):
        """お呼びする方の名前などを直した（番号は同じ＝いまかける番号が、言ってはいけないことを聞いた前の担当の方の番号でもある）⇒ お詫びの電話は、
        その番号のまま前の担当の方の期（その番号で電話を受けた方への電話＝next_action._plan_person の、お詫び・番号の聞き直しの決まり。0206）。
        届かずにかけ直すときも前の担当の方へ（新しい担当の方へお詫びしない）。届いたら、新しい担当の方へ引き継ぎを確かめる電話（前の担当の方への
        お詫びの電話のあと＝after_apology）。⛔ 0205 まで：いまかける番号は、いまの担当の方の番号なので、かけ直しの予定が新しい担当の方の期になった
        （前の担当の方へのお詫びをやめて引き継ぎにしていた。お詫びの元の通話で業務を終えていれば、お詫びが新しい担当の方の期の予定になった）。"""
        from salescall.core import row_fixes
        from tests import support
        s = self.fresh(stage_cap=20)
        self.put_client(s, "gate.same_contact_min_interval", 1)
        aid = self.call(s, verify=None, items=())
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
        self.settle(s, aid)
        self.order(s, aid)
        before = self.plan(s)
        self.assertEqual((before["purpose"], before["contact_id"], before["person_since"]), ("apology", s["contact"], None))
        self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部"), OPERATOR)
        self.assertEqual(self.plan(s)["seq"], before["seq"])                   # 予定はそのまま（前の担当の方へのお詫び）
        time.sleep(1.1)
        first = self.call(s, report=rep())                                      # 届かなかった
        self.settle(s, first)
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, first)))
        p = self.plan(s)
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["person_since"]), ("call", "apology", s["contact"], None))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：いまかける", promised=True,
                                                    purpose="apology"))
        time.sleep(1.1)
        self.settle(s, self.call(s, report=replace(rep(), apology_delivered=True)))
        p = self.plan(s)
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"], p["apology_reasons"]),
                         ("call", "normal", s["contact"], False, True, []))

    def test_after_a_name_fix_an_apology_that_never_reaches_the_old_person_hands_over_to_the_new_person(self):
        """お呼びする方の名前などを直したあと、前の担当の方へのお詫びの電話（同じ番号・前の担当の方の期）を、ふつうのお詫びのかけ直しの決まりで
        決めた回数（retry.apology_max）かけても届かなかった ⇒ 行を終えず、新しい担当の方へ引き継ぎを確かめる電話（前の担当の方へのお詫びの電話の
        あと＝after_apology。届いたとき＝上の試験と同じ形。0206 の形を、届かずに回数を使い切った場面でも確かめる。0208）。"""
        from salescall.core import row_fixes
        s = self.fresh(stage_cap=20)
        self.put_client(s, "retry.apology_max", 2)
        self.put_client(s, "gate.same_contact_min_interval", 1)
        aid = self.call(s, verify=None, items=())
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=said()))
        self.settle(s, aid)
        self.order(s, aid)
        self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部"), OPERATOR)
        time.sleep(1.1)
        first = self.apology_call(s, apology_delivered=False)["decision"]      # 1回目：届かなかった ⇒ 前の担当の方へ、お詫びのかけ直し
        self.assertEqual((first.kind, first.target.purpose, first.target.contact_id), ("call", "apology", s["contact"]))
        self.assertIsNone(self.plan(s)["person_since"])
        self.now_on(s, s["contact"])
        time.sleep(1.1)
        self.apology_call(s, apology_delivered=False)                          # 2回目：届かずに回数を使い切った
        p = self.plan(s)
        self.assertEqual((p["kind"], p["purpose"], p["contact_id"], p["promised"], p["handover"], p["apology_reasons"]),
                         ("call", "normal", s["contact"], False, True, []))
        self.assertIsNotNone(p["person_since"])                                 # 新しい担当の方の期

    def test_an_apology_call_settled_before_a_later_fix_keeps_its_person(self):
        """お詫びの電話を締めた時刻（salescall.settled_at）は、お詫びの電話の結び（apology_outcomes。お詫びの電話は業務の判定の版を書かない）でも
        決まる：いまの担当の方がお詫びの電話で断り、そのあと契約者が「担当の方は違う」で直しても、その通話は前の担当の方への通話にならない（断りの
        範囲＝dnc.refusal_reach が、あとから狭まらない。0203 の見直し H の (B3) と同じ決まりを、お詫びの電話でも確かめる）。"""
        from salescall.core import row_fixes
        from tests.support import fictional_jp_fixed
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        self.apology_call(s, refusals=rep(dnc_=True).refusals)
        aid = self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND purpose = 'apology'",
                                           (s["task"],)))["attempt_id"]
        self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=False), OPERATOR)
        self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.assertEqual(self.tx(s, lambda tx: dnc.refusal_reach(tx, aid)), self.tx(s, lambda tx: dnc.reach(tx, s["task"])))

    def now_on(self, s, contact):
        """試験の近道：別の時間帯を待たずに、いま、その番号へお詫びの電話をかける予定にする。"""
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        from tests import support
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=contact, cause="試験：いまかける", promised=True,
                                                    purpose="apology"))

    def test_an_unreached_apology_comes_back_by_the_same_rule_as_one_ended_by_the_number(self):
        """✅ 2026-11-06 ユーザー＝計画書 7-1 (14)：お詫びの電話を決めた回数かけてもお客様に届かずに終えた行（apology_unreached）も、
        番号を直したら「戻す」。戻し方は番号のせいで終えた行と同じ（next_action._restart_apology の1か所）：同じ相手 ⇒ 新しい番号へお詫び／
        違う相手 ⇒ お詫びなしで引き継ぎを確かめる営業の電話／違う相手で業務を終えていた ⇒ 戻さない。戻すかは
        next_action.reopens_on_fix の1か所（決めた回数で終えた＝COUNT_ENDS）。"""
        for other, items, want in ((False, TAKEN, ("call", "apology", True, False)), (True, (), ("call", "normal", False, True)),
                                   (True, TAKEN, None)):
            with self.subTest(other=other, items=bool(items)):
                s = self.fresh(stage_cap=20)
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.ended_apology(s, items=items, other_person=other, end="apology_unreached")
                new = self.fixed(s)
                p = self.plan(s)
                if want is None:   # 業務を終えていた（すべて取れた）お客様へ、違う相手として営業の電話はかけない
                    self.assertEqual((p["kind"], p["end_reason"]), ("end", "apology_unreached"))
                else:
                    self.assertEqual((p["kind"], p["purpose"], p["promised"], p["handover"], p["contact_id"]), (*want, new))

    def test_an_apology_put_back_on_the_new_number_counts_from_zero(self):
        """✅ 2026-11-06 ユーザー＝計画書 7-1 (15)：番号を直して戻したお詫びの電話の回数（retry.apology_max まで）は「数え直す」＝新しい
        番号で0回から（数えるのは gate.purpose_dials の counted の1か所）。前の番号で決めた回数かけて届かずに終えた行を戻し、新しい番号で
        届かなくても、すぐには終えず、別の時間帯にかけ直す（⛔ 0195 の前は前の番号の回数も数え、新しい番号で1回かけただけで終えた）。"""
        s = self.fresh(stage_cap=20)
        self.put_client(s, "retry.apology_max", 2)
        self.order(s, self.incident(s))
        self.apology_call(s, apology_delivered=False)
        self.now_on(s, s["contact"])
        got = self.apology_call(s, apology_delivered=False)
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "apology_unreached"))   # 前の番号で2回
        new = self.fixed(s)
        self.assertEqual((self.plan(s)["purpose"], self.plan(s)["contact_id"]), ("apology", new))
        d = self.apology_call(s, apology_delivered=False)["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "apology", new))         # 新しい番号で1回目

    def test_an_apology_on_the_old_number_rings_there_once_and_then_moves_to_the_new_number(self):
        """✅ 2026-11-06 ユーザー（伺い「直したあとのお詫び」に「約束の電話と同じ」＝計画書 7-1 (17)）：番号を直したとき、前の番号への
        お詫びの電話がまだ鳴っていなければ、前の番号で1回かけ、届かなければ新しい番号へお詫び（同じ相手）。もう鳴っていれば、直した
        ときに新しい番号へ移す（`_old_number_moves` の1か所＝約束の電話の `_point` と同じ物差し）。新しい番号の回数は0から（計画書 7-1 (15)）。
        ⛔ 0195 の初めの作りは、前の番号で直したときから数え直して、もう決めた回数かけていた。"""
        with self.subTest("まだ鳴っていない"):
            s = self.fresh(stage_cap=20)
            self.put_client(s, "retry.apology_max", 2)
            old = s["contact"]
            self.order(s, self.incident(s))
            new = self.fixed(s)
            self.assertEqual((self.plan(s)["purpose"], self.plan(s)["contact_id"]), ("apology", old))   # 前の番号で1回
            self.now_on(s, old)
            d = self.apology_call(s, apology_delivered=False)["decision"]      # 届かなかった ⇒ 新しい番号へ（前の番号へは1回まで）
            self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "apology", new))
            d = self.apology_call(s, apology_delivered=False)["decision"]      # 新しい番号で1回目：まだ終えない（0から数える）
            self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "apology", new))
        with self.subTest("もう鳴っていた"):
            s = self.fresh(stage_cap=20)
            self.put_client(s, "retry.apology_max", 2)
            old = s["contact"]
            self.order(s, self.incident(s))
            self.apology_call(s, apology_delivered=False)                     # 直す前に前の番号で1回鳴った
            self.assertEqual((self.plan(s)["purpose"], self.plan(s)["contact_id"]), ("apology", old))
            new = self.fixed(s)
            self.assertEqual((self.plan(s)["purpose"], self.plan(s)["contact_id"]), ("apology", new))   # 直したときに新しい番号へ
            d = self.apology_call(s, apology_delivered=False)["decision"]      # 新しい番号で1回目：まだ終えない（前の番号の1回を足さない）
            self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "apology", new))

    def test_an_apology_on_the_old_number_that_did_not_ring_also_moves_to_the_new_number(self):
        """直したあと前の番号へかけたお詫びの電話が、お客様のところで鳴らなかった（発信に失敗）ときも、前の番号へは1本までなので、
        新しい番号へお詫び（約束の電話と同じ＝直したあと前の番号へ1本かければ、結果によらず次は新しい番号。計画書 7-1 (4)・(17)）。
        ⛔ 2026-11-06 の見直し：前は鳴ったかだけを見て、前の番号へ決まった順（当日・翌日・2日後）でかけ続けた。"""
        s = self.fresh(stage_cap=20)
        old = s["contact"]
        self.order(s, self.incident(s))
        new = self.fixed(s)
        self.now_on(s, old)
        aid = self.call(s, ("dialing", "call_failed"))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "apology", new))

    def test_calls_to_a_number_are_counted_per_number_from_the_latest_fix(self):
        """別の時間帯にかけ直す電話（お詫び・番号の聞き直し）の回数は、その番号へ、かける番号を直したあとにかけた電話だけ（1か所＝
        gate.purpose_dials の counted。計画書 7-1 (3)・(15)「新しい番号で数え直す」）。直したあと前の番号へかけた電話も、その番号の
        回数には入る（⚠️ 行の回数の範囲 attempts.in_row_count は、それを除くので使わない：番号の聞き直しは直したあとも前の番号へかけ続ける
        ＝next_action.current_dial。除くと回数が増えず、決めた回数で終わらない）。"""
        s = self.fresh(stage_cap=20)
        self.put_client(s, "retry.apology_max", 5)
        old = s["contact"]
        self.order(s, self.incident(s))
        self.apology_call(s, apology_delivered=False)                         # 直す前に前の番号で1回（数えない）
        new = self.fixed(s)
        self.now_on(s, old)
        self.apology_call(s, apology_delivered=False)                         # 直したあと前の番号で1回（前の番号の回数に入る）
        count = lambda number: self.tx(s, lambda tx: next_action._new_band_retry(   # noqa: E731
            tx, s["task"], tx.one("SELECT now() AS t")["t"], purpose="apology", number=number).tries)
        self.assertEqual((count(old), count(new)), (1, 0))

    def test_an_apology_on_the_old_number_after_a_fix_moves_on_by_the_same_rule(self):
        """番号を直したあと、お詫びの電話（前の番号のまま＝その番号のお客様へかける電話）が番号違いだった（番号のせい）⇒ 終えずに、
        同じ1か所（`_restart`）で、新しい番号へお詫び（同じ相手）。門が前の番号を「鳴らない電話が続いた」で止めたときも同じ。"""
        for path in ("call", "gate"):
            with self.subTest(path=path):
                s = self.fresh(stage_cap=20)
                self.order(s, self.incident(s))
                new = self.fixed(s)
                self.assertEqual(self.plan(s)["purpose"], "apology")
                if path == "call":
                    d = self.settle(s, self.call(s, report=replace(rep(("wrong_party",), cut=Cut("ai", True, False)),
                                                                   apology_delivered=False)))["decision"]
                else:
                    with unittest.mock.patch.object(next_action, "usable", return_value=next_action.Target(None, "number_unusable")):
                        d = self.tx(s, lambda tx: next_action.after_denial(
                            tx, s["task"], gate.denied("number_unusable"), tenant_id=s["tenant"], client_id=s["client"],
                            business_id=s["business"]))
                self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "apology", new))


if __name__ == "__main__":
    unittest.main()
