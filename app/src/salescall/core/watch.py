"""見守り：エラーを記録して止めるかを決める・停止を解く確かめを記録して解くかを決める（DESIGN.md 9章・9-1）。持ち主 CL-2。

- エラーの種類と処置は core/errors.py の表だけを読む（ここで2つ目の判断を作らない）。止める・解くの記録は core/stops.py。
- **止めるか**（`decide_stop`）：処置の表の「止める原因・範囲・何回続いたら」と、「何回続いたか」（データベースの1か所
  `salescall.consecutive_errors`。種類ごとの「直った証拠」（core/errors.py の EVIDENCE）より後：回線＝その回線で、お客様のところで
  電話が鳴って終わった発信（回線の部品が番号を受け付けなかった・お客様が出る前に回線の側で終わったではない。0122）／会話AI＝その契約者のどこかの通話が会話に入った。どれも、同じ原因・同じ範囲の停止が
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
- **こちらのプログラムの不具合**（0139。2026-10-25 ユーザー：「3行同じ問題なら、甚大な問題と判断。」）：通話ごとの記録・知らせ・修正依頼は
  `record_error`（電話ごと＝処置の表の per_call）。止めるかは `decide_stop`（理由ごとに、違う行の数。直った証拠＝program_fixed：その種類を
  直したと記録され、そのあとプログラムが替わったこと・こちらの不具合なしにお客様と話し終えた電話）。止めた停止は、その種類を直したと記録
  され、そのあとプログラムが替わったら `resume_fixed` が解く（管理者・契約者も解ける）。
- **テスト番号で確かめる停止**（core/ring_test.checks_stop）は、1回鳴れば解く（`record_check`。2026-10-25 夜 管理者「鳴ったら、テストＯＫ
  なだけ」。0143）。
"""

from __future__ import annotations

import hashlib

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
    # 知らせの鍵：同じ種類は、同じ範囲で1時間に1回。こちらのプログラムの不具合で終えた電話（per_call）は電話ごと（2026-10-25 ユーザー：
    # 「契約者(管理者含む)と、開発AIの担当、記録にそれを伝え」）
    key = (f"{kind}:attempt:{attempt_id}" if action.per_call and attempt_id is not None
           else f"{kind}:{row['tenant']}:{row['client']}:{row['hour']}")
    refs = {"error_id": row["error_id"], "kind": kind, "attempt_id": attempt_id}
    # 根源的なもの（待っても直らない）は、止めずに直す担当へ修正依頼を出す（2026-10-17 ユーザー：「人はそのプロセスをチェック
    # もしくは承認するだけ」）。管理者には、どの種類も知らせる（確かめ・承認のため）。契約者には、処置の表が「知らせる」とした種類だけ
    fixer = action.fixer or (action.lasting_fixer if action.lasting_fixer and _lasting(tx, kind) else None)
    new_request = False
    if fixer is not None:
        # 同じ不具合は、契約者をまたいでも修正依頼1つ（直すのは同じプログラム）。別の不具合（どこで・何が起きたか）は別の依頼
        # （⛔ 2026-10-22 の見直し：前は契約者ごとに1つで、同じ不具合が契約者の数だけ出て、同じ契約者の別の不具合は最初の依頼に
        # まとめられて隠れた）
        what = hashlib.sha256(f"{report.source}\n{report.code}\n{report.message}".encode("utf-8")).hexdigest()[:16]
        new_request = fix_requests.request(tx, fixer, kind, key=f"error:{kind}:{what}", refs=refs,
                                           summary=errors.LABELS[kind] + ("" if action.fixer else "（1時間以上続いている）"))
    # 管理者へは、修正依頼を新しく出したとき（同じ不具合を契約者の数だけ・1時間ごとに知らせない）と、直す担当の無い種類のとき、
    # こちらのプログラムの不具合で終えた電話（per_call。電話ごとに）。契約者へは、処置の表が「知らせる」とした種類を、その契約者の範囲で
    # 1時間に1回（per_call は電話ごと）。修正依頼は、同じ不具合で1つ（電話ごとに重ねない）
    admin = new_request or fixer is None or action.per_call
    if admin or action.notify:
        # こちらの不具合で終えた電話（per_call）は、その行が復旧を待っていること（何が直れば動くか）も文に入れる（0143。⛔ 見直し21a の 4：
        # 最後に残った行が復旧待ちで動かないとき、だれも知らなかった。新しい知らせは増やさない）
        summary = ((f"{fixer} に修正依頼：" if fixer else "エラー：") + errors.LABELS[kind]
                   + (f"。この行は復旧を待っています（{errors.RECOVERY_WAIT}）" if action.per_call else ""))
        notify.emit(tx, "error_notice", key=f"error:{key}", summary=summary,
                    refs={**refs, "fixer": fixer}, admin=admin, contractor=action.notify)
    return kind


