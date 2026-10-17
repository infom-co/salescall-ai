"""お客様が言った別の番号へのかけ直しの試験（DESIGN.md 3-5・3-6・5-2-2・7-2-1）。

- データベースの約束：行の番号の表（一覧の番号は自動で入る・変えられない）／発信と予定は、その行の番号にしかかけられない／
  1つの行に進行中の発信は1つだけ。
- 流れ：締め（行に番号を足す・かけるかを決める）→ 次の予定（番号つき）→ 門（予定の番号へかける）。
"""

from __future__ import annotations

import time
import unittest
import uuid
from datetime import timedelta

from salescall.core import attempts, calling_hours, dnc, gate, next_action, number_recheck, results, tasks
from salescall.core.access import Rejected
from salescall.core.call_report import Callback
from salescall.core.phone import normalize
from tests.support import CLEAN, OPERATOR, SYSTEM, fictional_jp_fixed, owner_access
from tests.test_settle import SettleBase, rep

NO_ANSWER = ["dialing", "no_answer"]


def jp() -> str:
    return normalize(fictional_jp_fixed()).e164


def other(said, *, e164=True, at=None):
    """「こちらの番号にかけ直して」の報告（番号は架空の日本の番号）。"""
    return rep(("reception",), not_reached=None, callback=Callback(
        "other_number", at, said, normalize(said).e164 if e164 else None, None))


class Base(SettleBase):
    def ready_jp(self, **kw):
        return self.ready(phone=fictional_jp_fixed(), **kw)

    def now(self, s, plus="0 seconds"):
        return self.tx(s, lambda tx: tx.one("SELECT now() + %s::interval AS t", (plus,))["t"])

    def numbers(self, s):
        return self.tx(s, lambda tx: tx.all("SELECT contact_id, phone_e164, source, source_attempt_id"
                                            " FROM salescall.task_numbers WHERE task_id = %s ORDER BY added_at",
                                            (s["task"],)))

    def current(self, s):
        return self.tx(s, lambda tx: next_action.current(tx, s["task"]))

    def redirect(self, s, said=None, *, at_plus="0 seconds", **kw):
        """一覧の番号にかけ、受付で「こちらの番号に（at_plus のあと）かけ直して」と言われて締める。"""
        said = said or fictional_jp_fixed()
        aid = self.call(s, report=other(said, at=self.now(s, at_plus), **kw))
        return said, aid, self.settle(s, aid)


