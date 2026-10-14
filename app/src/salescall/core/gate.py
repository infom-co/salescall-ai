"""発信の門（Q2「今この相手にかけてよいか」）。持ち主 CL-2。DESIGN.md 5-2・5-2-1。

- **判定と予約を1つの取引で行う**（直列化の水準。読み書きの層が始め、ぶつかったら最初からやり直す）。すべて通ったときだけ
  試行（予約 `claimed`）を作り、予約証を返す。**1つでも判断できなければ通さない**（設定が無い・暦の範囲の外・材料が無い）。
- 予約を作るのは、この部品の `claim` だけ（I-12。app/checks が見張る）。発信へ進めるのは出口（core/dial_exit.py）だけ。
- 時計はデータベースの時刻1つ（5-2-1 の1）。日本時間への直し方も、データベースの関数1つ（`salescall.jst`）。時間帯の決まりは core/calling_hours.py、データで決まる「かけない理由」は
  core/target_policy.py、止まっているかは停止の記録、段は core/quality_stage.py を読む（ここで2つ目の判断を作らない）。
- 回数と間隔の数え方は `pacing` の1か所（5-2-1 の8）。門も、次の動き（Q4）も、画面も、これを読む。
- 不許可は、理由の名前（下の REASONS が一覧の正本）を返し、記録に残す（画面で理由ごとに数える）。理由の種類：
  permanent＝待っても変わらない（次の動きは終える）／wait＝待てば変わる（次の動きは待ってかけ直す）／
  cannot_judge＝判断の材料が無い（通さない。運営者が直す。暦の取り込み・設定の値・業務の版）。
- 依頼主の範囲の外（ほかの依頼主・契約者の試行）は、中身を返さない確かめの関数だけで見る（「同じ契約者か、外か」だけ）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from salescall.core import calling_hours, quality_stage, settings, stops, target_policy
from salescall.core.access import Rejected, Tx
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.phone import normalize
from salescall.core.tasks import TaskNotFound

# 不許可の理由（名前 → 日本語・種類）。名前の一覧の正本はここだけ（画面・次の動きは、これを読む）。
REASONS: dict[str, tuple[str, str]] = {
    "no_schedule": ("次の予定がありません", "cannot_judge"),
    "task_ended": ("この行は処理済みです", "permanent"),
    "not_due": ("次の予定の時刻より前です", "wait"),
    "awaiting_decision": ("前の発信のあとの、次の予定がまだ決まっていません", "wait"),
    "number_unknown": ("番号が不明です", "permanent"),
    "number_kind_forbidden": ("かけない種類の番号です", "permanent"),
    "number_kind_not_allowed": ("この業務の設定でかけない種類の番号です", "permanent"),
    "dnc_client": ("発信禁止です", "permanent"),
    "dnc_system": ("システム共通の発信禁止です", "permanent"),
    "dnc_client_ref": ("この管理番号の相手は発信禁止です", "permanent"),
    "max_attempts": ("この行に発信してよい回数を使い切りました", "permanent"),
    "no_version": ("実施回に業務設定の版が割り当てられていません", "cannot_judge"),
    "version_not_ready": ("この版は、まだかけられる段に入っていません（模擬通話・最初の段の前）", "wait"),
    "stage_full": ("いまの段の件数に達しました（次の段へ進むのを待っています）", "wait"),
    "stopped": ("止まっています", "wait"),
    "outside_hours": ("かけてよい時間帯の外です", "wait"),
    "calendar_out_of_range": ("祝日の暦が無い日です", "cannot_judge"),
    "verification_pending": ("前の通話の、通話後の検証が済んでいません", "wait"),
    "same_contact_interval": ("同じ相手への前の発信から、間隔が空いていません", "wait"),
    "unknown_result_interval": ("結果が分からない前の発信から、間隔が空いていません", "wait"),
    "daily_attempts": ("同じ相手へ、今日かけてよい回数を使い切りました", "wait"),
    "cross_client_interval_same_tenant": ("ほかの依頼主が同じ番号へかけたばかりです", "wait"),
    "cross_client_interval_other_tenant": ("同じ番号へかけたばかりです", "wait"),   # 契約者の外は、中身を出さない
    "line_unknown": ("その回線はありません", "cannot_judge"),
    "line_not_leased": ("回線を借りていません（または、借用の期限が予約証より先に切れます）", "wait"),
    "line_daily_limit": ("この回線の、今日の発信の上限に達しました", "wait"),
    "line_busy": ("回線が使用中です", "wait"),
    "client_concurrency": ("この依頼主の同時の本数の上限です", "wait"),
    "ai_capacity": ("会話AIの枠がいっぱいです", "wait"),
    "contact_active": ("同じ相手に、進行中の発信があります", "wait"),
    "number_active_same_client": ("同じ番号に、進行中の発信があります", "wait"),
    "number_active_same_tenant": ("同じ番号に、ほかの依頼主の進行中の発信があります", "wait"),
    "number_active_other_tenant": ("同じ番号に、進行中の発信があります", "wait"),   # 契約者の外は、中身を出さない
    "setting_missing": ("設定の値が決まっていません", "cannot_judge"),
}

_DIALED = "state NOT IN ('claimed', 'released')"   # 発信まで進んだ試行（回数・間隔に数えるのは、これだけ。5-2-1 の8）
# 同じ相手への最小の間隔・1日の回数に数える発信：相手の意思と関係なく繋がらなかった・切れたものは数えない（2026-10-14 ユーザー決定。
# 決まりはデータベースの1か所 salescall.not_by_callee）
_COUNTED = _DIALED + " AND NOT salescall.not_by_callee(state, hangup_side, attempt_id)"


@dataclass(frozen=True)
class Claim:
    """予約証。期限までに出口（core/dial_exit.py）で発信へ進めなければ、門が次の取引で取り消す。"""
    attempt_id: UUID
    task_id: UUID
    line_id: UUID
    phone_e164: str
    version_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class Denied:
    reason: str
    message: str
    kind: str          # permanent / wait / cannot_judge


@dataclass(frozen=True)
class Pacing:
    """回数と間隔（5-2-1 の8）。earliest が None ＝ 前の通話の検証待ち（いつかけられるか、まだ決まらない）。"""
    remaining: int
    earliest: datetime | None
    binding: str | None    # earliest を決めた理由の名前（今すぐかけられるなら None）
    promised: bool         # いまの予定が、相手と約束したかけ直しか（回数の上限・最小の間隔・1日の回数の対象外）


def _deny(reason: str) -> Denied:
    message, kind = REASONS[reason]
    return Denied(reason, message, kind)


def _task(tx: Tx, task_id: UUID) -> dict[str, Any]:
    row = tx.one(
        "SELECT t.task_id, t.tenant_id, t.client_id, t.run_id, t.contact_id, t.client_ref, c.phone_e164, r.business_id"
        " FROM salescall.tasks t"
        " JOIN salescall.contacts c ON c.client_id = t.client_id AND c.contact_id = t.contact_id"
        " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
        " WHERE t.task_id = %s", (task_id,))
    if row is None:
        raise TaskNotFound(str(task_id))
    return row


def _setting(tx: Tx, key: str, task: dict[str, Any]) -> Any:
    return settings.resolve(tx, key, tenant_id=task["tenant_id"], client_id=task["client_id"],
                            business_id=task["business_id"])


def _pacing(tx: Tx, task: dict[str, Any], now: datetime) -> Pacing:
    # 「かけた回数」の数え方は1つ（行の回数の上限・1日の回数・最小の間隔のどれも）：相手の意思と関係なく繋がらなかった・
    # 切れた発信は数えない。相手と約束したかけ直しは、どの上限でも止めない（2026-10-14 ユーザー：「ほぼ同じことでしょ。統一」）
    counted = tx.one(f"SELECT count(*) AS n FROM salescall.attempts WHERE task_id = %s AND {_COUNTED}",
                     (task["task_id"],))["n"]
    remaining = max(0, int(_setting(tx, "retry.max_attempts_per_task", task)) - counted)
    nxt = tx.one("SELECT promised FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1",
                 (task["task_id"],))
    promised = bool(nxt and nxt["promised"])
    if tx.one("SELECT 1 FROM salescall.attempts WHERE contact_id = %s AND verification = 'pending' LIMIT 1",
              (task["contact_id"],)) is not None:
        return Pacing(remaining, None, "verification_pending", promised)
    limits: list[tuple[datetime, str]] = []
    if not promised:
        last = tx.one(f"SELECT max(ended_at) AS at FROM salescall.attempts WHERE contact_id = %s AND {_COUNTED}",
                      (task["contact_id"],))["at"]
        if last is not None:
            limits.append((last + timedelta(seconds=int(_setting(tx, "gate.same_contact_min_interval", task))),
                           "same_contact_interval"))
        today = tx.one(
            f"SELECT count(*) AS n, salescall.jst_day_start(salescall.jst(now())::date + 1) AS tomorrow"
            f" FROM salescall.attempts WHERE contact_id = %s AND {_COUNTED}"
            f"  AND salescall.jst(claimed_at)::date = salescall.jst(now())::date",
            (task["contact_id"],))
        if today["n"] >= int(_setting(tx, "gate.max_dials_per_contact_per_day", task)):
            limits.append((today["tomorrow"], "daily_attempts"))
    # 結果不明のあとの長い間隔は、相手が出たあと（話していたかもしれない）の結果不明だけ（相手が出る前なら、誰とも話していない）
    unknown = tx.one("SELECT max(ended_at) AS at FROM salescall.attempts WHERE contact_id = %s AND state = 'technical_error'"
                     " AND salescall.reached_answer(attempt_id)", (task["contact_id"],))["at"]
    if unknown is not None:
        limits.append((unknown + timedelta(seconds=int(_setting(tx, "gate.unknown_result_min_interval", task))),
                       "unknown_result_interval"))
    cross = int(_setting(tx, "gate.cross_client_interval", task))
    for r in tx.all("SELECT relation, last_at FROM salescall.number_last_dial_elsewhere(%s)", (task["phone_e164"],)):
        limits.append((r["last_at"] + timedelta(seconds=cross), "cross_client_interval_" + r["relation"]))
    binding = [x for x in limits if x[0] > now]
    if not binding:
        return Pacing(remaining, now, None, promised)
    at, reason = max(binding, key=lambda x: x[0])
    return Pacing(remaining, at, reason, promised)


def pacing(tx: Tx, task_id: UUID) -> Pacing:
    """その行（タスク）の、残りの回数と、次にかけてよい最も早い時刻（門・次の動き・画面が読む1か所）。"""
    task = _task(tx, task_id)
    return _pacing(tx, task, tx.one("SELECT now() AS now")["now"])


def claim(tx: Tx, *, task_id: UUID, line_id: UUID, lease_holder: str) -> Claim | Denied:
    """その行に、その回線で発信してよいかを判定し、よければ予約する（1つの取引）。不許可は記録して返す。"""
    task = _task(tx, task_id)
    tx.one("SELECT salescall.release_expired_claims(%s, %s, %s)", (task["contact_id"], task["phone_e164"], line_id))
    try:
        outcome = _judge_and_claim(tx, task, line_id, lease_holder)
    except settings.SettingMissing:
        outcome = _deny("setting_missing")
    except CalendarOutOfRange:
        outcome = _deny("calendar_out_of_range")
    if isinstance(outcome, Denied):
        tx.execute("INSERT INTO salescall.gate_denials (tenant_id, client_id, task_id, line_id, reason)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s)",
                   (task_id, None if outcome.reason == "line_unknown" else line_id, outcome.reason))
    return outcome


def _judge_and_claim(tx: Tx, task: dict[str, Any], line_id: UUID, lease_holder: str) -> Claim | Denied:
    clock = tx.one("SELECT now() AS now, salescall.jst(now()) AS local")
    now, local = clock["now"], clock["local"]

    # (1) 次の予定（Q4 の記録を読むだけ）
    nxt = tx.one("SELECT kind, call_at, decided_at FROM salescall.task_next_actions WHERE task_id = %s"
                 " ORDER BY seq DESC LIMIT 1", (task["task_id"],))
    if nxt is None:
        return _deny("no_schedule")
    if nxt["kind"] == "end":
        return _deny("task_ended")
    if nxt["call_at"] > now:
        return _deny("not_due")
    # 予定は1回の発信で使い切る：前の発信のあとに決め直された予定でなければ通さない（判定・次の動きが済む前に、
    # 使った予定でもう一度かけない）
    last_dial = tx.one(f"SELECT max(claimed_at) AS at FROM salescall.attempts WHERE task_id = %s AND {_DIALED}",
                       (task["task_id"],))["at"]
    if last_dial is not None and nxt["decided_at"] <= last_dial:
        return _deny("awaiting_decision")

    # (2) データで決まる「かけない理由」（番号の種類・発信禁止の番号と管理番号。取込と同じ1か所）
    blocked = target_policy.block_reason(tx, normalize(task["phone_e164"]), client_ref=task["client_ref"],
                                         tenant_id=task["tenant_id"], client_id=task["client_id"],
                                         business_id=task["business_id"])
    if blocked is not None:
        return _deny(blocked[0])

    # (3) 回数と間隔（pacing の1か所）
    pace = _pacing(tx, task, now)
    if pace.remaining <= 0 and not pace.promised:
        return _deny("max_attempts")

    # (4) 業務設定の版と段
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (task["run_id"],))["v"]
    if version is None:
        return _deny("no_version")
    ok, why = quality_stage.allowance(tx, version)
    if not ok:
        return _deny(why)

    # (5) 止まっているか（全体・契約者・依頼主・業務・回線）
    if stops.open_stops(tx, tenant_id=task["tenant_id"], client_id=task["client_id"],
                        business_id=task["business_id"], line_id=line_id):
        return _deny("stopped")

    # (6) 時間帯：予約から予約証の期限まで、まるごと内側（5-2-1 の4）。祝日の暦の外は判断できない（5-2-1 の5）
    lifetime = timedelta(seconds=int(_setting(tx, "gate.claim_lifetime", task)))
    if not calling_hours.covers(_setting(tx, "calling.hours", task), holiday_checker(tx), local, local + lifetime):
        return _deny("outside_hours")

    # (7) 間隔の待ち
    if pace.earliest is None:
        return _deny("verification_pending")
    if pace.binding is not None:
        return _deny(pace.binding)

    # (8) 回線：借りている本人で、借用の期限が予約証の期限より先。回線ごとの1日の上限（Intent.md 8-6）
    line = tx.one("SELECT lease_holder, lease_expires_at FROM salescall.lines WHERE line_id = %s", (line_id,))
    if line is None:
        return _deny("line_unknown")
    if line["lease_holder"] != lease_holder or line["lease_expires_at"] < now + lifetime:
        return _deny("line_not_leased")
    dials = tx.one("SELECT salescall.line_dials_on(%s, %s) AS n", (line_id, local.date()))["n"]
    if dials >= int(_setting(tx, "line.max_dials_per_day", task)):
        return _deny("line_daily_limit")

    # (9) 同時の本数：依頼主ごとの上限と、全体の会話AIの枠（5-2 の9。全依頼主で共有）
    active = tx.one("SELECT count(*) AS n FROM salescall.attempts WHERE salescall.is_live(state, claim_expires_at)")["n"]
    if active >= int(_setting(tx, "calling.max_concurrent_calls", task)):
        return _deny("client_concurrency")
    capacity = int(Decimal(str(_setting(tx, "gate.ai_tpm_limit", task))) * Decimal(str(_setting(tx, "gate.ai_tpm_safety", task)))
                   // Decimal(str(_setting(tx, "gate.ai_tpm_per_call", task))))
    if tx.one("SELECT salescall.active_attempts_total() AS n")["n"] >= capacity:
        return _deny("ai_capacity")

    # (10) 予約。同じ相手・番号・回線の進行中は、データベースの部分一意が拒む（その部分だけ取り消して理由を返す）
    try:
        with tx.savepoint():
            row = tx.one(
                "INSERT INTO salescall.attempts (tenant_id, client_id, task_id, contact_id, phone_e164, line_id, version_id,"
                "                                state, claimed_at, claim_expires_at)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, 'claimed', now(), now() + %s)"
                " RETURNING attempt_id, claim_expires_at",
                (task["task_id"], task["contact_id"], task["phone_e164"], line_id, version, lifetime))
    except Rejected as e:
        if e.constraint == "attempts_one_active_per_contact":
            return _deny("contact_active")
        if e.constraint == "attempts_one_active_per_line":
            return _deny("line_busy")
        if e.constraint == "attempts_one_active_per_number":
            relation = tx.one("SELECT salescall.number_active_relation(%s) AS r", (task["phone_e164"],))["r"]
            return _deny("number_active_" + (relation or "other_tenant"))
        raise
    return Claim(row["attempt_id"], task["task_id"], line_id, task["phone_e164"], version, row["claim_expires_at"])