def _lasting(tx: Tx, kind: str) -> bool:
    """その契約者で、その種類が errors.LASTING 以上続いているか（その長さより長くあかずに起き続けている）。数えるのはデータベースの
    salescall.error_lasting の1か所（契約者のすべての依頼者のエラーを数える。⚠️ 前は記録した取引のまま数えたので、依頼者の取引では
    その依頼者のエラーしか見えず、契約者全体で続いていても修正依頼が出なかった＝見直し6）。"""
    return tx.one("SELECT salescall.error_lasting(%s, %s) AS lasting", (kind, errors.LASTING))["lasting"]


def decide_stop(tx: Tx, kind: str, *, line_id: UUID | None = None) -> dict[str, Any] | None:
    """処置の表のとおり、止めるなら止める（その契約者の取引で呼ぶ：契約者＝その契約者の会話AIの異常／回線＝その回線）。
    止めた停止の記録を返す（止めなければ None）。根源的なもの（直す担当へ修正依頼を出したもの）は、管理者にも知らせる（確かめ・承認のため）。"""
    action = errors.ACTIONS[kind]
    if action.stop_cause is None:
        return None
    if action.stop_scope == "line" and line_id is None:
        raise ValueError("回線を止める種類なのに、回線がありません")
    line = line_id if action.stop_scope == "line" else None
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    if stops.open_same(tx, scope=action.stop_scope, cause=action.stop_cause, tenant_id=tenant, line_id=line) is not None:
        return None                                  # もう止まっている（重ねて止めない）
    n = tx.one("SELECT salescall.consecutive_errors(%s, %s, %s, %s) AS n",
               (list(errors.counted_with(kind)), action.stop_cause, errors.EVIDENCE[kind], line))["n"]
    if n is None:
        raise PermissionError("この取引では、その範囲のエラーを数えられません（契約者の取引で数える。回線は、その回線の契約者の取引）")
    need = 1 if action.stop_after is None else int(_tenant_setting(tx, action.stop_after, tenant))
    if n < need:
        return None
    how = (f"根源的なもの：{action.fixer} に修正依頼を出しました。直るまで新しい発信を待ちます" if action.fundamental
           else "一時的なもの：直ったら自動で続けます")
    return stops.stop(tx, scope=action.stop_scope, cause=action.stop_cause,
                      reason=f"{action.meaning}（続けて {n} 回。基準 {need} 回。{how}）", evidence=STOPPED_BY + kind,
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


STOPPED_BY = "エラーの種類："   # エラーで止めた停止の根拠の頭（そのあとにエラーの種類。書くのは decide_stop、読むのは `stopped_kind`）


def stopped_kind(evidence: str | None) -> str | None:
    """エラーで止めた停止の根拠の文から、止めたエラーの種類を読む（1か所。書くのは decide_stop＝`STOPPED_BY`＋種類。エラーで止めたので
    なければ None。読むのは lasting_stops・resume_fixed）。"""
    return evidence[len(STOPPED_BY):] if (evidence or "").startswith(STOPPED_BY) else None


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
    # テスト番号で確かめる停止（core/ring_test.checks_stop の1か所）は、1回鳴れば解く（2026-10-25 夜 管理者：「鳴ったら、テストＯＫなだけ」。
    # ⛔ 0136〜0142：アプリの確かめ用の回数＝stop.line.resume_after_successes（3）を使い、再開のたびに約2分で3回、管理者の電話を鳴らした＝
    # 見直し21a の 3）。アプリの確かめ（確かめの部品）は、今までどおり決めた回数
    from salescall.core import ring_test   # テスト番号の部品が、試行の部品を通してこの部品を読むので、ここで読む（読み合いにしない）
    interval = int(_tenant_setting(tx, interval_key, s["tenant_id"]))
    need = 1 if ring_test.checks_stop(tx, s) else int(_tenant_setting(tx, need_key, s["tenant_id"]))
    streak = ((prev["streak"] if prev is not None else 0) + 1) if ok else 0
    next_at = now + _flapping_wait(tx, s, interval)
    tx.execute("INSERT INTO salescall.stop_checks (stop_event_id, ok, streak, checked_at, next_check_at, evidence)"
               " VALUES (%s, %s, %s, %s, %s, %s)", (stop_event_id, ok, streak, now, next_at, evidence))
    resumed = None
    if ok and streak >= need:
        resumed = stops.resume(tx, stop_event_id, reason=f"確かめが続けて {streak} 回成功（基準 {need} 回）", evidence=evidence)
    return {"ok": ok, "streak": streak, "need": need, "next_check_at": next_at, "resumed": resumed}


def lasting_stops(tx: Tx) -> list[int]:
    """一時的な異常で止めたまま errors.LASTING 以上たった停止について、止めたエラーの種類の担当へ修正依頼を出す（1か所。一時的な
    異常も1時間以上続けば修正依頼＝Intent.md 10-3。鍵はエラーで数えるときと同じ＝同じ不具合の依頼は1つ）。この取引が見る範囲の停止
    （全体の取引＝全体の停止／契約者の取引＝その契約者の停止）。依頼を出した停止の番号を返す。見守り役が、確かめの部品の有る無しに
    関係なく呼ぶ（⚠️ 見直し8では解く確かめの中で出していたので、確かめの部品の無い PC では、何時間止まっても依頼が出なかった＝見直し9。
    その前は、止まると新しいエラーが起きないので数えられなかった＝見直し8）。
    テスト番号で確かめる回線の停止（core/ring_test.checks_stop）は、止まっていた長さを、テスト番号を鳴らしてよい時間の分だけで数える
    （ring_test.checkable_seconds。2026-10-24 ユーザー：テスト番号はいまユーザー自身の電話なので夜は鳴らさない＝夜は確かめていない。
    確かめられなかった夜の待ちで「確かめが通らない」とはしない）。"""
    from salescall.core import ring_test   # テスト番号の部品が、試行の部品を通してこの部品を読むので、ここで読む（読み合いにしない）
    asked = []
    for s in tx.all(
            "SELECT s.event_id, s.tenant_id, s.evidence, s.cause::text AS cause, s.line_id, s.at FROM salescall.open_stop_events s"
            " WHERE s.actor_kind = 'system' AND s.cause::text = ANY(%s) AND s.at <= now() - %s"
            " AND (s.scope = 'system') = (salescall.cur_tenant() IS NULL) ORDER BY s.event_id",
            (list(PROBE_SETTINGS), errors.LASTING)):
        kind = stopped_kind(s["evidence"])
        fixer = errors.ACTIONS[kind].lasting_fixer if kind in errors.ACTIONS else None
        if fixer is None:
            continue   # 根源的なもの（起きたときに依頼を出した）・エラーで止めたのではないもの
        if ring_test.checks_stop(tx, s) and ring_test.checkable_seconds(tx, s["at"]) < errors.LASTING.total_seconds():
            continue   # テスト番号で確かめる停止：鳴らしてよい時間の中で、まだ1時間たっていない（夜の待ちは数えない）
        fix_requests.request(tx, fixer, kind, key=f"error:{kind}:{s['tenant_id']}",
                             summary=errors.LABELS[kind] + "（止まったまま1時間以上続いている）",
                             refs={"stop_event_id": s["event_id"], "kind": kind})
        asked.append(s["event_id"])
    return asked


FIXED_BY_PROGRAM = ("program_trouble",)   # 直したプログラムの版に替わったら解く停止の原因（1か所。0139）


def resume_fixed(tx: Tx) -> list[int]:
    """こちらのプログラムの不具合で止めた停止（すべての行にかかわる甚大な問題。原因 program_trouble）を、止めたあとで、止めた種類の不具合
    を直したと記録され、そのあと動いているプログラムの版が替わっていたら解く（1か所。直した時刻はデータベースの salescall.trouble_fixed_at
    ＝行の休みが戻る時刻と同じ。2026-10-25 夜 管理者の決定。⛔ 0139：版が替われば、どの種類でも解いた）。止めた種類は、停止の根拠
    （`STOPPED_BY`＋種類）から読む。この取引が見る範囲のシステムが止めた停止（契約者の取引＝その契約者の停止）。解いた停止の番号を返す。
    見守り役の決まった間隔の仕事が呼ぶ（supervisor.jobs の fixed_stops）。解いたあとに同じ不具合が続けば、違う行の数をまた数えて止める
    （数えるのは解いた時より後だけ）。"""
    done = []
    for s in tx.all(
            "SELECT s.event_id, s.evidence, s.at FROM salescall.open_stop_events s WHERE s.actor_kind = 'system'"
            " AND s.cause::text = ANY(%s) AND (s.scope = 'system') = (salescall.cur_tenant() IS NULL) ORDER BY s.event_id",
            (list(FIXED_BY_PROGRAM),)):
        kind = stopped_kind(s["evidence"])
        if kind is None or not tx.one("SELECT salescall.trouble_fixed_at(%s) > %s AS f", (kind, s["at"]))["f"]:
            continue
        stops.resume(tx, s["event_id"], reason="止めた種類の不具合を直したと記録され、そのあと直したプログラムの版に替わった",
                     evidence="直した記録とプログラムの版の替わり")
        done.append(s["event_id"])
    return done


def checkable(tx: Tx) -> list[dict[str, Any]]:
    """確かめで解ける停止（システムが止めた、確かめで解ける原因の、まだ解かれていない停止のうち、この取引が確かめを書けるもの：
    全体の取引＝全体の停止／契約者の取引＝その契約者の停止）と、次に確かめてよいまでの秒（wait。0＝いま）。1か所。
    **声が聞き取れずに止めた回線を、止め直した**（テスト番号が鳴って再開したあと、また声が聞き取れずに止めた。続けて止めた回数＝
    データベースの salescall.audio_stops_in_a_row の1か所）ときは、最初の確かめを、止めた時から、長さの並び（line.test_ring_gaps＝
    core/ring_test.gap_after の1か所。初め 15→30→60→120→240分）の（止め直した回数）番目の長さのあとにする（2026-10-29 管理者：「テスト番号が
    鳴れば再開のあと、また聞き取れなければ止め直し、次に試すまで 15→30→60→120→240分あけます。+管理者か契約者が、直ったことを確かめて手で
    再開も可能」。声が聞き取れて話し終えた電話があれば、最初に戻る。⛔ 0143 まで：テスト番号では声が通るかを確かめられないのに、止めては
    すぐ再開し、1日に停止13回・お詫びの電話40本をくり返した＝見直し22 の場面 b5b）。声以外の理由で止めた回線は今のまま（すぐ確かめる）。
    手で解く道（管理者・契約者が理由を書いて解く＝stops.resume）は、どの停止にも今のまま効く。"""
    from salescall.core import ring_test   # テスト番号の部品が、試行の部品を通してこの部品を読むので、ここで読む（読み合いにしない）
    rows = tx.all(
        "SELECT s.event_id, s.scope, s.cause::text AS cause, s.line_id,"
        " greatest(0, extract(epoch FROM coalesce((SELECT c.next_check_at FROM salescall.stop_checks c"
        "   WHERE c.stop_event_id = s.event_id ORDER BY c.check_id DESC LIMIT 1), now()) - now()))::float AS wait,"
        " NOT EXISTS (SELECT 1 FROM salescall.stop_checks c WHERE c.stop_event_id = s.event_id) AS fresh,"
        " salescall.audio_stops_in_a_row(s.event_id) AS again, extract(epoch FROM now() - s.at)::float AS since"
        " FROM salescall.open_stop_events s"
        " WHERE s.actor_kind = 'system' AND s.cause::text = ANY(%s)"
        " AND (s.scope = 'system') = (salescall.cur_tenant() IS NULL)"   # 確かめを書ける停止だけ（0029 の (3)）
        " ORDER BY s.event_id", (list(PROBE_SETTINGS),))
    for r in rows:
        if r.pop("fresh") and r["again"] > 1:   # 止め直した：止めた時から、並びの（止め直した回数）番目の長さのあとに確かめる
            r["wait"] = max(r["wait"], ring_test.gap_after(tx, r["again"] - 1) - r["since"])
        del r["again"], r["since"]
    return rows


def due_checks(tx: Tx) -> list[dict[str, Any]]:
    """いま確かめてよい停止（`checkable` のうち、次に確かめてよい時刻が来たもの）。"""
    return [s for s in checkable(tx) if s["wait"] <= 0]