class DatabasePromises(Base):
    def test_list_number_is_added_with_the_task(self):
        s = self.ready()
        self.assertEqual([(r["contact_id"], r["source"]) for r in self.numbers(s)], [(s["contact"], "list")])
        self.assertEqual(self.tx(s, lambda tx: tasks.numbers(tx, s["task"])), [s["phone"]])

    def test_numbers_are_frozen_and_list_number_is_the_task_contact(self):
        s = self.ready()
        own = owner_access()
        for sql in ("UPDATE salescall.task_numbers SET added_at = now() WHERE task_id = %s",
                    "DELETE FROM salescall.task_numbers WHERE task_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql, (s["task"],)))
        k2 = self.w.contact(s["tenant"], s["client"], jp())
        with self.assertRaises(Rejected) as e:   # 2つ目の「一覧の番号」・連絡先でない番号を一覧の番号と名乗らせない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source)"
                " SELECT %s, %s, %s, contact_id, phone_e164, 'list' FROM salescall.contacts WHERE contact_id = %s",
                (s["tenant"], s["client"], s["task"], k2)))
        self.assertEqual(e.exception.constraint, "task_numbers_one_list_number")

    def test_callee_number_needs_a_call_of_the_same_row(self):
        s = self.ready()
        t2 = self.ready()
        k2 = self.w.contact(s["tenant"], s["client"], jp())
        stranger = self.w.insert_claim(t2)             # ほかの依頼者の行の試行
        for source_attempt in (None, stranger, uuid.uuid4()):
            with self.subTest(source_attempt=source_attempt), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(
                    "INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source,"
                    " source_attempt_id) SELECT %s, %s, %s, contact_id, phone_e164, 'callee', %s"
                    " FROM salescall.contacts WHERE contact_id = %s",
                    (s["tenant"], s["client"], s["task"], source_attempt, k2)))

    def test_calls_and_schedules_only_to_numbers_of_the_row(self):
        s = self.ready()
        k2 = self.w.contact(s["tenant"], s["client"], jp())
        phone2 = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s", (k2,)))
        with self.assertRaises(Rejected) as e:
            self.w.insert_claim(dict(s, contact=k2, phone=phone2["phone_e164"]))
        self.assertEqual(e.exception.constraint, "attempts_number_of_task")
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], self.now(s), contact_id=k2, cause="試験",
                                                            promised=False))
        self.assertEqual(e.exception.constraint, "task_next_actions_number_of_task")
        with self.assertRaises(Rejected) as e:   # かけ直す予定は、必ず番号を持つ（処理を通らずに書いても）
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, cause, promised)"
                " VALUES (%s, %s, %s, 99, 'call', now(), '試験', false)", (s["tenant"], s["client"], s["task"])))
        self.assertEqual(e.exception.constraint, "task_next_actions_number")

    def test_one_active_call_per_row(self):
        s = self.ready_jp()
        said, aid, _ = self.redirect(s)
        alt = self.numbers(s)[1]
        self.w.insert_claim(s)                                            # 一覧の番号への予約
        with self.assertRaises(Rejected) as e:
            self.w.insert_claim(dict(s, contact=alt["contact_id"], phone=alt["phone_e164"]),
                                line=self.w.line(s["tenant"]))                # 同じ行の別の番号（別の回線）
        self.assertEqual(e.exception.constraint, "attempts_one_active_per_task")


    def test_expired_claim_on_another_number_of_the_row_is_released(self):
        """期限の切れた予約が、同じ行のほかの番号への発信を止めたままにしない（門が同じ行の分も取り消す）。"""
        s = self.ready_jp()
        self.redirect(s, at_plus="1 hour")
        alt = self.numbers(s)[1]["contact_id"]
        stale = self.w.insert_claim(s, line=self.w.line(s["tenant"]), expires_in=timedelta(seconds=1))   # 一覧の番号
        time.sleep(1.5)
        self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], self.now(s), contact_id=alt, cause="試験",
                                                        promised=True))
        self.assertIsInstance(self.claim(s), gate.Claim)
        self.assertEqual(self.state(s, stale), "released")


