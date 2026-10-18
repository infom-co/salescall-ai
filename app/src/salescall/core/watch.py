"""見守り：エラーを記録して止めるかを決める・停止を解く確かめを記録して解くかを決める（DESIGN.md 9章・9-1）。持ち主 CL-2。

- エラーの種類と処置は core/errors.py の表だけを読む（ここで2つ目の判断を作らない）。止める・解くの記録は core/stops.py。
- **止めるか**（`decide_stop`）：処置の表の「止める原因・範囲・何回続いたら」と、「何回続いたか」（データベースの1か所
  `salescall.consecutive_errors`。種類ごとの「直った証拠」（core/errors.py の EVIDENCE）より後：回線＝その回線でお客様を
  呼び出せた発信／会話AI＝その契約者のどこかの通話が会話に入った。どれも、同じ原因・同じ範囲の停止が
  解かれた時刻より後＝管理者か契約者が再開したら・確かめで解けたら数え直す）。
  同じ原因・同じ範囲で、まだ解かれていない停止があれば、重ねて止めない。
- **止める範囲は、その契約者か、その回線**（全体は止めない）。会話AIの枠は契約者ごと（契約者が自分のキー・プロジェクトを持つ。
  2026-10-16 ユーザー。Intent.md 1-4）なので、会話AIの異常も、その契約者のエラーだけを数えて、その契約者だけを止める
  （2026-10-17 見直し：前は全体で数えて全体を止め、ある契約者のキーの誤りで、ほかの契約者まで止まった）。
- **解くか**（`record_check`）：システムが止めた停止のうち、確かめで解ける原因（回線・会話AI）だけ。確かめの結果を記録し、
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

from salescall.core import errors, fix_requests, notify, settings, stops
from salescall.core.access import Access, Actor, Tx

# 確かめで解ける原因 → （確かめの間隔, 何回続けて成功したら解くか）の設定の名前
PROBE_SETTINGS: dict[str, tuple[str, str]] = {
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
    なら契約者にも知らせる（同じ種類は、同じ範囲で1時間に1回）。止めるかは `decide_stop`（その契約者の取引で呼ぶ）。"""
    kind = errors.classify(report)
    row = tx.one(
        "INSERT INTO salescall.error_events (tenant_id, client_id, line_id, attempt_id, source, kind, code, message)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s)"
        " RETURNING error_id, to_char(salescall.jst(occurred_at), 'YYYY-MM-DD\"T\"HH24') AS hour,"
        "           salescall.cur_tenant() AS tenant, salescall.cur_client() AS client",
        (line_id, attempt_id, report.source, kind, report.code, report.message))
    action = errors.ACTIONS[kind]
    key = f"{kind}:{row['tenant']}:{row['client']}:{row['hour']}"
    refs = {"error_id": row["error_id"], "kind": kind, "attempt_id": attempt_id}
    # 根源的なもの（待っても直らない）は、止めずに直す担当へ修正依頼を出す（2026-10-17 ユーザー：「人はそのプロセスをチェック
    # もしくは承認するだけ」）。管理者には、どの種類も知らせる（確かめ・承認のため）。契約者には、処置の表が「知らせる」とした種類だけ
    fixer = action.fixer or (action.lasting_fixer if action.lasting_fixer and _lasting(tx, kind) else None)
    if fixer is not None:
        fix_requests.request(tx, fixer, kind, key=f"error:{kind}:{row['tenant']}", refs=refs,
                             summary=errors.LABELS[kind] + ("" if action.fixer else "（1時間以上続いている）"))
    notify.emit(tx, "error_notice", key=f"error:{key}",
                summary=(f"{fixer} に修正依頼：" if fixer else "エラー：") + errors.LABELS[kind],
                refs={**refs, "fixer": fixer}, admin=True, contractor=action.notify)
    return kind


def _lasting(tx: Tx, kind: str) -> bool:
    """その契約者で、その種類が errors.LASTING 以上続いているか（その長さより長くあかずに起き続けている）。"""
    return tx.one(
        "WITH e AS (SELECT occurred_at, lag(occurred_at) OVER (ORDER BY occurred_at) AS prev FROM salescall.error_events"
        "           WHERE kind = %s AND tenant_id = salescall.cur_tenant() AND occurred_at > now() - interval '7 days')"
        " SELECT coalesce(now() - max(occurred_at) FILTER (WHERE prev IS NULL OR occurred_at - prev > %s) >= %s, false) AS lasting"
        " FROM e", (kind, errors.LASTING, errors.LASTING))["lasting"]


