"""次の動き（Q4。DESIGN.md 7-2・4-1）。持ち主 CL-2。

- 次の予定の正本は「次の動きの記録」の最新だけ（「かけ直す（日時）」か「終える（理由）」）。
- 次の動きの記録を足すのは、この部品だけ（I-7・I-12。設計書の `scheduled_at`＝次の予定を書くのは、ここだけ。app/checks が見張る）。
- **何を決めるかは `decide_after_call`・`decide_after_denial` の1か所**（データベースを使わない部品。同じ入力からは同じ答え）。
  通話のあと（締めの処理 core/settle.py が判定と一緒に呼ぶ `after_call`）と、門で止められたあと（`after_denial`）の両方が使う。
  - 回数と間隔は門の数え方（`gate.pacing`）を読む（数え直さない）。再発信の方針は業務の決まり（core/business_rules.py）。
  - 決める予定は、門の最も早い時刻より前にしない（門で止まるだけの予定を作らない）。
  - 門で止められた理由が「前の発信の締めがまだ」「予定の時刻より前」なら、何も書かない（締めより先に予定を書き直さない）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from dataclasses import dataclass
from datetime import timedelta

from salescall.core import attempts, calling_hours, gate, settings
from salescall.core.access import Tx
from salescall.core.business_rules import Rules
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.judge import Judgement
from salescall.core.tasks import TaskNotFound, lock


def current(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """いまの予定（いちばん新しい記録）。無ければ None（まだ何も決めていない）。"""
    return tx.one(
        "SELECT task_id, seq, kind, call_at, end_reason, cause, promised, decided_at FROM salescall.task_next_actions"
        " WHERE task_id = %s ORDER BY seq DESC LIMIT 1",
        (task_id,),
    )


def _append(tx: Tx, task_id: UUID, *, call_at: datetime | None, end_reason: str | None, cause: str,
            call_now: bool = False, promised: bool | None = None) -> int:
    calls = call_at is not None or call_now
    if calls == (end_reason is not None) or (call_now and call_at is not None):
        raise ValueError("「かけ直す（日時）」か「終える（理由）」の、どちらか1つだけです")
    if calls != (promised is not None):
        raise ValueError("かけ直すときは、相手と約束したかけ直しか（promised）を必ず決めます（終えるときは持たない）")
    lock(tx, task_id)  # 同じタスクへの追記を1つずつ順にする（tasks.py の説明）
    row = tx.one(
        "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, end_reason, cause,"
        "                                         promised)"
        " SELECT t.tenant_id, t.client_id, t.task_id,"
        "        coalesce((SELECT max(n.seq) FROM salescall.task_next_actions n WHERE n.task_id = t.task_id), 0) + 1,"
        "        %s, CASE WHEN %s THEN now() ELSE %s::timestamptz END, %s, %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING seq",
        ("call" if calls else "end", call_now, call_at, end_reason, cause, promised, task_id),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row["seq"]


def schedule_call(tx: Tx, task_id: UUID, call_at: datetime, *, cause: str, promised: bool) -> int:
    """かけ直す（日時）を記録する。promised＝相手と約束したかけ直し（相手に頼まれた・こちらが約束した）か。
    約束したかけ直しは、門の「同じ相手への最小の間隔・1日の回数」の対象外（2026-10-14 ユーザー決定）。"""
    return _append(tx, task_id, call_at=call_at, end_reason=None, cause=cause, promised=promised)


def schedule_now(tx: Tx, task_id: UUID, *, cause: str) -> int:
    """いまから、かけてよい（日時はデータベースの時刻。取込で登録したタスクの最初の予定）。
    実際にかけるか・いつかけるかは門が決める（時間帯・間隔・停止など）。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=False)


def end(tx: Tx, task_id: UUID, reason: str, *, cause: str) -> int:
    """終える（理由）を記録する。同じ取引で、その行のまだ発信していない予約を取り消す（門の判定に効く変更。5-2-1 の2）。"""
    seq = _append(tx, task_id, call_at=None, end_reason=reason, cause=cause)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    return seq


# ── 何を決めるか（1か所。DESIGN.md 7-2） ──
END_STATES = frozenset({"completed", "declined", "do_not_call", "out_of_scope", "concluded"})   # 目的を果たした・終わった


@dataclass(frozen=True)
class Decision:
    kind: str                    # call / end
    at: datetime | None          # call のとき
    promised: bool | None        # call のとき：相手と約束したかけ直しか
    end_reason: str | None       # end のとき
    rule: str                    # どの決まりで決めたか（記録の cause に残す）


def _call(at: datetime, promised: bool, rule: str) -> Decision:
    return Decision("call", at, promised, None, rule)


def _end(reason: str, rule: str) -> Decision:
    return Decision("end", None, None, reason, rule)