class Flow(Base):
    def test_calls_the_number_the_callee_gave(self):
        s = self.ready_jp()
        said, aid, got = self.redirect(s)
        self.assertEqual((got["state"], got["decision"].kind, got["decision"].promised), ("callback_scheduled", "call", True))
        nums = self.numbers(s)
        self.assertEqual([(n["phone_e164"], n["source"], n["source_attempt_id"]) for n in nums],
                         [(s["phone"], "list", None), (normalize(said).e164, "callee", aid)])
        self.assertEqual(self.current(s)["contact_id"], nums[1]["contact_id"])
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual(c.phone_e164, normalize(said).e164)            # 門は予定の番号へかける（一覧の番号ではない）
        detail = self.tx(s, lambda tx: results.latest(tx, s["task"]))["detail"]
        self.assertEqual(detail["callback"]["number_said"], said)          # 依頼者へは、お客様が言ったままの番号も渡る

    def test_retries_stay_on_the_number_the_callee_gave(self):
        s = self.ready_jp()
        said, _, _ = self.redirect(s)
        self.settle(s, self.call(s, NO_ANSWER))                            # 別の番号で出なかった
        nxt = self.current(s)
        self.assertEqual((nxt["kind"], nxt["promised"], nxt["contact_id"]), ("call", False, self.numbers(s)[1]["contact_id"]))

    def test_records_only_when_the_business_does_not_call_other_numbers(self):
        s = self.ready_jp()
        self.put_client(s, "callback.call_alternate_number", False)
        said, _, got = self.redirect(s)
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "alternate_not_called"))
        self.assertEqual(len(self.numbers(s)), 2)                           # かけなくても行の番号には足す（3-6）
        self.assertEqual(self.reason(s), "task_ended")

    def test_records_only_when_the_number_must_not_be_called(self):
        s = self.ready_jp()
        _, _, got = self.redirect(s, "0570-000-002")                       # ナビダイヤル（初めの設定でかけない）
        self.assertEqual(got["decision"].end_reason, "alternate_blocked:number_kind_not_allowed")
        s2 = self.ready_jp()
        said = fictional_jp_fixed()
        self.tx(s2, lambda tx: dnc.register(tx, said, "manual"))
        _, _, got = self.redirect(s2, said)
        self.assertEqual(got["decision"].end_reason, "alternate_blocked:dnc_client")

    def test_number_that_cannot_be_normalized_is_recorded_only(self):
        s = self.ready_jp()
        _, _, got = self.redirect(s, "ゼロサンの、内線の…", e164=False)
        self.assertEqual((got["state"], got["decision"].end_reason), ("callback_scheduled", "alternate_blocked:number_unknown"))
        self.assertEqual(len(self.numbers(s)), 1)

    def test_same_number_as_the_list_number(self):
        s = self.ready_jp()
        _, _, got = self.redirect(s, s["phone"])
        self.assertEqual(len(self.numbers(s)), 1)
        self.assertEqual((got["decision"].kind, self.current(s)["contact_id"]), ("call", s["contact"]))

    def test_refusal_on_the_other_number_stops_every_number_of_the_row(self):
        s = self.ready_jp()
        said, _, _ = self.redirect(s)
        got = self.settle(s, self.call(s, report=rep(dnc_=True)))         # 別の番号で、本人に断られた
        self.assertEqual(got["state"], "do_not_call")
        for phone in (s["phone"], said):
            self.assertEqual(self.tx(s, lambda tx: dnc.lookup(tx, normalize(phone).e164))["ledger"], "client")

    def test_refusal_in_the_same_call_also_stops_the_given_number(self):
        s = self.ready_jp()
        said = fictional_jp_fixed()
        report = rep(("reception",), dnc_=True, callback=Callback("other_number", None, said, normalize(said).e164, None))
        got = self.settle(s, self.call(s, report=report))
        self.assertEqual(got["state"], "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: dnc.lookup(tx, normalize(said).e164))["ledger"], "client")


class SameParty(Base):
    """同じお客様＝かける番号が同じ発信、または同じ行の発信（番号を変えて回数・間隔をすり抜けない）。"""

    def test_daily_limit_and_interval_count_the_row(self):
        s = self.ready_jp()
        self.put_client(s, "gate.max_dials_per_contact_per_day", 1)
        said, _, _ = self.redirect(s)
        alt = self.numbers(s)[1]["contact_id"]
        self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], self.now(s), contact_id=alt, cause="試験：約束でない",
                                                        promised=False))
        self.assertIn(self.reason(s), ("daily_attempts", "same_contact_interval"))   # 一覧の番号へかけた1回を数える
        self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], self.now(s), contact_id=alt, cause="試験：約束",
                                                        promised=True))
        self.assertIsInstance(self.claim(s), gate.Claim)                  # 約束したかけ直しは止めない

    def test_unsettled_call_blocks_numbers_it_could_stop(self):
        s = self.ready_jp()
        said, _, _ = self.redirect(s, at_plus="1 hour")                   # 行 A：一覧の番号 X ＋ 別の番号 Y
        other_run = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        b = dict(s, run=other_run, contact=self.numbers(s)[1]["contact_id"], phone=normalize(said).e164)
        b["task"] = self.w.task(s["tenant"], s["client"], other_run, b["contact"])     # 行 B：一覧の番号が Y
        self.tx(b, lambda tx: next_action.schedule_now(tx, b["task"], cause="試験"))
        self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], self.now(s), contact_id=s["contact"], cause="試験",
                                                        promised=True))
        self.call(s, NO_ANSWER[:1])                                         # 行 A が X にかけている（締めはまだ）
        self.assertEqual(self.reason(b), "related_unsettled")              # A が断られたら Y も止まる ⇒ 締めまで待つ
        self.assertIsNone(self.tx(b, lambda tx: gate.pacing(tx, b["task"])).earliest)