def decide_stop(tx: Tx, kind: str, *, line_id: UUID | None = None) -> dict[str, Any] | None:
    """処置の表のとおり、止めるなら止める（その契約者の取引で呼ぶ：契約者＝その契約者の会話AIの異常／回線＝その回線）。
    止めた停止の記録を返す（止めなければ None）。根源的なもの（直す担当へ修正依頼を出したもの）は、管理者にも知らせる（確かめ・承認のため）。"""
    action = errors.ACTIONS[kind]
    if action.stop_cause is None:
        return None
    if action.stop_scope == "line" and line_id is None:
        raise ValueError("回線を止める種類なのに、回線がありません")
    line = line_id if action.stop_scope == "line" else None
    open_same = tx.one(
        "SELECT event_id FROM salescall.open_stop_events s WHERE s.cause = %s AND s.scope = %s"
        " AND s.tenant_id = salescall.cur_tenant() AND s.line_id IS NOT DISTINCT FROM %s LIMIT 1",
        (action.stop_cause, action.stop_scope, line))
    if open_same is not None:
        return None                                  # もう止まっている（重ねて止めない）
    n = tx.one("SELECT salescall.consecutive_errors(%s, %s, %s, %s) AS n",
               (list(errors.counted_with(kind)), action.stop_cause, errors.EVIDENCE[kind], line))["n"]
    if n is None:
        raise PermissionError("この取引では、その範囲のエラーを数えられません（契約者の取引で数える。回線は、その回線の契約者の取引）")
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    need = 1 if action.stop_after is None else int(_tenant_setting(tx, action.stop_after, tenant))
    if n < need:
        return None
    how = (f"根源的なもの：{action.fixer} に修正依頼を出しました。直るまで新しい発信を待ちます" if action.fundamental
           else "一時的なもの：直ったら自動で続けます")
    return stops.stop(tx, scope=action.stop_scope, cause=action.stop_cause,
                      reason=f"{action.meaning}（続けて {n} 回。基準 {need} 回。{how}）", evidence=f"エラーの種類：{kind}",
                      tenant_id=tenant, line_id=line, admin=action.fundamental)


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
    if errors.ACTIONS[kind].stop_scope is not None:
        if tenant_id is None:      # 止める種類（会話AI・回線）は、契約者のキー・回線で起きる。どの契約者か分からなければ数えられない
            raise ValueError(f"止める種類のエラー（{kind}）なのに、契約者がありません")
        access.in_tenant(actor, tenant_id, lambda tx: decide_stop(tx, kind, line_id=line_id))
    return kind


def _tenant_setting(tx: Tx, key: str, tenant_id: UUID | None) -> Any:
    """止める・解く決まりの値（契約者ごと＝2026-10-17 ユーザー：「異常で止める回数と、解くまでの確かめ　契約者」）。
    全体の停止（契約者の無い停止）は、システムの値。"""
    return settings.resolve(tx, key, tenant_id=tenant_id, client_id=None, business_id=None)


def _flapping_wait(tx: Tx, stop: dict[str, Any], interval: int) -> timedelta:
    window = int(_tenant_setting(tx, "stop.flapping.window", stop["tenant_id"]))
    times = tx.one(
        "SELECT count(*) AS n FROM salescall.stop_events s WHERE s.action = 'stop' AND s.cause = %s AND s.scope = %s"
        " AND s.line_id IS NOT DISTINCT FROM %s AND s.event_id <> %s AND s.at > %s - %s * interval '1 second'",
        (stop["cause"], stop["scope"], stop["line_id"], stop["event_id"], stop["at"], window))["n"]
    if times == 0:
        return timedelta(seconds=interval)
    wait = interval * int(_tenant_setting(tx, "stop.flapping.wait_multiplier", stop["tenant_id"])) ** times
    return timedelta(seconds=min(wait, int(_tenant_setting(tx, "stop.flapping.max_wait", stop["tenant_id"]))))


def record_check(tx: Tx, stop_event_id: int, ok: bool, *, evidence: str) -> dict[str, Any]:
    """停止を解く確かめの結果を記録し、決めた回数続けて成功したら解く（同じ取引）。記録を返す（resumed＝解いたか）。"""
    s = tx.one(
        "SELECT event_id, scope, cause::text AS cause, tenant_id, line_id, actor_kind::text AS actor_kind, at"
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
    interval, need = (int(_tenant_setting(tx, interval_key, s["tenant_id"])),
                      int(_tenant_setting(tx, need_key, s["tenant_id"])))
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