def decide_after_call(j: Judgement, *, verification: str, normal: gate.Pacing, promised: gate.Pacing, rules: Rules,
                      times_with_key: int, now: datetime) -> Decision:
    """通話のあとの次の動き。normal / promised＝ふつうの予定・約束したかけ直しにした場合の門の数え方。
    times_with_key＝この行で、今回の結果と同じ再発信の方針の鍵の結果が出た回数（今回を含む）。"""
    if j.state in END_STATES:
        return _end(j.state, "目的を果たした・終わった結果")
    if verification == "failed":
        return _end("verification_failed", "通話後の検証で、拒否の聞き逃しを確かめられなかった（かけない側に倒す）")
    if j.complaint:
        return _end("complaint", "苦情があった")
    if j.state == "unknown":
        return _end("judge_unknown", "判定できなかった（入力の誤り。監視へ知らせる）")
    key = j.detail.get("policy_key")
    if j.state == "callback_scheduled":
        cb = j.detail["callback"]
        if cb["at"] is not None:
            at = datetime.fromisoformat(cb["at"])
        else:
            p = rules.retry[key]
            if p.action == "end" or times_with_key > p.max_times:
                return _end(key, "かけ直しの時刻が決まっていない約束：再発信の方針で終える")
            at = now + timedelta(seconds=p.interval_seconds)
        return _call(max(at, promised.earliest or now), True, "相手と約束したかけ直し")
    if normal.remaining <= 0:
        return _end("max_attempts", "この行にかけてよい回数を使い切った")
    if key is None:
        return _end("judge_unknown", "再発信の方針の鍵が無い（入力の誤り）")
    p = rules.retry[key]
    if p.action == "end":
        return _end(key, f"再発信の方針：{key} は終える")
    if times_with_key > p.max_times:
        return _end(f"policy_max:{key}", f"再発信の方針：{key} は {p.max_times} 回まで")
    return _call(max(now + timedelta(seconds=p.interval_seconds), normal.earliest or now), False,
                 f"再発信の方針：{key}")


def decide_after_denial(d: gate.Denied, *, current_promised: bool, pacing: gate.Pacing, now: datetime,
                        opening: datetime | None, retry_wait: int) -> Decision | None:
    """門で止められたあとの次の動き。何も書かないときは None。"""
    if d.reason in ("awaiting_decision", "not_due"):
        return None                                  # 締めの処理・いまの予定に任せる（先に書き直さない）
    if d.kind == "permanent":
        return _end(d.reason, f"門：待っても変わらない理由（{d.reason}）")
    if d.reason == "outside_hours":
        if opening is None:
            return _end("no_calling_hours", "これから先、かけてよい時間帯が無い")
        return _call(max(opening, pacing.earliest or opening), current_promised, "門：時間帯の外 ⇒ 次の時間帯の始まり")
    return _call(max(now + timedelta(seconds=retry_wait), pacing.earliest or now), current_promised,
                 f"門：待てば変わる理由（{d.reason}）")


def _write(tx: Tx, task_id: UUID, d: Decision) -> int:
    if d.kind == "end":
        return end(tx, task_id, d.end_reason, cause=d.rule)
    return schedule_call(tx, task_id, d.at, cause=d.rule, promised=d.promised)


def after_call(tx: Tx, task_id: UUID, j: Judgement, *, verification: str, rules: Rules) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。判定の結果から次の動きを決めて記録する（同じ取引）。"""
    now = tx.one("SELECT now() AS now")["now"]
    key = j.detail.get("policy_key")
    times = 0 if key is None else tx.one(
        "SELECT count(*) AS n FROM salescall.task_results WHERE task_id = %s AND kind = 'judged'"
        " AND detail ->> 'policy_key' = %s", (task_id, key))["n"]
    d = decide_after_call(j, verification=verification, normal=gate.pacing(tx, task_id, promised=False),
                          promised=gate.pacing(tx, task_id, promised=True), rules=rules, times_with_key=times, now=now)
    _write(tx, task_id, d)
    return d


def after_denial(tx: Tx, task_id: UUID, denied: gate.Denied, *, tenant_id: UUID, client_id: UUID,
                 business_id: UUID) -> Decision | None:
    """門で止められたあとに呼ぶ（候補を出す処理が、門の答えを受けて呼ぶ）。決めた予定を記録する。"""
    now = tx.one("SELECT now() AS now")["now"]
    cur = current(tx, task_id)
    opening = None
    if denied.reason == "outside_hours":
        lifetime = timedelta(seconds=int(settings.resolve(tx, "gate.claim_lifetime", tenant_id=tenant_id,
                                                          client_id=client_id, business_id=business_id)))
        hours = settings.resolve(tx, "calling.hours", tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    retry_wait = int(settings.resolve(tx, "gate.denied_retry_wait", tenant_id=tenant_id, client_id=client_id,
                                      business_id=business_id))
    if denied.reason == "outside_hours":
        local = tx.one("SELECT salescall.jst(now()) AS t")["t"]
        try:
            found = calling_hours.next_opening(hours, holiday_checker(tx), local, lifetime)
        except CalendarOutOfRange:   # 次の時間帯を探す途中に暦の無い日がある ⇒ 決めつけず、待ってからもう一度
            found = None
            denied = gate.Denied("calendar_out_of_range", *gate.REASONS["calendar_out_of_range"])
        opening = None if found is None else tx.one("SELECT salescall.from_jst(%s) AS t", (found,))["t"]
    d = decide_after_denial(denied, current_promised=bool(cur and cur["promised"]), pacing=gate.pacing(tx, task_id),
                            now=now, opening=opening, retry_wait=retry_wait)
    if d is not None:
        _write(tx, task_id, d)
    return d
