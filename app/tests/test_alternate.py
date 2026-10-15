"""相手が言った別の番号へのかけ直しの試験（DESIGN.md 3-5・3-6・5-2-2・7-2-1）。

- データベースの約束：行の番号の表（一覧の番号は自動で入る・変えられない）／発信と予定は、その行の番号にしかかけられない／
  1つの行に進行中の発信は1つだけ。
- 流れ：締め（行に番号を足す・かけるかを決める）→ 次の予定（番号つき）→ 門（予定の番号へかける）。
"""

from __future__ import annotations

import time
import unittest
import uuid
from datetime import timedelta

from salescall.core import dnc, gate, next_action, results, tasks
from salescall.core.access import Rejected
from salescall.core.call_report import Callback
from salescall.core.phone import normalize
from tests.support import OPERATOR, SYSTEM, fictional_jp_fixed, owner_access
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
        stranger = self.w.insert_claim(t2)             # ほかの依頼主の行の試行
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
        self.assertEqual(detail["callback"]["number_said"], said)          # 依頼主へは、相手が言ったままの番号も渡る

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
    """同じ相手＝かける番号が同じ発信、または同じ行の発信（番号を変えて回数・間隔をすり抜けない）。"""

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


if __name__ == "__main__":
    unittest.main()