class WrongNumber(Base):
    """お客様が言った番号が番号違いだったとき（2026-10-15 ユーザー決定）：証拠と付け合わせ（番号の確かめ）、聞き違いなら正しい番号へ、
    言われたとおりなら、番号を聞いた通話の番号へかけ、番号違いだったことを告げて、もう一度番号を伺う。"""

    def wrong_call(self, s, outcome=None, corrected=None):
        """いまの予定の番号へかけ、番号違いだった（検証は、番号の確かめを記録してから済ませる）。締めた結果を返す。"""
        aid = self.call(s, report=rep(("wrong_party",)), verify=None)
        if outcome is not None:
            self.tx(s, lambda tx: number_recheck.record(tx, aid, outcome, corrected=corrected,
                                                        evidence="試験：別の耳の文字と付け合わせた", checked_by="試験"))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, "done", summary=CLEAN))
        return aid, self.settle(s, aid)

    def test_verification_needs_the_recheck_first(self):
        s = self.ready_jp()
        self.redirect(s)
        aid = self.call(s, report=rep(("wrong_party",)), verify=None)
        with self.assertRaises(attempts.RecheckMissing):
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, "done", summary=CLEAN))
        with self.assertRaises(attempts.RecheckMissing):
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, "failed", summary=None))

    def test_as_said_calls_the_first_party_to_ask_again(self):
        s = self.ready_jp()
        said, first, _ = self.redirect(s)
        alt = normalize(said).e164
        _, got = self.wrong_call(s, "as_said")
        self.assertEqual(got["state"], "not_reached")
        self.assertEqual(self.tx(s, lambda tx: dnc.lookup(tx, alt))["reason"], "wrong_party")   # 違った番号だけ止める
        self.assertIsNone(self.tx(s, lambda tx: dnc.lookup(tx, s["phone"])))
        nxt = self.current(s)
        self.assertEqual((nxt["contact_id"], nxt["purpose"], nxt["promised"]), (s["contact"], "ask_number_again", True))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual((c.phone_e164, c.purpose, c.wrong_phone_e164), (s["phone"], "ask_number_again", alt))
        row = self.tx(s, lambda tx: tx.one("SELECT purpose, wrong_contact_id FROM salescall.attempts WHERE attempt_id = %s",
                                           (c.attempt_id,)))
        self.assertEqual(row["purpose"], "ask_number_again")
        with self.assertRaises(Rejected) as e:   # 発信の目的は、あとから変えられない
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET purpose = 'normal', wrong_contact_id = NULL"
                                             " WHERE attempt_id = %s", (c.attempt_id,)))
        self.assertEqual(e.exception.constraint, "attempts_purpose_fixed")

    def test_misheard_calls_the_corrected_number(self):
        s = self.ready_jp()
        said, first, _ = self.redirect(s)
        right = fictional_jp_fixed()
        self.wrong_call(s, "misheard", right)
        nums = {n["phone_e164"]: n for n in self.numbers(s)}
        self.assertEqual(nums[normalize(right).e164]["source_attempt_id"], first)   # 正しい番号も、聞いた通話の番号として残す
        nxt = self.current(s)
        self.assertEqual((nxt["contact_id"], nxt["purpose"]), (nums[normalize(right).e164]["contact_id"], "normal"))
        self.assertEqual(self.claim(s).phone_e164, normalize(right).e164)

    def test_second_wrong_number_after_asking_again_ends_the_row(self):
        s = self.ready_jp()
        self.redirect(s)
        self.wrong_call(s, "as_said")                                       # 1回目：聞き直しへ
        said2, _, _ = self.redirect(s)                                      # 聞き直しで、別の番号を言われた
        _, got = self.wrong_call(s, "as_said")                              # それも番号違い
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "alternate_wrong_again"))

    def test_recheck_is_recorded_only_for_a_wrong_number_the_callee_gave(self):
        s = self.ready_jp()
        listed = self.call(s, report=rep(("wrong_party",)), verify=None)    # 一覧の番号の番号違い：確かめは要らない
        with self.assertRaises(number_recheck.RecheckInvalid):
            self.tx(s, lambda tx: number_recheck.record(tx, listed, "as_said", corrected=None, evidence="x", checked_by="x"))
        s2 = self.ready_jp()
        said, _, _ = self.redirect(s2)
        aid = self.call(s2, report=rep(("wrong_party",)), verify=None)
        for outcome, corrected, evidence in [("misheard", None, "x"), ("misheard", said, "x"), ("as_said", said, "x"),
                                             ("as_said", None, " "), ("guess", None, "x")]:
            with self.subTest(outcome=outcome, corrected=corrected), self.assertRaises(number_recheck.RecheckInvalid):
                self.tx(s2, lambda tx: number_recheck.record(tx, aid, outcome, corrected=corrected, evidence=evidence,
                                                             checked_by="試験"))
        self.tx(s2, lambda tx: number_recheck.record(tx, aid, "cannot_check", corrected=None, evidence="録音が無い",
                                                     checked_by="試験"))
        with self.assertRaises(Rejected):                                    # 1つの通話に1回だけ
            self.tx(s2, lambda tx: number_recheck.record(tx, aid, "as_said", corrected=None, evidence="x", checked_by="x"))


    def test_unanswered_ask_again_moves_to_other_bands_up_to_the_limit(self):
        """聞き直しの電話に出なかった ⇒ 3回まで、毎回、前と別の時間帯に（2026-10-15 ユーザー）。"""
        s = self.ready_jp()
        self.redirect(s)
        self.wrong_call(s, "as_said")
        boundaries = [12, 15]
        for n in (1, 2, 3):
            aid = self.call(s, NO_ANSWER)
            got = self.settle(s, aid)
            if n < 3:
                nxt = self.current(s)
                self.assertEqual((nxt["kind"], nxt["purpose"], nxt["promised"]), ("call", "ask_number_again", True))
                bands = self.tx(s, lambda tx: tx.one(
                    "SELECT salescall.jst(%s) AS next, salescall.jst(claimed_at) AS tried FROM salescall.attempts"
                    " WHERE attempt_id = %s", (nxt["call_at"], aid)))
                self.assertNotEqual(calling_hours.band_of(bands["next"], boundaries),
                                    calling_hours.band_of(bands["tried"], boundaries))   # 前と別の時間帯
                self.tx(s, lambda tx: next_action.schedule_call(          # 試験：その時刻が来たことにする
                    tx, s["task"], self.now(s), contact_id=nxt["contact_id"], cause="試験", promised=True,
                    purpose="ask_number_again", wrong_contact_id=nxt["wrong_contact_id"]))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "ask_number_again_unanswered"))

    def test_ask_again_that_never_connects_is_bounded_and_spaced(self):
        """回線の側で繋がらない聞き直しの電話も、回数に数える（数えないと上限なくかけ続けた＝2026-10-16 の見直し）。
        かけ直しは、前の発信が終わってから最小の間隔（5分）を空ける（約束したかけ直しは門の間隔の対象外のため）。"""
        s = self.ready_jp()
        self.redirect(s)
        self.wrong_call(s, "as_said")
        for n in (1, 2, 3):
            aid = self.call(s, ["dialing", "call_failed"])
            got = self.settle(s, aid)
            if n < 3:
                nxt = self.current(s)
                self.assertEqual((nxt["kind"], nxt["purpose"]), ("call", "ask_number_again"))
                gap = self.tx(s, lambda tx: tx.one(
                    "SELECT %s - ended_at AS g FROM salescall.attempts WHERE attempt_id = %s", (nxt["call_at"], aid)))["g"]
                self.assertGreaterEqual(gap, timedelta(minutes=5))
                self.tx(s, lambda tx: next_action.schedule_call(          # 試験：その時刻が来たことにする
                    tx, s["task"], self.now(s), contact_id=nxt["contact_id"], cause="試験", promised=True,
                    purpose="ask_number_again", wrong_contact_id=nxt["wrong_contact_id"]))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "ask_number_again_unanswered"))

    def test_database_promises(self):
        s = self.ready_jp()
        said, _, _ = self.redirect(s)
        alt = self.numbers(s)[1]["contact_id"]
        with self.assertRaises(Rejected) as e:          # 聞き直しの予定は、違った番号を必ず持つ
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, cause, promised,"
                " contact_id, purpose) VALUES (%s, %s, %s, 99, 'call', now(), '試験', true, %s, 'ask_number_again')",
                (s["tenant"], s["client"], s["task"], s["contact"])))
        self.assertEqual(e.exception.constraint, "task_next_actions_wrong_number")
        with self.assertRaises(Rejected) as e:          # 聞き直しの発信も、違った番号を必ず持つ
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.attempts (tenant_id, client_id, task_id, contact_id, phone_e164, line_id, version_id,"
                " state, claimed_at, claim_expires_at, purpose) VALUES (%s, %s, %s, %s, %s, %s, %s, 'claimed', now(),"
                " now() + interval '1 minute', 'ask_number_again')",
                (s["tenant"], s["client"], s["task"], s["contact"], s["phone"], s["line"], s["version"])))
        self.assertEqual(e.exception.constraint, "attempts_wrong_number")
        aid, _ = self.wrong_call(s, "as_said")
        own = owner_access()
        for sql in ("UPDATE salescall.number_rechecks SET outcome = 'cannot_check' WHERE attempt_id = %s",
                    "DELETE FROM salescall.number_rechecks WHERE attempt_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):     # 番号の確かめは変えられない
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql, (aid,)))


if __name__ == "__main__":
    unittest.main()
