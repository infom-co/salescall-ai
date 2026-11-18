"""見守り：エラーを記録して止めるかを決める・停止を解く確かめを記録して解くかを決める（DESIGN.md 9章・9-1）。持ち主 CL-2。

- エラーの種類と処置は core/errors.py の表だけを読む（ここで2つ目の判断を作らない）。止める・解くの記録は core/stops.py。
- **止めるか**（`decide_stop`）：処置の表の「止める原因・範囲」の種類（AIの不具合＝会話の側が「止まった」と決めたAI）が、同じ原因の
  前の解きのあとに1つでもあれば止める（1回で止める。数え始めはデータベースの salescall.errors_counted_since の1か所＝管理者か契約者が
  再開した時刻・確かめで解けたなら、その確かめを始めた時刻。0159。その種類があったかを見るのは `_happened` の1か所）。
  同じ原因・同じ範囲で、まだ解かれていない停止があれば、重ねて止めない（ただし、契約者以上が緊急の道で再開した AIの不具合の停止は、
  緊急の道で補えない種類＝会話AI・別の耳・緊急の道が代われない判定・緊急の道そのものが止まったときは、止まっていないものとみなして、
  また止める。0155。見方は緊急の道を出せるか `ai_blocker` と同じ。その種類そのものが無ければ止めない＝⛔ 見直し25a の A：前は、その種類
  が無くても補える種類と合わせて数えて届いたことにし、緊急の道で再開したあと、その再開のもとになった判定の止まりで「会話AIが止まった」
  として止め直した）。
  会話AIの生の誤り（つながらない・Google の側の誤り・不明・設定の誤り＝`errors.CONVO_RAW`）では止めない（記録・知らせ・修正依頼だけ。
  CL-1 の連絡帳 4-69 の 4-179 の (a)）。⛔ 2026-11-02 まで：それらを、どこかの通話が会話に入ったら数え直す数（データベースの
  salescall.consecutive_errors の証拠 in_conversation）で数え、設定 stop.ai.stop_after_failures の数で止めていた（止まったかを会話の側と
  2か所で決めていた。0170 で外した）。
- **止める範囲は、その契約者**（全体は止めない）。会話AIの枠は契約者ごと（契約者が自分のキー・プロジェクトを持つ。
  2026-10-16 ユーザー。Intent.md 1-4）なので、会話AIの異常も、その契約者のエラーだけを数えて、その契約者だけを止める
  （2026-10-17 見直し：前は全体で数えて全体を止め、ある契約者のキーの誤りで、ほかの契約者まで止まった）。
- **回線の異常と、こちらのプログラムの不具合は、ここでは数えも止めもしない**：回線の休みと再開は回線の欄、こちらの不具合の止めと解きは
  契約者の欄（状態と出来事の表＝DESIGN.md 17章。core/health）が決める（`errors.TABLE_CAUSES`）。こちらの不具合で終えた電話ごとの記録・
  知らせ・修正依頼は、表を引く入口が `record_error` で行う（電話ごと＝処置の表の per_call）。
  ⛔ 4段目まで：回線の異常の数（仮の値 stop.line.stop_after_failures）で回線を止め、止めた回線をテスト番号やアプリの確かめで解き
  （ring_test.checks_stop・checkable の声の止め直し）、こちらの不具合で止めた停止を直した版で解いていた（resume_fixed）。5段目（0147）で外した。
- **解くか**（`record_check`）：システムが止めた停止のうち、確かめで解ける原因（会話AI）だけ。確かめの結果を記録し、
  決めた回数続けて成功したら、同じ取引で解く。確かめを始めた時刻よりあとに、止める種類のエラーがあれば、成功でも解かない
  （確かめの間にまた止まった。0159）。解いたあとは、その確かめを始めた時刻から数え直す（それより前の出来事は、通った確かめが
  「回復した」と示している。⛔ 見直し25a の C：解いた時刻から数えたので、解くのと同時に書いた止まったAIが数えられずに消えた）。
  **次に確かめてよい時刻より前の確かめは受け付けない**（待ち＝確かめの間隔の1つだけ。⛔ 見直し29b の 6 まで：同じ日に止まった回数で
  倍々に延ばした＝ばたつきの待ち）。
- 確かめそのもの（試しのセッション）は、持ち主の部品が行い、結果だけをここへ渡す。
- 管理者か契約者が押した緊急停止（manual）と、確かめでは解けない原因（言ってはいけないこと・品質の基準）は、ここでは解かない
  （品質は新しい版の合格で解く。持ち主の処理が解く）。
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


def decide_stop(tx: Tx, kind: str) -> dict[str, Any] | None:
    """処置の表のとおり、止めるなら止める（その契約者の取引で呼ぶ。止めるのは、その契約者のAIの不具合だけ）。
    止めた停止の記録を返す（止めなければ None）。状態の表が決める原因（`errors.TABLE_CAUSES`）は止めない。止める種類は、どれも1回で止める
    （`_happened`）。⛔ 2026-11-02 まで：根源的なもの（設定の誤り・不明）は理由に「修正依頼を出しました」を書き、管理者に写しを送った
    （会話AIの生の誤りで止めるのを外したので、止める種類に根源的なものは無い＝試験が見張る）。"""
    action = errors.ACTIONS[kind]
    if action.stop_cause is None or action.stop_cause in errors.TABLE_CAUSES:
        return None
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    # もう止まっている（重ねて止めない）。緊急の道で再開した停止は、補える種類（Anthropic の Sonnet）なら止まっているとみなし、補えない
    # 種類ならみなさない（また止める。0155）
    if stops.open_same(tx, scope=action.stop_scope, cause=action.stop_cause, tenant_id=tenant,
                       passed_too=action.emergency) is not None:
        return None
    if not _happened(tx, kind):
        return None
    # 止める種類は、どれも回復の確かめが通れば自動で続ける（止まったまま1時間以上なら、処置の表の担当へ修正依頼＝`lasting_stops`）。
    # 管理者へは、止まったAIのエラーの知らせ（`record_error`）が届いている
    return stops.stop(tx, scope=action.stop_scope, cause=action.stop_cause, reason=action.meaning, evidence=STOPPED_BY + kind,
                      tenant_id=tenant)


def _happened(tx: Tx, kind: str, *, since: Any = None) -> bool:
    """止める種類のエラーが、同じ原因の前の解き（と since）のあとに、その契約者で1つでもあるか（1か所。止めるか＝`decide_stop`・緊急の
    道を出せるか＝`ai_blocker`・確かめの間にまた止まったか＝`record_check` が読む）。数えるのはデータベースの salescall.errors_since
    （契約者の取引で）。その種類そのものだけを見る（ほかの種類で、この種類が起きたことにしない。⛔ 見直し25 の試験で見つけた）。"""
    action = errors.ACTIONS[kind]
    n = tx.one("SELECT salescall.errors_since(%s, %s, %s) AS n", ([kind], action.stop_cause, since))["n"]
    if n is None:
        raise PermissionError("この取引では、その範囲のエラーを数えられません（契約者の取引で数える）")
    return n > 0


def ai_blocker(tx: Tx) -> str | None:
    """緊急の道で補えないAI（errors.EMERGENCY_BLOCKERS）が、前の解きのあとに止まったか（止まっていれば、その種類。契約者の取引で）。
    1か所：緊急の道を出せるか（stops.emergency_possible）と、出したあとに止め直すか（`decide_stop`）が、同じ見方を読む。"""
    return next((k for k in errors.EMERGENCY_BLOCKERS if _happened(tx, k)), None)


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
        if tenant_id is None:      # 止める種類（会話AI）は、契約者のキーで起きる。どの契約者か分からなければ数えられない
            raise ValueError(f"止める種類のエラー（{kind}）なのに、契約者がありません")
        access.in_tenant(actor, tenant_id, lambda tx: decide_stop(tx, kind))
    return kind


def decide_ai_stops(tx: Tx) -> list[dict[str, Any]]:
    """AIの不具合（原因 ai_connection）で、その契約者の新しい電話を止めるかを決める（契約者の取引で。1か所）。どの種類も `decide_stop`
    （もう止まっていれば何もしない・解いたあとの古い出来事は数えない）。どの通話で書いたかは見ない（数えるのは契約者のエラー）。⛔ 見直し25a
    の A：前は、その通話で前に記録した種類を読み直して決めたので、古い種類で止め直すことがあった。止めた停止の記録を返す。"""
    done = []
    for kind in errors.COUNTED_FOR["ai_connection"]:
        got = decide_stop(tx, kind)
        if got is not None:
            done.append(got)
    return done


def after_ai_troubles(access: Access, actor: Actor, tenant_id: UUID) -> list[dict[str, Any]]:
    """止まったAI（core/ai_troubles.note が書いたもの）を書いたあと、その契約者の新しい電話を止めるかを決める入口（1か所。通話ごとの
    書き手が書いたすぐあと・回線ごとの処理が通話のあと・通話後の確かめの入口が呼ぶ。決めるのは `decide_ai_stops`。決められなかった
    ときも、見守り役の仕事 ai_stops が決め直す）。止めた停止の記録を返す。"""
    return access.in_tenant(actor, tenant_id, decide_ai_stops)


def _tenant_setting(tx: Tx, key: str, tenant_id: UUID | None) -> Any:
    """止める・解く決まりの値（契約者ごと＝2026-10-17 ユーザー：「異常で止める回数と、解くまでの確かめ　契約者」）。
    全体の停止（契約者の無い停止）は、システムの値。"""
    return settings.resolve(tx, key, tenant_id=tenant_id, client_id=None, business_id=None)


STOPPED_BY = "エラーの種類："   # エラーで止めた停止の根拠の頭（そのあとにエラーの種類。書くのは decide_stop、読むのは `stopped_kind`）


def stopped_kind(evidence: str | None) -> str | None:
    """エラーで止めた停止の根拠の文から、止めたエラーの種類を読む（1か所。書くのは decide_stop＝`STOPPED_BY`＋種類。エラーで止めたので
    なければ None。読むのは lasting_stops）。"""
    return evidence[len(STOPPED_BY):] if (evidence or "").startswith(STOPPED_BY) else None


def check_begins(tx: Tx) -> Any:
    """確かめを始める時刻（データベースの時刻。`record_check` に渡す）。"""
    return tx.one("SELECT now() AS t")["t"]


def record_check(tx: Tx, stop_event_id: int, ok: bool, *, evidence: str, begun_at: Any) -> dict[str, Any]:
    """停止を解く確かめの結果を記録し、決めた回数続けて成功したら解く（同じ取引）。begun_at＝確かめを始めた時刻（`check_begins`）。
    その時刻よりあとに、止める数に届いたエラーがあれば、成功でも成功と数えない（確かめの間にまた止まった）。解いたら、その時刻から
    数え直す（解いた記録の counted_from）。記録を返す（resumed＝解いたか）。"""
    s = tx.one(
        "SELECT event_id, scope, cause::text AS cause, tenant_id, line_id, actor_kind::text AS actor_kind, at"
        " FROM salescall.open_stop_events s WHERE s.event_id = %s", (stop_event_id,))
    if s is None:
        raise LookupError(f"まだ解かれていない停止ではありません（または、取引の範囲の外）：{stop_event_id}")
    if s["actor_kind"] != "system" or s["cause"] not in PROBE_SETTINGS:
        raise NotProbeable(f"確かめで解ける停止ではありません（原因 {s['cause']}・止めたのは {s['actor_kind']}）")
    if begun_at is None:
        raise ValueError("確かめを始めた時刻（check_begins）が要ります")
    now = tx.one("SELECT now() AS t")["t"]
    prev = tx.one("SELECT ok, streak, next_check_at FROM salescall.stop_checks WHERE stop_event_id = %s"
                  " ORDER BY check_id DESC LIMIT 1", (stop_event_id,))
    if prev is not None and now < prev["next_check_at"]:
        raise NotDue(f"次に確かめてよいのは {prev['next_check_at']} から")
    interval_key, need_key = PROBE_SETTINGS[s["cause"]]
    interval = int(_tenant_setting(tx, interval_key, s["tenant_id"]))
    need = int(_tenant_setting(tx, need_key, s["tenant_id"]))
    if ok and s["scope"] == "tenant":
        late = next((k for k in errors.COUNTED_FOR[s["cause"]] if _happened(tx, k, since=begun_at)), None)
        if late is not None:
            ok, evidence = False, f"{evidence}（確かめの間に、また止まった：{errors.LABELS[late]}）"
    streak = ((prev["streak"] if prev is not None else 0) + 1) if ok else 0
    # 次に確かめてよい時刻は、確かめの間隔の1つだけ（2026-10-31 管理者：「確かめが通れば自動で再開します」。⛔ 見直し29b の 6：前は同じ
    # 日に同じ原因で止まった回数に応じて、間隔を倍々に延ばした＝最長4時間。CL-2 が仮に足した「ばたつき」の待ちで、ご決定に無かった）
    next_at = now + timedelta(seconds=interval)
    tx.execute("INSERT INTO salescall.stop_checks (stop_event_id, ok, streak, checked_at, next_check_at, evidence, begun_at)"
               " VALUES (%s, %s, %s, %s, %s, %s, %s)", (stop_event_id, ok, streak, now, next_at, evidence, begun_at))
    resumed = None
    if ok and streak >= need:
        resumed = stops.resume(tx, stop_event_id, reason=f"確かめが続けて {streak} 回成功（基準 {need} 回）", evidence=evidence,
                               counted_from=begun_at)
    return {"ok": ok, "streak": streak, "need": need, "next_check_at": next_at, "resumed": resumed}


def lasting_stops(tx: Tx) -> list[int]:
    """一時的な異常で止めたまま errors.LASTING 以上たった停止について、止めたエラーの種類の担当へ修正依頼を出す（1か所。一時的な
    異常も1時間以上続けば修正依頼＝Intent.md 10-3。鍵はエラーで数えるときと同じ＝同じ不具合の依頼は1つ）。この取引が見る範囲の停止
    （全体の取引＝全体の停止／契約者の取引＝その契約者の停止）。依頼を出した停止の番号を返す。見守り役が、確かめの部品の有る無しに
    関係なく呼ぶ（⚠️ 見直し8では解く確かめの中で出していたので、確かめの部品の無い PC では、何時間止まっても依頼が出なかった＝見直し9。
    その前は、止まると新しいエラーが起きないので数えられなかった＝見直し8）。回線の休みが続くときの知らせと修正依頼は、回線の状態の表が
    出す（L-20。⛔ 4段目まで：止めた回線の停止を、テスト番号を鳴らしてよい時間だけで数えてここで出した）。"""
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
        fix_requests.request(tx, fixer, kind, key=f"error:{kind}:{s['tenant_id']}",
                             summary=errors.LABELS[kind] + "（止まったまま1時間以上続いている）",
                             refs={"stop_event_id": s["event_id"], "kind": kind})
        asked.append(s["event_id"])
    return asked


def checkable(tx: Tx) -> list[dict[str, Any]]:
    """確かめで解ける停止（システムが止めた、確かめで解ける原因の、まだ解かれていない停止のうち、この取引が確かめを書けるもの：
    全体の取引＝全体の停止／契約者の取引＝その契約者の停止）と、次に確かめてよいまでの秒（wait。0＝いま）。1か所。
    手で解く道（管理者・契約者が理由を書いて解く＝stops.resume）は、どの停止にも今のまま効く。⛔ 4段目まで：声が聞き取れずに止め直した
    回線の最初の確かめを、止め直した回数であけた（audio_stops_in_a_row。いまは回線の欄の次のテストの時刻＝17章）。"""
    return tx.all(
        "SELECT s.event_id, s.scope, s.cause::text AS cause, s.line_id,"
        " greatest(0, extract(epoch FROM coalesce((SELECT c.next_check_at FROM salescall.stop_checks c"
        "   WHERE c.stop_event_id = s.event_id ORDER BY c.check_id DESC LIMIT 1), now()) - now()))::float AS wait"
        " FROM salescall.open_stop_events s"
        " WHERE s.actor_kind = 'system' AND s.cause::text = ANY(%s)"
        " AND (s.scope = 'system') = (salescall.cur_tenant() IS NULL)"   # 確かめを書ける停止だけ（0029 の (3)）
        " ORDER BY s.event_id", (list(PROBE_SETTINGS),))


def due_checks(tx: Tx) -> list[dict[str, Any]]:
    """いま確かめてよい停止（`checkable` のうち、次に確かめてよい時刻が来たもの）。"""
    return [s for s in checkable(tx) if s["wait"] <= 0]
