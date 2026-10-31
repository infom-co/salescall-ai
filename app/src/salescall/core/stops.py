"""運転の状態（Q13）：止める・解く・止まっているか。持ち主 CL-2。DESIGN.md 9-1・Intent.md 10-3。

- 止めるのも解くのも、原因ごとに決めた条件で、システムが行う（原因ごとの意味と自動で解く決まりは `CAUSES` の1か所）。
- **何で止まったか**は `explain` で確かめる（原因の意味・理由・根拠・誰がいつ・数えたエラーとその意味・解く確かめの記録・
  品質の評価の指標。画面はこれを出す。2026-10-15 ユーザー：「何で止まったのかは確認できるようにしてあるの？」）。
- **契約者の判断で再開できる**（`resume`。理由は必ず書く）。どの原因の停止も、管理者か契約者が解ける：全体の範囲は管理者だけ、
  契約者の範囲は、その契約者（データベースが守る。0019）。管理者か契約者が解いたあと、同じ原因で数えるもの（続いたエラー・品質の指標）
  は、解いた時刻から数え直す（解いた直後に、前の数でまた止まらない）。システムは、管理者か契約者が押した緊急停止を解けない。
- 止まっているか＝「まだ解かれていない停止が1つでもあるか」（原因が重なれば、片方を解いても止まったまま）。
- 止めるときは、同じ取引で、まだ発信していない予約を取り消す（DESIGN.md 5-2-1 の2）。
- 止めた・解いたは、知らせの台帳に入れる（同じ取引。core/notify.py。送るのは CL-3）。
- **止めているあいだも、お客様との約束を果たす電話を通すか**は、原因ごとに、データベースの表 `salescall.stop_lets_through` の
  1か所で決める（0116。約束を果たす電話の種類＝`KEEPING_KINDS`。電話がどの種類かは、データベースの1か所 `salescall.keeping_kind`）。
  門は `blocking`、候補を出す処理・これからかける約束の一覧・ほかの依頼者の約束・止めたときの予約の取り消しは、データベースの
  同じ関数（salescall.blocking_stops・salescall.lets_through）を読む。いま通すのは、品質の停止でのお詫びの電話だけ（業務の話し方を
  使わない決まった文面だけなので）。緊急停止・回線や会話AIの異常で止めたときは、どれもかけない。かけられなかった約束は、次に
  つながったときにお詫びから始める（core/next_action）。赤字では止めない（知らせるだけ＝2026-10-22 深夜 ユーザー。前の、赤字の停止
  で約束の3つを通す行は 0118 で外した）。
- 停止の記録を足すのは、この部品だけ（I-12。app/checks が見張る）。「誰が」は取引の「誰が」をそのまま使う
  （データベースが、違えば拒む）。システムが管理者か契約者が押した緊急停止を解こうとすると、データベースが拒む。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from salescall.core import ai_troubles, errors, health
from salescall.core import notify as _notify
from salescall.core.access import Tx

Scope = Literal["system", "tenant", "client", "business", "line"]   # line＝その回線だけ（回線の異常。9-1）
Cause = Literal["manual", "line_failure", "ai_connection", "forbidden_speech", "quality_gate", "judge_unknown", "program_trouble"]
# 止める処理の無い原因（保存の失敗・会話AIの利用拒否・費用の上限・赤字。いまはどれも止めずに、修正依頼・次のキー・知らせで続ける＝
# 2026-10-16・10-17・10-22 深夜 ユーザー）：新しく止めるのはデータベースが断る（守り stop_events_cause_retired。0148＝見直し23 の (4)）。
# 前の記録の値としては残す（`CAUSES` の説明も残す。`Cause` には入れない）
RETIRED_CAUSES = ("storage_failure", "project_denied", "budget", "deficit")

SCOPE_NAMES: dict[str, str] = {"system": "全体", "tenant": "契約者", "client": "依頼者", "business": "業務", "line": "回線"}


@dataclass(frozen=True)
class CauseInfo:
    """停止の原因の説明（画面と知らせに出す。1か所）。"""
    label: str            # 短い名前（知らせに出す。注意は短く、詳しくは画面の「止まった理由」で。2026-10-17 ユーザー）
    meaning: str          # 何が起きたか
    auto_resume: str      # 自動で解く決まり
    before_resume: str    # 管理者か契約者が解く前に確かめること（管理者か契約者が解いたら、どうなるか）


# お客様との約束を果たす電話の種類（名前と説明。電話がどれに当たるかは、データベースの salescall.keeping_kind の1か所。原因ごとに
# 通す種類は、データベースの表 salescall.stop_lets_through の1か所）。番号の聞き直しは入らない（ユーザーが挙げたのは、この3つだけ）
KEEPING_KINDS: dict[str, str] = {
    "apology": "お詫びの電話（言ってはいけないことを言ったあとの。決まった文面だけ）",
    "resume": "途中で切れた通話の、お詫びのかけ直し",
    "promised": "お客様と約束したかけ直し",
}


CAUSES: dict[str, CauseInfo] = {
    "manual": CauseInfo("緊急停止", "管理者か契約者が押した緊急停止", "自動では解かない（管理者か契約者だけが解く）",
                        "押した理由が片付いたか"),
    "storage_failure": CauseInfo("保存の失敗", "記録の保存の失敗（いまは、この原因で止める処理は無い：保存の失敗では止めず、CL-2 に"
                                 "修正依頼を出して、ほかの行を続ける。2026-10-16 ユーザー）",
                                 "自動では解かない（この原因で止める処理が無い）", "データベースの状態"),
    "line_failure": CauseInfo("回線の異常", "その回線の異常が続いた（いまは、この原因で止める処理は無い：回線の休みと再開は、回線の"
                              "状態の表が回線の欄に持ち、停止の記録を作らない＝DESIGN.md 17章。前の作りの記録の値）",
                              "自動では解かない（この原因で止める処理が無い）",
                              "端末・アプリの状態。管理者か契約者が解くと、回線の欄へ手の再開（E12）として渡す"),
    "ai_connection": CauseInfo("AIの不具合",
                               "AIの不具合（2026-10-30 深夜 管理者：「アンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、"
                               "AI の不具合として回復するまで一時停止」）：通話の中か通話後の確かめで、使うAI（会話AI・判定・別の耳）のどれかが止まった、"
                               "または会話AIの異常が続いた（つながらない・Google の側の誤り・設定の誤り・不明のエラー）",
                               "回復の確かめ（通話に使うAIに短く問い合わせ、全部が答えた）が、決めた回数続けて成功したら解く（設定の誤りは、"
                               "直すまで確かめも失敗する）",
                               "止まったAIの名前と時刻・数えたエラーの意味。管理者か契約者が解くと、続いたエラーは解いた時から数え直す（確かめで解けたら、"
                               "その確かめを始めた時から）。"
                               "どうしても要るときは、解かずに、緊急の道（OpenRouter を通して Google が預かる Claude）で再開できる（止まった"
                               "のが判定か別の耳のときだけ。会話AIが止まっていると話せないので出せない）"),
    "project_denied": CauseInfo("会話AIの利用拒否", "会話AIのプロジェクトの利用拒否・枠の超過（いまは、この原因で止める処理は無い："
                                "そのプロジェクトをしばらく休ませ、次のキーで続ける。2026-10-17 ユーザー）",
                                "自動では解かない（この原因で止める処理が無い）", "Google の管理画面の上限"),
    "forbidden_speech": CauseInfo("言ってはいけないこと", "言ってはいけないことを言った（2つの耳で確かめた。検証モードの業務だけ"
                                  "1件で止める。本番は止めない＝2026-10-22 ユーザー）",
                                  "直した新しい版が模擬通話に合格したら解く",
                                  "その通話の場面。管理者か契約者が解くと、その版をもう一度測り直す（段の件数を最初から数える）"),
    "quality_gate": CauseInfo("品質の基準", "品質の基準を外れた（2つの耳の食い違い・苦情・技術的な失敗・検証できなかった の割合。"
                              "検証モードだけ）",
                              "直した新しい版が模擬通話に合格したら解く",
                              "外れた指標。管理者か契約者が解くと、その版をもう一度測り直す（段の件数を最初から数える）"),
    "program_trouble": CauseInfo("こちらのプログラムの不具合",
                                 "違う行で、同じ理由のこちらのプログラムの不具合（固まった・AI の異常・会話を進められない・"
                                 "通話を進める部品が落ちた。「声が聞き取れない」は数えない＝2026-10-25 夜 案A）で通話を終えた行が、"
                                 "決めた数（設定 stop.trouble.stop_after_rows。初め3行）に"
                                 "なった＝すべての行にかかわる甚大な問題（2026-10-25 ユーザー：「3行同じ問題なら、甚大な問題と判断。」）。"
                                 "止まるのは新しい電話だけ（話している電話は切らない）",
                                 "直す担当（修正依頼）がその不具合を直したと記録し、そのあとプログラムが替わったら解く（状態の表の契約者の欄＝"
                                 "DESIGN.md 17章の C-5。止めた種類がすべて直るまで止めたまま）",
                                 "修正依頼が直ったか。管理者か契約者が解くと、同じ理由の行の数は解いた時から数え直す（直っていなければ、"
                                 "また止まる）"),
    "judge_unknown": CauseInfo("判定できない通話", "「判定できない」通話があった（会話の報告の書き漏れなど、こちらの誤り。検証モードの"
                               "業務だけ1件で止める。本番は止めない＝2026-10-22 ユーザー）",
                               "原因を直して、その場面を入れた模擬通話に合格したら解く（同じ版でもよい）",
                               "評価の記録の原因と通話。判定待ちの行は、原因が直れば自動で判定し直す"),
    "budget": CauseInfo("費用の上限", "費用の1日の上限を超えた（いまは、この原因で止める処理は無い：注意を知らせるだけ。"
                        "2026-10-16 ユーザー：「注意喚起のみ」）",
                        "自動では解かない（この原因で止める処理が無い）", "上限の設定"),
    "deficit": CauseInfo("赤字", "赤字（いまは、この原因で止める処理は無い：赤字は知らせるだけ。期限までに確認されなければ、"
                         "もう一度、契約者と管理者へ知らせる。2026-10-22 深夜 ユーザー：「止めない。知らせるだけでいい」）",
                         "自動では解かない（この原因で止める処理が無い）", "その依頼者の報酬の決まりと、今月の費用の内訳"),
}


def stop(tx: Tx, *, scope: Scope, cause: Cause, reason: str, evidence: str | None = None,
         tenant_id: UUID | None = None, client_id: UUID | None = None, business_id: UUID | None = None,
         line_id: UUID | None = None, admin: bool = False, notify: bool = True) -> dict[str, Any]:
    """止める。止めた記録と、取り消した予約の数を返す。admin＝管理者あての写しも知らせる（担当へ修正依頼を出した原因＝管理者は確かめ・承認・
    ユーザーが「管理者へ知らせる」と決めた停止。決めるのは呼ぶ側の1か所）。知らせは短く、範囲と原因の短い名前だけ（理由・根拠は
    `explain`＝画面の「止まった理由」で見る。2026-10-17 ユーザー：注意は短く、詳しくは i マークで）。notify=False＝知らせない（知らせを
    決めるのが呼ぶ側の表＝回線・行・契約者の状態の表を引く入口 core/health のとき。同じ出来事を二重に知らせない）。"""
    row = tx.one(
        "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, cause, actor,"
        " actor_kind, reason, evidence)"
        " VALUES (%s, %s, %s, %s, %s, 'stop', %s, salescall.cur_actor(), salescall.cur_actor_kind()::salescall.actor_kind,"
        " %s, %s) RETURNING event_id",
        (scope, tenant_id, client_id, business_id, line_id, cause, reason, evidence),
    )
    released = tx.one("SELECT salescall.release_claims_for_stop(%s) AS n", (row["event_id"],))["n"]
    if not notify:
        return {"event_id": row["event_id"], "released_claims": released}
    _notify.emit(tx, "stopped", key=f"stop:{row['event_id']}",
                 summary=f"発信を止めました（{SCOPE_NAMES[scope]}：{CAUSES[cause].label}）。",
                 refs={"stop_event_id": row["event_id"], "scope": scope, "cause": cause, "client_id": client_id,
                       "business_id": business_id, "line_id": line_id}, admin=admin)
    return {"event_id": row["event_id"], "released_claims": released}


def open_same(tx: Tx, *, scope: Scope, cause: Cause, tenant_id: UUID | None = None, client_id: UUID | None = None,
              business_id: UUID | None = None, line_id: UUID | None = None, passed_too: bool = True) -> int | None:
    """同じ原因・同じ範囲の、まだ解かれていない停止（あればその番号。1か所。止める側は、あれば重ねて止めない）。passed_too=False＝
    緊急の道で再開した停止は数えない（その停止では止まっていないので。core/watch.decide_stop が、緊急の道で補えない種類のときに使う）。"""
    row = tx.one(
        "SELECT s.event_id FROM salescall.open_stop_events s WHERE s.cause::text = %s AND s.scope = %s"
        " AND s.tenant_id IS NOT DISTINCT FROM %s AND s.client_id IS NOT DISTINCT FROM %s"
        " AND s.business_id IS NOT DISTINCT FROM %s AND s.line_id IS NOT DISTINCT FROM %s"
        " AND (%s OR NOT EXISTS (SELECT 1 FROM salescall.stop_emergency_passes p WHERE p.stop_event_id = s.event_id))"
        " ORDER BY s.event_id LIMIT 1",
        (cause, scope, tenant_id, client_id, business_id, line_id, passed_too))
    return None if row is None else row["event_id"]


def resume(tx: Tx, stop_event_id: int, *, reason: str, evidence: str | None = None, notify: bool = True,
           counted_from: Any = None) -> int:
    """その停止を解く（範囲は停止と同じ）。解いた記録の番号を返す。notify=False＝知らせない（`stop` と同じ）。counted_from＝確かめで
    解いたとき、その確かめを始めた時刻（core/watch.record_check だけが渡す。そこから数え直す＝データベースの salescall.errors_counted_since。
    渡さなければ解いた時刻から）。"""
    row = tx.one(
        "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, resolves_event_id,"
        " actor, actor_kind, reason, evidence, counted_from)"
        " SELECT scope, tenant_id, client_id, business_id, line_id, 'resume', event_id, salescall.cur_actor(),"
        "        salescall.cur_actor_kind()::salescall.actor_kind, %s, %s, %s"
        " FROM salescall.stop_events WHERE event_id = %s AND action = 'stop' RETURNING event_id, scope",
        (reason, evidence, counted_from, stop_event_id),
    )
    if row is None:
        raise LookupError(f"その停止は無い（または、取引の範囲の外）：{stop_event_id}")
    cause = tx.one("SELECT cause::text AS c FROM salescall.stop_events WHERE event_id = %s", (stop_event_id,))["c"]
    if notify:
        _notify.emit(tx, "resumed", key=f"resume:{row['event_id']}",
                     summary=f"止めていた発信を解きました（{SCOPE_NAMES[row['scope']]}：{CAUSES[cause].label}）。",
                     refs={"stop_event_id": stop_event_id, "resume_event_id": row["event_id"], "cause": cause})
    health.by_hand_stop(tx, stop_event_id, reason)   # 管理者か契約者が解いた（E12）なら、状態の表を引く入口へ（DESIGN.md 17章）
    return row["event_id"]


def open_stops(tx: Tx, *, tenant_id: UUID | None, client_id: UUID | None,
               business_id: UUID | None, line_id: UUID | None = None) -> list[dict[str, Any]]:
    """まだ解かれていない停止のうち、この範囲にかかるもの（空なら止まっていない）。line_id を渡すと、その回線の停止も含める。"""
    return tx.all(
        "SELECT event_id, scope, cause, reason, at FROM salescall.open_stops(%s, %s, %s, %s) ORDER BY event_id",
        (tenant_id, client_id, business_id, line_id),
    )


def blocking(tx: Tx, *, tenant_id: UUID | None, client_id: UUID | None, business_id: UUID | None,
             line_id: UUID | None = None, kind: str | None) -> list[dict[str, Any]]:
    """まだ解かれていない停止（`open_stops` と同じ範囲）のうち、その種類の電話を止めるもの（門が使う。決まりはデータベースの
    salescall.blocking_stops の1か所＝候補を出す処理・約束の一覧と同じ）。kind＝お客様との約束を果たす電話の種類
    （salescall.keeping_kind の答え。営業として新しくかける電話・番号の聞き直しは None＝どの停止でも止まる）。"""
    return tx.all(
        "SELECT event_id, scope::text AS scope, cause::text AS cause, reason, at"
        " FROM salescall.blocking_stops(%s, %s, %s, %s, %s) ORDER BY event_id",
        (tenant_id, client_id, business_id, line_id, kind),
    )


def lets_through(tx: Tx, cause: str) -> frozenset[str]:
    """その原因の停止でも通す、お客様との約束を果たす電話の種類（データベースの表 salescall.stop_lets_through の1か所）。"""
    return frozenset(r["kind"] for r in tx.all("SELECT kind FROM salescall.stop_lets_through WHERE cause::text = %s", (cause,)))


class EmergencyNotAllowed(Exception):
    """緊急の道での再開を出せない停止（理由は文で）。"""


def emergency_possible(tx: Tx, stop_event_id: int) -> str | None:
    """その停止で、緊急の道（OpenRouter を通して Google が預かる Claude）での再開を出せるか（1か所。画面がボタンを出すか・出す入口が
    読む）。出せれば None、出せなければ理由の文。出せるのは、緊急の道で再開できる停止（データベースの salescall.emergency_stop の1か所＝
    まだ解かれていない・システムが止めた・その契約者の AIの不具合の停止。守り・判定の道も同じ関数を読む）で、まだ出していないもの、かつ、
    緊急の道で補えない種類（errors.EMERGENCY_BLOCKERS＝会話AI・緊急の道そのもの）が止める数に届いていないとき（core/watch.ai_blocker の
    1か所＝止め直すかと同じ数え方。2026-10-30 深夜 管理者のご決定。CL-1 の連絡帳 4-62 の (c)：会話AIが止まっていると、再開しても話せない）。
    契約者の取引で読む。"""
    from salescall.core import watch   # watch は、ここ（停止の記録）を使う側でもあるので、使うときに読む
    s = tx.one(
        "SELECT salescall.emergency_stop(s.event_id) AS ok,"
        " EXISTS (SELECT 1 FROM salescall.stop_emergency_passes p WHERE p.stop_event_id = s.event_id) AS passed"
        " FROM salescall.open_stop_events s WHERE s.event_id = %s", (stop_event_id,))
    if s is None:
        return "まだ解かれていない、その停止はありません"
    if not s["ok"]:
        return "緊急の道で再開できるのは、システムが止めた、契約者の AIの不具合の停止だけです"
    if s["passed"]:
        return "もう緊急の道で再開しています"
    blocked = watch.ai_blocker(tx)
    if blocked is not None:
        return f"緊急の道では補えないAIが止まっています（{errors.LABELS[blocked]}）。回復を待ってください"
    return None


def emergency_pass(tx: Tx, stop_event_id: int, *, reason: str) -> dict[str, Any]:
    """その停止のあいだ、緊急の道で新しい電話をかけてよいことにする（契約者か管理者だけ＝データベースが守る。理由は必ず書く）。出したあとの
    電話は、判定を初めから緊急の道へ送る（予約の judge_route＝データベースの salescall.emergency_route の1か所）。止めた停止が解ければ
    （確かめが通った・解いた）一緒に終わる。緊急の道で補えないAIがあとで止まれば、また止まる（core/watch.decide_stop）。出した記録を返す。"""
    why = emergency_possible(tx, stop_event_id)
    if why is not None:
        raise EmergencyNotAllowed(why)
    row = tx.one(
        "INSERT INTO salescall.stop_emergency_passes (stop_event_id, tenant_id, reason, passed_by, actor_kind)"
        " SELECT %s, salescall.cur_tenant(), %s, salescall.cur_actor(), salescall.cur_actor_kind()::salescall.actor_kind"
        " RETURNING stop_event_id, reason, passed_by, actor_kind::text AS actor_kind, at", (stop_event_id, reason))
    _notify.emit(tx, "emergency_route", key=f"emergency_pass:{stop_event_id}",
                 summary="AIの不具合で止めていた新しい電話を、緊急の道（OpenRouter を通して Google が預かる Claude）で再開しました。",
                 refs={"stop_event_id": stop_event_id}, admin=True)
    return row


def explain(tx: Tx, stop_event_id: int, *, error_limit: int = 20) -> dict[str, Any]:
    """何で止まったか（画面と契約者の判断のため。1か所）。取引で見える停止だけ。中身：
    stop（範囲・原因・理由・根拠・誰が・いつ）・cause（意味・自動で解く決まり・管理者か契約者が解く前に確かめること）・who_can_resume・
    resolved（解かれていれば、誰が・いつ・なぜ）・errors（その停止に数えたエラーと、一覧で引いた意味と出どころ）・
    checks（解く確かめの記録）・quality（品質の評価の指標）・held_results（品質で止めた版の、まだ確定していない結果＝検証モードで
    契約者の確かめを待つもの。確定するなら approval.approve_result・直すなら approval.correct_result。依頼者へ渡すことではない）。"""
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
    if kinds and s["scope"] in ("system", "tenant", "line"):
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
    # AIの不具合の停止：止まったAIの名前と時刻（CL-3 の画面）と、緊急の道での再開（出せるか・出した人と理由）
    troubles, emergency = [], None
    if s["cause"] == "ai_connection" and s["scope"] == "tenant":
        troubles = ai_troubles.for_stop(tx, stop_event_id, error_limit)
        emergency = {"impossible_because": None if resolved is not None else emergency_possible(tx, stop_event_id),
                     "pass": tx.one("SELECT reason, passed_by, actor_kind::text AS actor_kind, at"
                                    " FROM salescall.stop_emergency_passes WHERE stop_event_id = %s", (stop_event_id,))}
    return {"stop": s, "ai_troubles": troubles, "emergency": emergency, "cause": {"label": info.label, "meaning": info.meaning, "auto_resume": info.auto_resume,
                                 "before_resume": info.before_resume,
                                 "lets_through": sorted(lets_through(tx, s["cause"]))},   # 止めているあいだも通す電話
            "who_can_resume": "管理者" if s["scope"] == "system" else "その契約者・管理者",
            "resolved": resolved, "errors": found, "checks": checks, "quality": quality, "held_results": held}
