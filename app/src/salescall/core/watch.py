"""見守り：エラーを記録して止めるかを決める・停止を解く確かめを記録して解くかを決める（DESIGN.md 9章・9-1）。持ち主 CL-2。

- エラーの種類と処置は core/errors.py の表だけを読む（ここで2つ目の判断を作らない）。止める・解くの記録は core/stops.py。
- **止めるか**（`decide_stop`）：処置の表の「止める原因・範囲・何回続いたら」と、「何回続いたか」（データベースの1か所
  `salescall.consecutive_errors`。種類ごとの「直った証拠」（core/errors.py の EVIDENCE）より後：回線＝その回線でお客様を
  呼び出せた発信／会話AI＝どこかの通話が会話に入った／別の耳＝どこかの通話の検証が済んだ。どれも、同じ原因・同じ範囲の停止が
  解かれた時刻より後＝管理者か契約者が再開したら・確かめで解けたら数え直す）。
  同じ原因・同じ範囲で、まだ解かれていない停止があれば、重ねて止めない。
- **解くか**（`record_check`）：システムが止めた停止のうち、確かめで解ける原因（保存・回線・会話AI）だけ。確かめの結果を記録し、
  決めた回数続けて成功したら、同じ取引で解く。**次に確かめてよい時刻より前の確かめは受け付けない**（ばたつきで伸びた待ちを、
  確かめを早めて越えられない）。待ち＝確かめの間隔 ×（ばたつきの倍率 ^ 期間内に同じ原因・同じ範囲で止まった回数）、上限つき。
- 確かめそのもの（試しの書き込み・回線の応答・試しのセッション）は、それぞれの持ち主の部品が行い、結果だけをここへ渡す。
- 管理者か契約者が押した緊急停止（manual）と、確かめでは解けない原因（言ってはいけないこと・品質の基準・費用）は、ここでは解かない
  （品質は新しい版の合格で、費用は期間が替わったとき。それぞれの持ち主の処理が解く）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from salescall.core import errors, next_action, notify, settings, stops
from salescall.core.access import Access, Actor, Tx

# 確かめで解ける原因 → （確かめの間隔, 何回続けて成功したら解くか）の設定の名前
PROBE_SETTINGS: dict[str, tuple[str, str]] = {
    "storage_failure": ("stop.storage.probe_interval", "stop.storage.resume_after_successes"),
    "line_failure": ("stop.line.probe_interval", "stop.line.resume_after_successes"),
    "ai_connection": ("stop.ai.probe_interval", "stop.ai.resume_after_successes"),
}


class NotDue(Exception):
    """次に確かめてよい時刻より前。"""


class NotProbeable(Exception):
    """確かめで解ける停止ではない（管理者か契約者が押した緊急停止・ほかの原因）。"""


def record_error(tx: Tx, report: errors.ErrorReport, *, line_id: UUID | None = None,
                 attempt_id: UUID | None = None) -> str:
    """起きたエラーを分類して記録する（取引の契約者・依頼者で）。種類を返す。どの種類も管理者に知らせ、処置の表が「知らせる」
    なら契約者にも知らせる（同じ種類は、同じ範囲で1時間に1回）。止めるかは `decide_stop`（範囲に合った取引で呼ぶ）。"""
    kind = errors.classify(report)
    row = tx.one(
        "INSERT INTO salescall.error_events (tenant_id, client_id, line_id, attempt_id, source, kind, code, message)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s)"
        " RETURNING error_id, to_char(salescall.jst(occurred_at), 'YYYY-MM-DD\"T\"HH24') AS hour,"
        "           salescall.cur_tenant() AS tenant, salescall.cur_client() AS client",
        (line_id, attempt_id, report.source, kind, report.code, report.message))
    action = errors.ACTIONS[kind]
    # エラーの一覧と処置はプログラム（直せるのは管理者だけ）なので、どの種類も管理者に知らせる。根源的なものは「直しの依頼」。
    # 契約者には、処置の表が「知らせる」とした種類だけ
    notify.emit(tx, "error_notice", key=f"error:{kind}:{row['tenant']}:{row['client']}:{row['hour']}",
                summary=f"{'直しの依頼' if action.fundamental else 'エラー'}：{action.meaning}",
                refs={"error_id": row["error_id"], "kind": kind, "fundamental": action.fundamental},
                admin=True, contractor=action.notify)
    if action.hold_row and attempt_id is not None:
        # その行だけ判定待ちにする（かけ直さない）。ほかの行は続ける（2026-10-16 ユーザー）
        task = tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
        if task is not None:
            next_action.hold(tx, task["task_id"], cause=f"エラー：{kind}（管理者に直しを依頼）")
    return kind


def decide_stop(tx: Tx, kind: str, *, line_id: UUID | None = None) -> dict[str, Any] | None:
    """処置の表のとおり、止めるなら止める（範囲に合った取引で呼ぶ：全体＝全体の取引／回線＝その回線の契約者の取引）。
    止めた停止の記録を返す（止めなければ None）。"""
    action = errors.ACTIONS[kind]
    if action.stop_cause is None:
        return None
    if action.stop_scope == "line" and line_id is None:
        raise ValueError("回線を止める種類なのに、回線がありません")
    line = line_id if action.stop_scope == "line" else None
    open_same = tx.one(
        "SELECT event_id FROM salescall.open_stop_events s WHERE s.cause = %s AND s.scope = %s"
        " AND s.line_id IS NOT DISTINCT FROM %s LIMIT 1", (action.stop_cause, action.stop_scope, line))
    if open_same is not None:
        return None                                  # もう止まっている（重ねて止めない）
    n = tx.one("SELECT salescall.consecutive_errors(%s, %s, %s, %s) AS n",
               (list(errors.counted_with(kind)), action.stop_cause, errors.EVIDENCE[kind], line))["n"]
    if n is None:
        raise PermissionError("この取引では、その範囲のエラーを数えられません（全体＝全体の取引／回線＝その回線の契約者の取引）")
    need = 1 if action.stop_after is None else int(settings.system_value(tx, action.stop_after))
    if n < need:
        return None
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    how = ("根源的なもの：管理者に直しを依頼しました。直るまで新しい発信を待ちます" if action.fundamental
           else "一時的なもの：直ったら自動で続けます")
    return stops.stop(tx, scope=action.stop_scope, cause=action.stop_cause,
                      reason=f"{action.meaning}（続けて {n} 回。基準 {need} 回。{how}）", evidence=f"エラーの種類：{kind}",
                      tenant_id=tenant if action.stop_scope == "line" else None, line_id=line_id)


def after_error(access: Access, actor: Actor, report: errors.ErrorReport, *, tenant_id: UUID | None = None,
                client_id: UUID | None = None, line_id: UUID | None = None, attempt_id: UUID | None = None) -> str:
    """エラーが起きたときに呼ぶ入口（1か所）：起きた取引の範囲で記録し、止める範囲の取引で止めるかを決める。種類を返す。"""
    def run(work):
        if client_id is not None:
            return access.in_client(actor, tenant_id, client_id, work)
        if tenant_id is not None:
            return access.in_tenant(actor, tenant_id, work)
        return access.in_system(actor, work)
    kind = run(lambda tx: record_error(tx, report, line_id=line_id, attempt_id=attempt_id))
    action = errors.ACTIONS[kind]
    if action.stop_scope == "system":
        access.in_system(actor, lambda tx: decide_stop(tx, kind))
    elif action.stop_scope == "line":
        access.in_tenant(actor, tenant_id, lambda tx: decide_stop(tx, kind, line_id=line_id))
    return kind


def _flapping_wait(tx: Tx, stop: dict[str, Any], interval: int) -> timedelta:
    window = int(settings.system_value(tx, "stop.flapping.window"))
    times = tx.one(
        "SELECT count(*) AS n FROM salescall.stop_events s WHERE s.action = 'stop' AND s.cause = %s AND s.scope = %s"
        " AND s.line_id IS NOT DISTINCT FROM %s AND s.event_id <> %s AND s.at > %s - %s * interval '1 second'",
        (stop["cause"], stop["scope"], stop["line_id"], stop["event_id"], stop["at"], window))["n"]
    if times == 0:
        return timedelta(seconds=interval)
    wait = interval * int(settings.system_value(tx, "stop.flapping.wait_multiplier")) ** times
    return timedelta(seconds=min(wait, int(settings.system_value(tx, "stop.flapping.max_wait"))))


def record_check(tx: Tx, stop_event_id: int, ok: bool, *, evidence: str) -> dict[str, Any]:
    """停止を解く確かめの結果を記録し、決めた回数続けて成功したら解く（同じ取引）。記録を返す（resumed＝解いたか）。"""
    s = tx.one(
        "SELECT event_id, scope, cause::text AS cause, line_id, actor_kind::text AS actor_kind, at"
        " FROM salescall.open_stop_events s WHERE s.event_id = %s", (stop_event_id,))
    if s is None:
        raise LookupError(f"まだ解かれていない停止ではありません（または、取引の範囲の外）：{stop_event_id}")
    if s["actor_kind"] != "system" or s["cause"] not in PROBE_SETTINGS:
        raise NotProbeable(f"確かめで解ける停止ではありません（原因 {s['cause']}・止めたのは {s['actor_kind']}）")
    now = tx.one("SELECT now() AS t")["t"]
    prev = tx.one("SELECT ok, streak, next_check_at FROM salescall.stop_checks WHERE stop_event_id = %s"
                  " ORDER BY check_id DESC LIMIT 1", (stop_event_id,))
    if prev is not None and now < prev["next_check_at"]:
        raise NotDue(f"次に確かめてよいのは {prev['next_check_at']} から")
    interval_key, need_key = PROBE_SETTINGS[s["cause"]]
    interval, need = int(settings.system_value(tx, interval_key)), int(settings.system_value(tx, need_key))
    streak = ((prev["streak"] if prev is not None else 0) + 1) if ok else 0
    next_at = now + _flapping_wait(tx, s, interval)
    tx.execute("INSERT INTO salescall.stop_checks (stop_event_id, ok, streak, checked_at, next_check_at, evidence)"
               " VALUES (%s, %s, %s, %s, %s, %s)", (stop_event_id, ok, streak, now, next_at, evidence))
    resumed = None
    if ok and streak >= need:
        resumed = stops.resume(tx, stop_event_id, reason=f"確かめが続けて {streak} 回成功（基準 {need} 回）", evidence=evidence)
    return {"ok": ok, "streak": streak, "need": need, "next_check_at": next_at, "resumed": resumed}


def due_checks(tx: Tx) -> list[dict[str, Any]]:
    """いま確かめてよい停止（システムが止めた、確かめで解ける原因の、まだ解かれていない停止のうち、この取引が確かめを書ける
    もの：全体の取引＝全体の停止／契約者の取引＝その契約者の停止）。"""
    return tx.all(
        "SELECT s.event_id, s.scope, s.cause::text AS cause, s.line_id FROM salescall.open_stop_events s"
        " WHERE s.actor_kind = 'system' AND s.cause::text = ANY(%s)"
        " AND (s.scope = 'system') = (salescall.cur_tenant() IS NULL)"   # 確かめを書ける停止だけ（0029 の (3)）
        " AND coalesce((SELECT c.next_check_at FROM salescall.stop_checks c WHERE c.stop_event_id = s.event_id"
        "               ORDER BY c.check_id DESC LIMIT 1), '-infinity') <= now()"
        " ORDER BY s.event_id", (list(PROBE_SETTINGS),))
