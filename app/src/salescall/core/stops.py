"""運転の状態（Q13）：止める・解く・止まっているか。持ち主 CL-2。DESIGN.md 9-1・Intent.md 10-3。

- 止めるのも解くのも、原因ごとに決めた条件で、システムが行う（原因ごとの意味と自動で解く決まりは `CAUSES` の1か所）。
- **何で止まったか**は `explain` で確かめる（原因の意味・理由・根拠・誰がいつ・数えたエラーとその意味・解く確かめの記録・
  品質の評価の指標。画面はこれを出す。2026-10-15 ユーザー：「何で止まったのかは確認できるようにしてあるの？」）。
- **人の判断で再開できる**（`resume`。理由は必ず書く）。どの原因の停止も、人は解ける：全体の範囲はシステムの管理者だけ、
  契約者の範囲は、その契約者の運営者（データベースが守る。0019）。人が解いたあと、同じ原因で数えるもの（続いたエラー・品質の指標）
  は、解いた時刻から数え直す（解いた直後に、前の数でまた止まらない）。システムは、人が押した緊急停止を解けない。
- 止まっているか＝「まだ解かれていない停止が1つでもあるか」（原因が重なれば、片方を解いても止まったまま）。
- 止めるときは、同じ取引で、まだ発信していない予約を取り消す（DESIGN.md 5-2-1 の2）。
- 止めた・解いたは、知らせの台帳に入れる（同じ取引。core/notify.py。送るのは CL-3）。
- 停止の記録を足すのは、この部品だけ（I-12。app/checks が見張る）。「誰が」は取引の「誰が」をそのまま使う
  （データベースが、違えば拒む）。システムが人の緊急停止を解こうとすると、データベースが拒む。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from salescall.core import errors, notify
from salescall.core.access import Tx

Scope = Literal["system", "tenant", "client", "business", "line"]   # line＝その回線だけ（回線の異常。9-1）
Cause = Literal["manual", "storage_failure", "line_failure", "ai_connection", "project_denied",
                "forbidden_speech", "quality_gate", "budget", "deficit", "judge_unknown"]

SCOPE_NAMES: dict[str, str] = {"system": "全体", "tenant": "契約者", "client": "依頼主", "business": "業務", "line": "回線"}


@dataclass(frozen=True)
class CauseInfo:
    """停止の原因の説明（画面と知らせに出す。1か所）。"""
    meaning: str          # 何が起きたか
    auto_resume: str      # 自動で解く決まり
    before_resume: str    # 人が解く前に確かめること（人が解いたら、どうなるか）


CAUSES: dict[str, CauseInfo] = {
    "manual": CauseInfo("人が押した緊急停止", "自動では解かない（人だけが解く）", "押した理由が片付いたか"),
    "storage_failure": CauseInfo("記録の保存に失敗した（記録できないまま、かけ続けない）",
                                 "試しの書き込みが、決めた回数続けて成功したら解く", "データベースの状態"),
    "line_failure": CauseInfo("その回線の異常が続いた（発信の失敗・回線の上限・端末やアプリの異常）",
                              "回線の応答の確かめが、決めた回数続けて成功したら解く",
                              "端末・アプリの状態。人が解くと、続いたエラーは解いた時から数え直す"),
    "ai_connection": CauseInfo("会話AI・別の耳の異常が続いた（つながらない・Google の側の誤り・設定の誤り・不明のエラー）",
                               "試しのセッションが、決めた回数続けて成功したら解く（設定の誤りは、直すまで確かめも失敗する）",
                               "数えたエラーの意味（会話AIのエラーの一覧）。人が解くと、続いたエラーは解いた時から数え直す"),
    "project_denied": CauseInfo("会話AIのプロジェクトの利用拒否・枠の超過",
                                "いまは、この原因で止める処理は無い（利用拒否は、そのプロジェクトをしばらく使わないことで扱う）",
                                "Google の管理画面の上限"),
    "forbidden_speech": CauseInfo("言ってはいけないことを言った（2つの耳で確かめた）",
                                  "直した新しい版が模擬通話に合格したら解く",
                                  "その通話の場面。人が解くと、その版をもう一度測り直す（段の件数を最初から数える）"),
    "quality_gate": CauseInfo("品質の基準を外れた（判定できない・2つの耳の食い違い・苦情・技術的な失敗 など）",
                              "直した新しい版が模擬通話に合格したら解く",
                              "外れた指標。人が解くと、その版をもう一度測り直す（段の件数を最初から数える）"),
    "judge_unknown": CauseInfo("同じ原因の「判定できない」が2件になった（会話の報告の書き漏れ・業務設定の抜けなど、こちらの誤り）",
                               "原因を直して、その場面を入れた模擬通話に合格したら解く（同じ版でもよい）",
                               "評価の記録の原因と通話。判定待ちの行は、原因が直れば自動で判定し直す"),
    "budget": CauseInfo("費用の1日の上限を超えた", "日が替わったら解く",
                        "上限の設定。上限を上げずに解くと、次の利用の記録でまた止まる"),
    "deficit": CauseInfo("赤字（今月の費用が、今月の報酬を超えた）",
                         "報酬の決まりを直した・月が替わった・月額の日割りが積み上がって、赤字でなくなったら解く（見守りが確かめる）",
                         "その依頼主の報酬の決まりと、今月の費用の内訳。決まりを直さずに解くと、次の利用の記録でまた止まる"),
}


def stop(tx: Tx, *, scope: Scope, cause: Cause, reason: str, evidence: str | None = None,
         tenant_id: UUID | None = None, client_id: UUID | None = None, business_id: UUID | None = None,
         line_id: UUID | None = None) -> dict[str, Any]:
    """止める。止めた記録と、取り消した予約の数を返す。"""
    row = tx.one(
        "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, cause, actor,"
        " actor_kind, reason, evidence)"
        " VALUES (%s, %s, %s, %s, %s, 'stop', %s, salescall.cur_actor(), salescall.cur_actor_kind()::salescall.actor_kind,"
        " %s, %s) RETURNING event_id",
        (scope, tenant_id, client_id, business_id, line_id, cause, reason, evidence),
    )
    released = tx.one("SELECT salescall.release_claims_for_stop(%s) AS n", (row["event_id"],))["n"]
    notify.emit(tx, "stopped", key=f"stop:{row['event_id']}",
                summary=f"発信を止めました（{SCOPE_NAMES[scope]}・{CAUSES[cause].meaning}）。{reason}",
                refs={"stop_event_id": row["event_id"], "scope": scope, "cause": cause, "client_id": client_id,
                      "business_id": business_id, "line_id": line_id})
    return {"event_id": row["event_id"], "released_claims": released}


def resume(tx: Tx, stop_event_id: int, *, reason: str, evidence: str | None = None) -> int:
    """その停止を解く（範囲は停止と同じ）。解いた記録の番号を返す。"""
    row = tx.one(
        "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, resolves_event_id,"
        " actor, actor_kind, reason, evidence)"
        " SELECT scope, tenant_id, client_id, business_id, line_id, 'resume', event_id, salescall.cur_actor(),"
        "        salescall.cur_actor_kind()::salescall.actor_kind, %s, %s"
        " FROM salescall.stop_events WHERE event_id = %s AND action = 'stop' RETURNING event_id",
        (reason, evidence, stop_event_id),
    )
    if row is None:
        raise LookupError(f"その停止は無い（または、取引の範囲の外）：{stop_event_id}")
    notify.emit(tx, "resumed", key=f"resume:{row['event_id']}", summary=f"止めていた発信を解きました（停止 {stop_event_id}）。{reason}",
                refs={"stop_event_id": stop_event_id, "resume_event_id": row["event_id"]})
    return row["event_id"]


def open_stops(tx: Tx, *, tenant_id: UUID | None, client_id: UUID | None,
               business_id: UUID | None, line_id: UUID | None = None) -> list[dict[str, Any]]:
    """まだ解かれていない停止のうち、この範囲にかかるもの（空なら止まっていない）。line_id を渡すと、その回線の停止も含める。"""
    return tx.all(
        "SELECT event_id, scope, cause, reason, at FROM salescall.open_stops(%s, %s, %s, %s) ORDER BY event_id",
        (tenant_id, client_id, business_id, line_id),
    )


def explain(tx: Tx, stop_event_id: int, *, error_limit: int = 20) -> dict[str, Any]:
    """何で止まったか（画面と人の判断のため。1か所）。取引で見える停止だけ。中身：
    stop（範囲・原因・理由・根拠・誰が・いつ）・cause（意味・自動で解く決まり・人が解く前に確かめること）・who_can_resume・
    resolved（解かれていれば、誰が・いつ・なぜ）・errors（その停止に数えたエラーと、一覧で引いた意味と出どころ）・
    checks（解く確かめの記録）・quality（品質の評価の指標）・held_results（品質で止めた版の、まだ送っていない結果。
    人が確かめて送るなら approval.approve_result・直すなら approval.correct_result）。"""
    s = tx.one(
        "SELECT event_id, scope::text AS scope, tenant_id, client_id, business_id, line_id, cause::text AS cause, reason,"
        " evidence, actor, actor_kind::text AS actor_kind, at FROM salescall.stop_events WHERE event_id = %s AND action = 'stop'",
        (stop_event_id,))
    if s is None:
        raise LookupError(f"その停止は無い（または、取引の範囲の外）：{stop_event_id}")
    resolved = tx.one("SELECT event_id, actor, actor_kind::text AS actor_kind, reason, evidence, at FROM salescall.stop_events"
                      " WHERE resolves_event_id = %s", (stop_event_id,))
    found = []
    kinds = errors.COUNTED_FOR.get(s["cause"], ())
    if kinds and s["scope"] in ("system", "line"):
        for e in tx.all("SELECT * FROM salescall.errors_for_stop(%s, %s, %s)", (stop_event_id, list(kinds), error_limit)):
            entry = errors.lookup(errors.ErrorReport(e["source"], e["code"], e["message"]))
            found.append(dict(e, meaning=errors.ACTIONS[e["kind"]].meaning if entry is None else entry.meaning,
                              origin=None if entry is None else entry.origin))
    checks = tx.all("SELECT ok, streak, checked_at, next_check_at, evidence FROM salescall.stop_checks WHERE stop_event_id = %s"
                    " ORDER BY check_id", (stop_event_id,))
    quality, held = None, []
    if s["scope"] == "business":
        quality = tx.one("SELECT version_id, stage_no, cap, used, metrics, evaluated_at FROM salescall.quality_evaluations"
                         " WHERE stop_event_id = %s", (stop_event_id,))
    if quality is not None:
        held = tx.all(
            "SELECT r.task_id, r.version_no, r.business_state::text AS business_state FROM salescall.task_results r"
            " JOIN salescall.attempts a ON a.attempt_id = r.attempt_id WHERE a.version_id = %s AND r.kind = 'judged'"
            " AND NOT EXISTS (SELECT 1 FROM salescall.result_releases x WHERE x.task_id = r.task_id"
            "                 AND x.version_no = r.version_no) ORDER BY r.task_id, r.version_no", (quality["version_id"],))
    info = CAUSES[s["cause"]]
    return {"stop": s, "cause": {"meaning": info.meaning, "auto_resume": info.auto_resume,
                                 "before_resume": info.before_resume},
            "who_can_resume": "システムの管理者" if s["scope"] == "system" else "その契約者の運営者・システムの管理者",
            "resolved": resolved, "errors": found, "checks": checks, "quality": quality, "held_results": held}
