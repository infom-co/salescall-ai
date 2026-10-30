"""管理者のご決定ごとの試験と、17-8 の誤りが無くなったことを示す場面（データベースを通す。DESIGN.md 17章の作り直しの4段目。持ち主 CL-2）。

(1) `Decided`：Intent.md の原則6・8-8・10-3・18-3 にある 2026-10-24〜10-29 の管理者のご決定（DESIGN.md 17-4 の対応表の行）ごとに、
    1つ以上の場面を、本物の部品（回線ごとの処理・門・発信の出口・締めの仕事・状態の表を引く入口）とデータベースで通す。試験の名前の
    頭の数字が、ご決定の日付（1024＝2026-10-24。夜はその日の夜）。
(2) `Errors178`：DESIGN.md 17-8 に書いた今の作りの誤り (1)〜(8) と、3段目の比べで見つけた誤り（声が聞き取れずに終えた行を同じ回線で
    すぐかけ直す・同じ行を休みとも復旧待ちとも答える）が、4段目（読み手が状態の欄を読む）で無くなったことを示す場面
    （試験の名前は `test_s17_…`）。
時計は進めない（その時が来たことにするのは、回線の欄・行の欄の次に動く時刻を直に前にする＝`due`・`voice_due`）。
⚠️ 試験のデータは架空のものだけ（番号は架空の範囲）。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from salescall.core import calling_hours, gate, health, next_action, registry, ring_test, scheduler, settings, troubles
from salescall.core.health_rules import LineStage, NumberStage, Wait
from salescall.core.phone import normalize
from salescall.supervisor import jobs, line
from salescall.supervisor.links import NoLinks
from tests import support
from tests.support import ADMIN, OPERATOR, SYSTEM, fictional_jp_fixed, owner_access
from tests.test_gate import HOLDER, FakeDriver
from tests.test_supervisor import PROBED, Blame, Runner
from tests.test_troubles import OurTroubles


class DecidedBase(OurTroubles, Blame):
    """場面の補助（回線の確かめ＝Blame の補助・こちらの不具合＝OurTroubles の補助）。この組そのものの試験は、ここでは流さない
    （`load_tests`）。"""

    def round_(self, s, d, runner=None):
        return Blame.round_(self, s, d, runner)

    def now(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])

    def current(self, s, task=None):
        return self.tx(s, lambda tx: next_action.current(tx, task or s["task"]))

    def blame(self, s, aid):
        return self.tx(s, lambda tx: health.blame_of(tx, aid))

    def stage(self, s, task=None):
        return self.tx(s, lambda tx: support.task_stage(tx, task or s["task"]))

    def phone_of(self, r):
        return self.tx(r, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s", (r["contact"],))["phone_e164"])

    def silent_rest(self, s):
        """その回線を、テスト番号が鳴らなかった休み（回線のせい・休み）にする（鳴らない番号へ1本かけ、15分たち、テスト番号も鳴らない）。"""
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.due(s)
        self.round_(s, FakeDriver(ring=False))
        self.assertEqual(self.line_of(s).stage, LineStage.LINE_REST)

    def poor(self, s, n):
        """違うお客様 n 人と、声が聞き取れずに終える（行は終える＝ほかの行へ進むため）。"""
        for _ in range(n):
            r = self.row(s)
            self.trouble(s, "poor_audio", answered=True, task=r["task"])
            self.finish(s, r["task"])

    def clear_requests(self, assignee, kind):
        """修正依頼は全体で1つ（同じ不具合）：前の試験の分は「直した」にしておく。"""
        self.report_fixed(assignee, kind)


class Decided(DecidedBase):
    """管理者のご決定ごとの場面（DESIGN.md 17-4）。"""

    # ── 2026-10-24
    def test_1024_a_number_fault_ends_the_row_and_a_line_fault_only_pauses_it(self):
        """2026-10-24：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめでなく一時中止
        だけですよ。」（R-2・R-3・L-10・N-3）。番号のせい（テスト番号が鳴った）で並びを使い切れば取りやめ、回線のせい（鳴らなかった）なら
        かけた予定のまま（番号に数えない）で、その回線は休み。"""
        s = self.ready()
        self.put_client(s, "dial.failed_retry_days", [1])                    # 試験：番号のせいが1回で並びを使い切る
        aid = self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.w.test_number_silent(s)
        jobs.settle_ready(self.ctx(s))
        d = self.current(s)
        self.assertEqual((self.blame(s, aid), d["kind"], d["end_reason"]), ("line", "call", None))
        self.assertEqual((self.line_of(s).stage, self.tx(s, lambda tx: health.number_of(tx, s["contact"]).n)), (LineStage.LINE_REST, 0))
        t = self.ready()
        self.put_client(t, "dial.failed_retry_days", [1])
        aid = self.fail_once(t, self.Refuses(self.bad_number(t)))
        self.w.test_number_rang(t)
        jobs.settle_ready(self.ctx(t))
        d = self.current(t)
        self.assertEqual((self.blame(t, aid), d["kind"], d["end_reason"]), ("number", "end", "number_unusable"))

    def test_1024_an_unknown_call_moves_on_to_the_next_row_and_the_next_ring_decides(self):
        """2026-10-24：「回線か番号かどちらかわからない場合は、その行に固執するのでなく、1回目から、次の行に進むようにしてください。
        次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断できますよね。」（L-2・L-5・R-1）。テスト番号は鳴らさない。"""
        s = self.ready()
        self.numbered(s)
        aid = self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.assertEqual((self.blame(s, aid), self.stage(s)), ("pending", "await_decision"))
        other = self.row(s)
        d = FakeDriver(ring=True)
        got = self.round_(s, d)                                                # 同じ回線で次の行へ（その行は待つ）
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], other["task"])
        self.assertEqual((self.blame(s, aid), d.rings), ("number", []))       # 次の行がつながった：番号のせい

    def test_1024_with_no_rows_left_the_test_number_decides(self):
        """2026-10-24：「その回線にかける行が残っていないときは、『テスト番号』で決めます。」（L-7〜L-10）。鳴れば番号のせい、鳴らなければ
        回線のせい。"""
        for rang, blame in ((True, "number"), (False, "line")):
            with self.subTest(rang=rang):
                s = self.ready()
                phone = self.numbered(s)
                aid = self.fail_once(s, self.Refuses(self.bad_number(s)))
                self.due(s)
                d = FakeDriver(ring=rang)
                self.round_(s, d)
                self.assertEqual((d.rings, self.blame(s, aid)), ([phone], blame))

    def test_1024_resuming_a_resting_line_is_checked_by_the_test_number_not_by_the_app(self):
        """2026-10-24「OK」（回線が止まったあとの再開の確かめも、テスト番号で。アプリが応答するかだけでは再開しない。L-15・L-16）。"""
        s = self.ready()
        phone = self.numbered(s)
        self.silent_rest(s)
        del PROBED[:]
        jobs.stop_checks(jobs.Context(self.w.a, self.cfg(s, SALESCALL_PROBER="tests.test_supervisor:make_prober"), HOLDER, NoLinks(),
                                      s["tenant"]))
        self.assertEqual((PROBED, self.line_of(s).stage), ([], LineStage.LINE_REST))
        self.due(s)
        d = FakeDriver(ring=True)
        self.round_(s, d)
        self.assertEqual((d.rings, self.line_of(s).stage), ([phone], LineStage.HEALTHY))

    def test_1024_an_unknown_call_is_not_counted_on_the_number_until_it_is_decided(self):
        """2026-10-24（「明らかに番号に問題がある場合は…取りやめ」）：番号の回数に数えるのは、番号のせいと決まった電話だけ（N-1）。同じ番号の
        ほかの回線の、まだ分からない電話は、番号を使えないと決める数にも、次にかける日を送る数にも入れない（決まるまで待つ＝N-4 の印だけ）。"""
        s = self.ready()                                                       # 番号の決まった順は初めの値（当日2回・翌日・2日後）
        a1 = self.fail_once(s, self.Refuses(self.bad_number(s)))             # 回線 A：まだ分からない
        b = self.w.line(s["tenant"])
        self.lease(s, line=b)
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])         # 同じ番号の、ほかの行
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        line.one_round(self.w.a, SYSTEM, s["tenant"], b, HOLDER, self.Refuses(self.bad_number(s)), Runner())
        jobs.settle_ready(self.ctx(s))
        self.w.test_number_rang(s, line=b)                                     # 回線 B の電話だけ、番号のせいと決まった
        num = self.tx(s, lambda tx: health.number_of(tx, s["contact"]))
        today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        self.assertEqual((num.stage, num.n, num.unknown, num.next_day), (NumberStage.RUN, 1, frozenset({str(a1)}), today))   # 当日の2回目
        self.assertFalse(self.tx(s, lambda tx: health.number_dead(tx, s["contact"])))
        t = self.ready()                                                       # 並びが2回（試験）：決まった1回と分からない1回で使い切らない
        self.put_client(t, "dial.failed_retry_days", [1, 1])
        self.fail_once(t, self.Refuses(self.bad_number(t)))
        c = self.w.line(t["tenant"])
        self.lease(t, line=c)
        t2 = self.w.task(t["tenant"], t["client"], self.w.run(t["tenant"], t["client"], t["business"], t["version"]), t["contact"])
        self.tx(t, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        line.one_round(self.w.a, SYSTEM, t["tenant"], c, HOLDER, self.Refuses(self.bad_number(t)), Runner())
        jobs.settle_ready(self.ctx(t))
        self.w.test_number_rang(t, line=c)
        self.assertEqual(self.tx(t, lambda tx: health.number_of(tx, t["contact"]).stage), NumberStage.RUN)

    # ── 2026-10-24 夜
    def test_1024_night_outside_the_ringing_hours_the_line_waits_for_their_start(self):
        """2026-10-24 夜：「私の電話です。…この仕事専用なら夜でもOK」（L-8・L-15）：鳴らしてよい時間の外は、テスト番号を鳴らさず、時間の
        始まりまで待つ（待つ理由と次に動く時刻を回線の欄に持つ）。"""
        s = self.ready()
        self.numbered(s)
        here = calling_hours.local(self.now(s))
        start = (here + timedelta(hours=2)).strftime("%H:%M")
        self.set_ring_hours(s, (start, (here + timedelta(hours=3)).strftime("%H:%M")))
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.due(s)
        d = FakeDriver(ring=True)
        self.round_(s, d)
        ln = self.line_of(s)
        self.assertEqual((d.rings, ln.stage, ln.wait), ([], LineStage.CHECK_B, Wait.OFF_HOURS))
        self.assertEqual(calling_hours.local(ln.next_at).strftime("%H:%M"), start)

    def test_1024_night_a_serious_trouble_and_poor_audio_are_told_apart(self):
        """2026-10-24 夜（甚大な問題の定義）：こちらのプログラムが意図しない状態に陥った電話（E3）は、その行が復旧を待ち（R-5）、声が
        ほとんど聞き取れない電話（E4）は、こちらの不具合に数えず、声の通る回線を待ってかけ直す（R-11）。"""
        s = self.ready()
        self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
        self.assertEqual(self.stage(s), "await_recovery")
        t = self.ready()
        self.trouble(t, "poor_audio", answered=True, task=t["task"])
        self.assertEqual(self.stage(t), "voice_redial")
        self.assertEqual((self.rows_counted(t, "call_poor_audio"), self.line_of(t).m), (0, 1))

    # ── 2026-10-25
    def test_1025_the_contractor_chooses_the_ringing_hours_in_any_number_of_ranges(self):
        """2026-10-25：「止める回線を2択だけじゃなく、こちらが決められるようにして。…0～24時の選択は残したうえで。」（L-8・L-15。鳴らしてよい
        時間は契約者の設定。いくつでも・日をまたいでも・0〜24時も）。初めの値は 7〜22時の1つ。"""
        s = self.ready()
        phone = self.numbered(s)                                               # 0:00〜24:00
        initial = owner_access().in_system(SYSTEM, lambda tx: settings.initial_value(tx, "line.test_ring_hours"))
        self.assertEqual(initial, [["07:00", "22:00"]])
        here = calling_hours.local(self.now(s))
        far = ((here + timedelta(hours=3)).strftime("%H:%M"), (here + timedelta(hours=4)).strftime("%H:%M"))
        near = ((here - timedelta(minutes=30)).strftime("%H:%M"), (here + timedelta(minutes=30)).strftime("%H:%M"))   # 日をまたぎうる
        self.set_ring_hours(s, far, near)                                      # 2つ目の範囲がいまを含む
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.due(s)
        d = FakeDriver(ring=True)
        self.round_(s, d)
        self.assertEqual(d.rings, [phone])

    def test_1025_the_gaps_are_15_30_60_120_240_minutes_per_contractor(self):
        """2026-10-25：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」（L-10・L-17：続けて鳴らなかった t 回目のあとは並びの t 番目。
        最後の長さで止まる）。"""
        s = self.ready()
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: settings.resolve(
            tx, "line.test_ring_gaps", tenant_id=None, client_id=None, business_id=None)), [900, 1800, 3600, 7200, 14400])
        self.numbered(s)
        self.gaps(s, 60, 120)
        self.silent_rest(s)
        steps = [int((self.line_of(s).next_at - self.line_of(s).tested_at).total_seconds())]
        for _ in range(2):
            self.due(s)
            self.round_(s, FakeDriver(ring=False))
            ln = self.line_of(s)
            steps.append(int((ln.next_at - ln.tested_at).total_seconds()))
        self.assertEqual(steps, [60, 120, 120])

    def test_1025_the_same_row_twice_moves_on_and_three_rows_stop_new_calls(self):
        """2026-10-25：同じ行で、こちらのプログラムの不具合が「2回続いたら、次の行へ。3行同じ問題なら、甚大な問題」（R-8・C-1）：同じ行の
        2回でその行は休み、回線は次の行へかける。違う3行で同じ理由なら、その契約者の新しい電話を止める。"""
        s = self.ready()
        self.trouble(s, "cannot_proceed", task=s["task"])
        self.assertTrue(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：戻した"), OPERATOR))   # 復旧を待たずに戻した
        self.trouble(s, "cannot_proceed", task=s["task"], again=False)      # 同じ行の2回目：休む・違う行の数は1のまま
        self.assertEqual((self.resting(s, s["task"]), self.rows_counted(s, "call_cannot_proceed")), (True, 1))
        b = self.row(s)
        self.trouble(s, "cannot_proceed", task=b["task"], again=False)      # 回線は次の行へ（2行目）
        self.assertEqual(self.tenant_stops(s), [])
        c = self.row(s)
        self.trouble(s, "cannot_proceed", task=c["task"], again=False)      # 3行目
        self.assertEqual(self.tenant_stops(s), ["program_trouble"])

    # ── 2026-10-25 夜
    def test_1025_night_a_ringing_test_is_just_ok(self):
        """2026-10-25 夜：「鳴ったら、テストＯＫなだけじゃないの？」（L-9・L-16：鳴ったらあけない・1回で再開）。再開のあとの、次の鳴らなかった
        電話の確かめは、また並びの1つ目から。"""
        s = self.ready()
        self.numbered(s)
        self.gaps(s, 900, 1800)
        self.silent_rest(s)
        self.due(s)
        self.round_(s, FakeDriver(ring=True))
        ln = self.line_of(s)
        self.assertEqual((ln.stage, ln.t, ln.next_at, ln.tested_at), (LineStage.HEALTHY, 0, None, None))
        r = self.row(s)
        self.round_(s, self.Refuses(self.phone_of(r)))
        ln = self.line_of(s)
        self.assertEqual((ln.stage, (ln.next_at - ln.run_start).total_seconds()), (LineStage.CHECK_A, 900))

    def test_1025_night_fifteen_minutes_with_no_new_setting(self):
        """2026-10-25 夜：「その回線で次にかけるのは15分後と決めたんだから、15分になるんじゃないの？新たな設定はいらないでしょ？」（L-2・L-7）：
        確かめ中 (a) の長さは、あける長さの並びの1つ目（最初の鳴らなかった電話から）。別の設定は持たない。"""
        s = self.ready()
        self.gaps(s, 900, 1800)
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        ln = self.line_of(s)
        self.assertEqual((ln.stage, (ln.next_at - ln.run_start).total_seconds()), (LineStage.CHECK_A, 900))
        for gone in ("line.decide_horizon", "line.pending_alert_after"):
            self.assertIsNone(owner_access().in_system(SYSTEM, lambda tx, g=gone: tx.one(
                "SELECT key FROM salescall.setting_definitions WHERE key = %s", (g,))))

    def test_1025_night_busy_power_off_or_out_of_range_means_the_line_works(self):
        """2026-10-25 夜：「テスト番号が話し中・電源オフ・圏外だと分かるのなら、回線の問題じゃないんじゃないの？」（E6 の範囲）：電話の部品は
        それらを「発信できた」（True）と答え（link/phone.Phone.test_ring）、表は鳴った（E6）として、回線は元気・続きは番号のせい。"""
        class BusyTone(FakeDriver):
            def test_ring(self, to):   # テスト番号は話し中の音（電話の部品の答えは「発信できた」）
                self.rings.append(to)
                return True
        s = self.ready()
        self.numbered(s)
        aid = self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.due(s)
        self.round_(s, BusyTone())
        self.assertEqual((self.line_of(s).stage, self.blame(s, aid)), (LineStage.HEALTHY, "number"))

    def test_1025_night_an_unclear_answer_is_told_at_once_one_time(self):
        """2026-10-25 夜「OK」（見分けられないと返ったら、すぐ1回知らせる。L-11）：同じ続きで何度見分けられなくても、契約者と管理者に1回。
        電話をつなぐ部品の直す担当へ修正依頼。テスト番号で決めるのはやめない。"""
        s = self.ready()
        self.numbered(s)
        self.clear_requests("CL-2", "test_ring_undecided")
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        d = FakeDriver(ring=None)
        for _ in range(2):
            self.due(s)
            self.round_(s, d)
        self.assertEqual((len(d.rings), self.told(s), len(self.undecided_requests())), (2, (1, 1), 1))
        self.assertEqual(self.line_of(s).stage, LineStage.CHECK_B)

    def test_1025_night_plan_a_1_poor_audio_is_not_our_trouble(self):
        """2026-10-25 夜 (1)「案A」：「声が聞き取れない」は、こちらの不具合に数えない（行が休まない・発信が止まらない・知らせと修正依頼を
        出さない）。同じ回線の違うお客様には回線に数える（L-3・R-11）。"""
        s = self.ready()
        for _ in range(2):
            self.trouble(s, "poor_audio", answered=True, task=s["task"], again=True)
        self.assertFalse(self.resting(s, s["task"]))
        self.assertEqual((self.notices(s, "error_notice"), self.tenant_stops(s)), ((0, 0), []))
        self.assertEqual(self.line_of(s).m, 1)                                 # 同じお客様だけ：1人

    def test_1025_night_plan_a_2_a_resting_row_comes_back_with_the_fixed_version(self):
        """2026-10-25 夜 (2)「案A」：休んだ行が戻るのは、その不具合の修正依頼が「直した」になり、そのあとプログラムが替わったとき（R-10・
        E11）。版が替わっただけでは戻らない。"""
        s = self.ready()
        self.trouble(s, "cannot_proceed", task=s["task"])
        self.trouble(s, "cannot_proceed", task=s["task"], again=False)
        self.assertTrue(self.resting(s, s["task"]))
        self.new_version()
        jobs.fixed_stops(self.ctx(s))
        self.assertTrue(self.resting(s, s["task"]))
        self.report_fixed("CL-1", "call_cannot_proceed")
        self.new_version()
        jobs.fixed_stops(self.ctx(s))
        self.assertFalse(self.resting(s, s["task"]))

    def test_1025_night_plan_a_3_a_call_talked_without_our_trouble_counts_the_rows_again(self):
        """2026-10-25 夜 (3)「案A」：違う3行の数え方は、こちらの不具合なしに話し終えた電話があれば数え直す（C-3）。"""
        s = self.ready()
        self.finish(s, s["task"])
        for _ in range(2):
            r = self.row(s)
            self.trouble(s, "cannot_proceed", task=r["task"], again=False)
            self.finish(s, r["task"])
        self.assertEqual(self.rows_counted(s, "call_cannot_proceed"), 2)
        self.w.talked(s)
        self.assertEqual(self.rows_counted(s, "call_cannot_proceed"), 0)
        r = self.row(s)
        self.trouble(s, "cannot_proceed", task=r["task"], again=False)
        self.assertEqual(self.tenant_stops(s), [])

    # ── 2026-10-29
    def test_1029_a_voice_rest_rings_back_stops_again_on_one_call_and_can_be_resumed_by_hand(self):
        """2026-10-29：「テスト番号が鳴れば再開のあと、また聞き取れなければ止め直し、次に試すまで 15→30→60→120→240分あけます。+管理者か
        契約者が、直ったことを確かめて手で再開も可能」（L-3・L-23・L-24・L-21・【当(3)】）。"""
        s = self.ready()
        self.numbered(s)
        self.gaps(s, 900, 1800)
        self.finish(s, s["task"])
        self.poor(s, 2)
        self.assertEqual((self.line_of(s).stage, self.line_of(s).j), (LineStage.AUDIO_REST, 1))
        self.round_(s, FakeDriver(ring=True))                                  # すぐテスト番号：鳴って再開
        self.assertEqual(self.line_of(s).stage, LineStage.HEALTHY)
        self.poor(s, 1)                                                        # 1本で止め直し・並びの1つ目をあける
        ln = self.line_of(s)
        self.assertEqual((ln.stage, ln.j), (LineStage.AUDIO_REST, 2))
        self.assertAlmostEqual((ln.next_at - self.now(s)).total_seconds(), 900, delta=60)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: health.by_hand_line(tx, s["line"], "試験：声の受け渡しを直したと確かめた"))
        self.assertEqual(self.line_of(s).stage, LineStage.HEALTHY)

    def test_1029_a_call_heard_well_counts_the_voice_again_from_the_start(self):
        """2026-10-29（声が聞き取れずに止めた回線の再開）：声が聞き取れて話し終えた電話があれば、止め直しの数え方は最初に戻る（次は違う
        お客様2人で休み、すぐテスト番号で確かめる。L-1）。"""
        s = self.ready()
        self.numbered(s)
        self.finish(s, s["task"])
        self.poor(s, 2)
        self.round_(s, FakeDriver(ring=True))                                  # 鳴って再開（止め直しは1本で＝j が残る）
        self.assertEqual((self.line_of(s).stage, self.line_of(s).j), (LineStage.HEALTHY, 1))
        r = self.row(s)
        self.round_(s, FakeDriver())                                           # 声が聞き取れて話し終えた
        self.finish(s, r["task"])
        self.assertEqual(self.line_of(s).j, 0)
        self.poor(s, 1)
        self.assertEqual(self.line_of(s).stage, LineStage.HEALTHY)             # 1本では止めない

    def test_1029_a_temporary_test_number_in_the_settings_until_the_contractor_sets_one(self):
        """2026-10-29：「一時的なtestcallは環境設定に追加」（L-13・E14）：管理者が入れたシステムの初めの値のテスト番号を、自分の番号を持たない
        契約者が使い、テスト番号を待っていた回線はすぐ鳴らす。契約者が自分の番号を入れたら、そちらを使う。"""
        a = self.ready()
        self.set_ring_hours(a, ("00:00", "24:00"))
        self.fail_once(a, self.Refuses(self.bad_number(a)))
        self.due(a)
        self.round_(a, FakeDriver(ring=True))
        self.assertEqual(self.line_of(a).wait, Wait.NO_NUMBER)
        temp = normalize(fictional_jp_fixed()).e164
        self.addCleanup(lambda: self.w.a.in_system(ADMIN, lambda tx: settings.set_system_value(
            tx, "line.test_number", None, status="provisional", rationale="試験：片付け", review_when="試験")))
        self.w.a.in_system(ADMIN, lambda tx: settings.set_system_value(
            tx, "line.test_number", temp, status="provisional", rationale="試験：仮のテスト番号", review_when="試験"))
        self.assertEqual(self.line_of(a).stage, LineStage.CHECK_C)            # テスト番号が入った：すぐ鳴らす頼み
        d = FakeDriver(ring=True)
        self.round_(a, d)
        self.assertEqual(d.rings, [temp])
        b = self.ready()
        own = self.numbered(b)
        self.assertEqual(self.w.a.in_tenant(SYSTEM, b["tenant"], ring_test.number), own)

    def test_1029_one_column_each_for_line_row_number_and_contractor(self):
        """2026-10-29：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」（17-1）：どれも欄は1つ（鍵は回線・行・番号・契約者）で、
        読み手（門・候補・締め）は、その欄の段を読む。行は「休み」と「復旧待ち」を同時に持たない。"""
        keys = owner_access().in_system(SYSTEM, lambda tx: {r["t"]: r["c"] for r in tx.all(
            "SELECT c.conrelid::regclass::text AS t, string_agg(a.attname, ',') AS c FROM pg_constraint c"
            " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)"
            " WHERE c.contype = 'p' AND c.conrelid::regclass::text IN ('salescall.line_states', 'salescall.task_states',"
            " 'salescall.number_states', 'salescall.tenant_states', 'salescall.attempt_blames') GROUP BY 1")})
        self.assertEqual(keys, {"salescall.line_states": "line_id", "salescall.task_states": "task_id",
                                "salescall.number_states": "contact_id", "salescall.tenant_states": "tenant_id",
                                "salescall.attempt_blames": "attempt_id"})
        s = self.ready()
        self.trouble(s, "cannot_proceed", task=s["task"])
        self.assertEqual((self.stage(s), self.claim(s).reason), ("await_recovery", "awaiting_recovery"))
        self.trouble(s, "cannot_proceed", task=s["task"])                    # 復旧のあと同じ行で2回目：休み（復旧待ちとは答えない）
        self.assertEqual((self.stage(s), self.claim(s).reason), ("resting", "task_resting"))

    def test_1029_every_check_knows_which_step_and_how_many_times(self):
        """2026-10-29：「分からないで放置はないんじゃないですか？どの確認中で何回目かなどが分かるんじゃないの？」：確かめ中の回線は、段
        （(a)(b)(c)）・何回目（t）・待つ理由・次に動く時刻を、回線の欄に持つ。"""
        s = self.ready()
        self.numbered(s)
        self.gaps(s, 60, 120)
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.assertEqual(self.line_of(s).stage, LineStage.CHECK_A)
        for t in (1, 2):
            self.due(s)
            self.round_(s, FakeDriver(ring=None))
            ln = self.line_of(s)
            self.assertEqual((ln.stage, ln.t, ln.wait, ln.next_at - ln.tested_at),
                             (LineStage.CHECK_B, t, Wait.GAP, timedelta(seconds=60 * t)))

    def test_1029_out_of_use_is_set_by_a_person_in_charge_and_the_rows_go_to_other_lines(self):
        """2026-10-29：「使わないとはどういうこと？」⇒「使用停止」（L-14・L-22・L-27）：管理者か契約者が回線を使用停止にすると、その回線の
        まだ分からない続きは回線のせい（行は、かけた予定のまま、ほかの回線でかける）。"""
        s = self.ready()
        aid = self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual((self.line_of(s).stage.value, self.blame(s, aid)), ("使用停止", "line"))
        jobs.settle_ready(self.ctx(s))
        other = self.w.line(s["tenant"])
        self.lease(s, line=other)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        self.assertIsInstance(self.claim(s, line=other), gate.Claim)

    def test_1029_once_the_lines_are_ready_plan_a_redials_from_a_line_known_to_carry_voice(self):
        """2026-10-29：「回線が整えば、案Aでいい」（R-11・R-12）：声が聞き取れずに終えた行は、声が通ると分かっている回線（ほかの元気な回線）が
        空いたら、その回線からお詫びとともにかける。声が聞き取れなかった回線では、すぐにはかけない。"""
        s = self.ready()
        self.trouble(s, "poor_audio", answered=True, task=s["task"])
        self.assertEqual(self.claim(s).reason, "voice_redial")
        b = self.w.line(s["tenant"])
        self.lease(s, line=b)
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.tick(tx, b))   # 回線 B の毎回の頭（元気・声の続き0）
        c = self.claim(s, line=b)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual(c.purpose, "resume")

    def test_1029_1_plan_b_a_silent_line_is_told_only_when_the_next_test_is_silent_too(self):
        """2026-10-29「2.案B」（17-7-2 の (1)）：テスト番号が鳴らなかった回線は、その時から休み（お客様へかけない）。契約者と管理者に知らせる
        のは、次のテストでも鳴らなかったとき（L-10・L-17）。"""
        s = self.ready()
        self.numbered(s)
        self.silent_rest(s)
        self.assertEqual(self.notices(s, "stopped"), (0, 0))
        self.due(s)
        self.round_(s, FakeDriver(ring=False))
        self.assertEqual(self.notices(s, "stopped"), (1, 1))
        self.due(s)
        self.round_(s, FakeDriver(ring=False))
        self.assertEqual(self.notices(s, "stopped"), (1, 1))                   # この休みで1回

    def test_1029_2_two_different_customers_rest_a_line(self):
        """2026-10-29「3.2人」（17-7-2 の (2)）：声が聞き取れない電話が、違うお客様2人で続いたら声の不調・休み（回線の異常3件の基準は通さない）。
        同じお客様だけなら休まない。"""
        s = self.ready()
        for _ in range(2):
            self.trouble(s, "poor_audio", answered=True, task=s["task"], again=True)
        self.assertEqual(self.line_of(s).stage, LineStage.HEALTHY)
        t = self.ready()
        self.finish(t, t["task"])
        self.poor(t, 2)
        self.assertEqual(self.rests(t), [["L-3"]])

    def test_1029_4_plan_a_with_no_line_known_to_carry_voice_the_same_line_calls_back_after_fifteen_minutes(self):
        """2026-10-29「4.案A」（17-7-2 の (4)。R-13）：声が通ると分かっている回線が来ないまま15分（並びの1つ目）たったら、同じ回線でお詫びと
        ともにかけ直す。"""
        s = self.ready()
        self.trouble(s, "poor_audio", answered=True, task=s["task"])
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertIsNone(got.called)                                          # 15分のうちは、かけない
        self.voice_due(s, s["task"])
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        called = self.tx(s, lambda tx: tx.one("SELECT task_id, purpose FROM salescall.attempts WHERE attempt_id = %s", (got.called,)))
        self.assertEqual((called["task_id"], called["purpose"]), (s["task"], "resume"))

    def test_1029_n4_plan_a_the_other_rows_of_a_number_wait_while_the_unknown_call_would_use_up_today(self):
        """2026-10-29（N-4「案A」）：分からない電話を番号のせいに数えると、その番号の今日の分を使い切る（並びを使い切る）あいだは、同じ番号の
        ほかの行は、どの回線でもかけずに待つ。決まれば外れる。"""
        s = self.ready()
        self.put_client(s, "dial.failed_retry_days", [1])
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        self.assertTrue(self.tx(s, lambda tx: health.number_held(tx, s["contact"])))
        self.assertEqual(self.claim(s, task=t2).reason, "number_unconfirmed")
        self.w.test_number_rang(s)                                             # 番号のせいと決まった：並びを使い切った
        self.assertEqual(self.claim(s, task=t2).reason, "number_unusable")


class Errors178(DecidedBase):
    """DESIGN.md 17-8 の誤り (1)〜(8) と、3段目の比べで見つけた誤りが、4段目で無くなったことを示す場面。"""

    def test_s17_1_a_line_whose_test_does_not_ring_never_calls_a_customer(self):
        """17-8 の (1)：テスト番号が鳴らなかった回線へ、かけ続けた（5分休んで約束の電話を取った）。⇒ 4段目：その時から休み。約束の電話が
        いまでも、その回線ではかけない（ほかの回線でかける）。"""
        s = self.ready()
        self.numbered(s)
        self.silent_rest(s)
        p = self.row(s)
        self.tx(s, lambda tx: support.schedule_call(tx, p["task"], tx.one("SELECT now() AS t")["t"], contact_id=p["contact"],
                                                    cause="試験：約束", promised=True))
        d = FakeDriver(ring=False)
        got = self.round_(s, d)
        self.assertEqual((got.called, got.pause, d.calls), (None, "line_stopped", []))

    def test_s17_2_two_different_customers_rest_the_line_without_a_third(self):
        """17-8 の (2)：声の不調で、3人目まで止めなかった（回線の異常3件の基準）。⇒ 4段目：違うお客様2人で休み。"""
        s = self.ready()
        self.finish(s, s["task"])
        self.poor(s, 2)
        self.assertEqual(self.line_of(s).stage, LineStage.AUDIO_REST)
        d = FakeDriver()
        self.round_(s, d)
        self.assertEqual(d.calls, [])                                          # 3人目へかけない（すぐテスト番号を待つ）

    def test_s17_3_without_a_test_number_an_app_check_never_resumes(self):
        """17-8 の (3)：テスト番号が無い契約者の止めた回線は、アプリが応答するかだけで再開した。⇒ 4段目：「テスト番号を入れてください」と
        知らせて待つ。アプリの確かめでは再開しない。"""
        s = self.ready()
        self.finish(s, s["task"])
        self.poor(s, 2)
        self.round_(s, FakeDriver(ring=True))
        ln = self.line_of(s)
        self.assertEqual((ln.stage, ln.wait), (LineStage.AUDIO_REST, Wait.NO_NUMBER))
        self.assertEqual(self.told(s), (1, 1))
        del PROBED[:]
        jobs.stop_checks(jobs.Context(self.w.a, self.cfg(s, SALESCALL_PROBER="tests.test_supervisor:make_prober"), HOLDER, NoLinks(),
                                      s["tenant"]))
        self.assertEqual((PROBED, self.line_of(s).stage), ([], LineStage.AUDIO_REST))

    def test_s17_4_a_resting_line_whose_test_is_never_tried_is_told_once(self):
        """17-8 の (4)：休みの回線の確かめを PC の部品が試さないとき、知らせが無かった。⇒ 4段目：試せないまま15分（並びの1つ目）で、その
        休みで1回、契約者と管理者に知らせ、修正依頼（L-19）。"""
        s = self.ready()
        self.numbered(s)
        self.gaps(s, 900)
        self.silent_rest(s)
        self.clear_requests("CL-2", "test_ring_undecided")

        class NotTried(FakeDriver):
            def test_ring(self, to):
                self.rings.append(to)
                raise line.dial_exit.CallWithheld("試験：PC の部品が試さなかった")
        ln = self.line_of(s)
        now = self.now(s)
        self.put_line(s, replace(ln, entered_at=now - timedelta(minutes=16), tested_at=now - timedelta(minutes=16),
                                      next_at=now - timedelta(seconds=1)))
        for _ in range(2):
            self.round_(s, NotTried())
            self.due(s)
        self.assertEqual((self.told(s), len(self.undecided_requests())), ((1, 1), 1))

    def test_s17_5_the_test_number_rings_fifteen_minutes_after_the_first_unrung_call_even_with_rows_left(self):
        """17-8 の (5)：かけられる行がある限りテスト番号を鳴らさず、壊れた回線で一覧を一巡させた。⇒ 4段目：最初の鳴らなかった電話から15分で
        決める（かける行が残っていても）。"""
        s = self.ready()
        phone = self.numbered(s)
        self.gaps(s, 900)
        rows = self.rows(s, 4)
        d = self.RefusesSome([self.phone_of(r) for r in rows], ring=False)
        self.round_(s, d)
        self.round_(s, d)
        self.assertEqual(d.rings, [])                                          # 15分のうちは、次の行で決めにいく
        self.due(s)
        self.round_(s, d)
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.line_of(s).stage, LineStage.LINE_REST)
        self.assertLess(len(d.calls), len(rows))                               # 一覧を一巡させない

    def test_s17_6_one_ladder_for_the_next_test_and_no_line_stop_record(self):
        """17-8 の (6)：再開を確かめる間が3つ重なった（確かめの間隔のばたつきの倍・テスト番号の間・声の止め直しの間）。⇒ 4段目：回線の欄の
        1つの並び（何回目か＝t）だけ。回線の休みは停止の記録を作らない（見張りの確かめ・ばたつきの倍は回線にかからない）。"""
        s = self.ready()
        self.numbered(s)
        self.gaps(s, 60, 120, 180)
        self.silent_rest(s)
        seen = []
        for _ in range(3):
            ln = self.line_of(s)
            seen.append(int((ln.next_at - ln.tested_at).total_seconds()))
            self.due(s)
            self.round_(s, FakeDriver(ring=False))
        self.assertEqual(seen, [60, 120, 180])
        stops_of_line = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.stop_events WHERE scope = 'line' AND line_id = %s", (s["line"],)))["n"]
        self.assertEqual(stops_of_line, 0)

    def test_s17_7_after_a_voice_rest_rings_back_one_poor_call_stops_it_again(self):
        """17-8 の (7)：声で止めた回線を再開すると声の続きが0に戻り、止め直すには最初と同じ本数（実際は3本）が要った。⇒ 4段目：再開のあと、
        声が聞き取れて話し終えた電話が無いうちは、1本で止め直す（【当(3)】）。"""
        s = self.ready()
        self.numbered(s)
        self.finish(s, s["task"])
        self.poor(s, 2)
        self.round_(s, FakeDriver(ring=True))
        self.assertEqual(self.line_of(s).stage, LineStage.HEALTHY)
        self.poor(s, 1)
        self.assertEqual(self.line_of(s).stage, LineStage.AUDIO_REST)

    def test_s17_8_a_row_awaiting_recovery_can_be_put_back_by_hand(self):
        """17-8 の (8)：復旧待ちの行に手の出口が無かった（戻せるのは休んだ行だけ）。⇒ 4段目：管理者か契約者が理由を書いて戻せる（R-7）。"""
        s = self.ready()
        self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
        self.assertEqual(self.stage(s), "await_recovery")
        self.assertTrue(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：直ったことを確かめた"), OPERATOR))
        self.assertEqual(self.stage(s), "on_plan")
        self.assertEqual(self.current(s)["purpose"], "resume")                # お客様と話している途中だった：お詫びとともに

    def test_s17_9_a_row_ended_by_poor_audio_is_not_called_again_at_once_on_the_same_line(self):
        """3段目の比べで見つけた誤り：声が聞き取れずに終えた行を、同じ回線ですぐかけ直した。⇒ 4段目：声の通る回線を待つ（同じ回線では
        15分のうちはかけない。R-11・R-13）。ほかの行へはかける。"""
        s = self.ready()
        self.trouble(s, "poor_audio", answered=True, task=s["task"])
        other = self.row(s)
        got = self.round_(s, FakeDriver())
        called = self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s", (got.called,)))["task_id"]
        self.assertEqual(called, other["task"])
        self.assertNotIn(s["task"], [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10, line_id=s["line"]))])

    def test_s17_10_a_row_is_never_both_resting_and_awaiting_recovery(self):
        """3段目の比べで見つけた誤り：同じ行を、休み（task_resting）とも復旧待ち（awaiting_recovery）とも答えた（読み手ごとに別の関数）。
        ⇒ 4段目：行の欄は1つの段だけで、門・候補・不具合の部品が同じ欄を読む。"""
        s = self.ready()
        self.trouble(s, "cannot_proceed", task=s["task"])
        self.trouble(s, "cannot_proceed", task=s["task"], again=False)
        self.assertEqual((self.stage(s), self.claim(s).reason, self.resting(s, s["task"])), ("resting", "task_resting", True))
        self.assertTrue(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：戻した"), OPERATOR))
        self.assertEqual((self.stage(s), self.resting(s, s["task"])), ("on_plan", False))


def load_tests(loader, standard, pattern):
    """この組の試験だけを流す（補助のために読み込んだ Blame・OurTroubles の試験は、それぞれの組で流す）。"""
    suite = unittest.TestSuite()
    for cls in (Decided, Errors178):
        for name in sorted(n for n in vars(cls) if n.startswith("test_")):
            suite.addTest(cls(name))
    return suite
