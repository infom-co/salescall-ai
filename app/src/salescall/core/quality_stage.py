"""業務の版の段（Q22「この業務（版）は、いまどの段か・次の段へ進めるか」）。持ち主 CL-2。DESIGN.md 10-1。

**段を決めるのは、この部品の1か所**（指標を数える `metrics`・段を評価して進める／止める `evaluate`・模擬通話の合否 `record_mock`・
版の始め方 `prepare`）。門は `allowance` を読むだけ。画面は記録（段・評価・模擬通話）を読んでそのまま出す（数え直さない）。

- 版の始め方（`prepare`）：依頼者で模擬通話に合格したことが無い（依頼者ごとに最初のみ。2026-10-13 ユーザー）か、その業務が品質で
  止まっている ⇒ 模擬通話の合格を待つ。それ以外 ⇒ 最初の段を開く。止まった版そのものでは再開しない（9-1）。
- 模擬通話（`record_mock`）：流すのは CL-1。本数・正解と一致した値と判定の割合・言ってはいけないことの数を、設定の基準と比べて
  合否を決めて記録する。合格なら、その業務の品質の停止を解き（止まった版とは違う版のときだけ）、最初の段を開く。
- 段の評価（`evaluate`。締めの処理が通話ごとに呼ぶ）。**段・割合の基準・1件で止めるのは、検証モードの業務だけ**。本番は評価しない
  （**止めない**。言ってはいけないこと・判定できないの1件ごとの手当て＝契約者と管理者への知らせ・直す担当への修正依頼は、締めの処理が
  通話ごとに出している＝同じ不具合は1つ。2026-10-22 ユーザー：「前にも言ったはずで本番は止めない（担当への修正依頼と管理者への
  知らせ）ただし、大問題は止める」。大問題＝このまま進めるとお客様に害が出るとき＝緊急停止・回線や会話AIの異常の停止。原則6。
  ⛔ 前は、件数になったら業務を止めていた＝2026-10-16 の決定の読み違い。⛔ 見直し23 の (3)：そのあとも、その撤回された決定の名残
  ＝「その日のうちに同じ原因が設定の件数（quality.production_same_cause_per_day。初め3）になったら管理者へ知らせる」（知らせの種類
  quality_alert）を残していた。2026-10-22 の決定で通話ごとに知らせと修正依頼が出ているので外した＝0148）。検証モードの業務：
  - 数の基準は、毎回見る。超えたら、その場で業務を止める：言ってはいけないこと（0件まで。原因 forbidden_speech）・
    判定できない（**1件で止める**。原因 judge_unknown。止める理由に、判定できない原因の名前と意味を入れる。2026-10-16 ユーザー決定：
    (a)(c)(d)）。管理者にも知らせるかは `_stop` の1か所で決める（本番の停止と、プログラムの誤りの判定できない原因
    ＝`judge.PROGRAM_CAUSES`。直す担当へは締めの処理が修正依頼を出し、管理者は確かめ・承認）。
  - 割合の基準（2つの耳の食い違い・苦情・技術的な失敗）は、段の件数に達し、その分がすべて締め終わったときに見る
    （小さな数の割合はぶれるため）。内側なら次の段へ（最後の段のあとは全部）、外れたら業務を止める（原因 quality_gate）。
  - 評価のたびに、指標ごとの値・基準・内側か外か・結果を記録する（CL-3 の 4-1e の2）。
- 記録が無い版は、まだかけられない（模擬通話の合格・最初の段を開く前。判断できなければ通さない）。
- **判定できないで止めた業務の再開**（2026-10-16 ユーザー決定）：原因を直して、**その場面（止めた原因の通話）を入れた模擬通話**に
  合格したら、自動で再開する。ほかの原因（言ってはいけないこと・品質の基準）で止めた業務でも、止めた版の測りの中に判定できなかった
  通話があれば、その場面を入れることを求める（すでに止まっているあいだの判定できないを、見落として再開しない）。原因は業務設定とは限らない（会話の処理・判定の処理）ので、同じ版でも再開する。どの場面を入れたかは
  模擬通話の記録（scenes）で確かめる（入れていなければ再開しない）。
- **管理者か契約者が品質の停止を解いたら、その版を測り直す**（2026-10-15 ユーザー：「人の判断で再開できる仕組みは？」）。段の件数と指標は、
  その版の通話のうち「人が解いた時刻に、まだ締めていなかった通話」から数える（数える通話の範囲は `_WINDOW` の1か所）。
  ⚠️ いままでは版の通話をすべて数えたので、管理者か契約者が解いても、前の数（言ってはいけないこと 1件など）で、次の通話の締めでまた止まった。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import approval, attempts, notify, settings, stops
from salescall.core.judge import PROGRAM_CAUSES, UNKNOWN_CAUSES
from salescall.core.access import Tx

STOP_CAUSES = ("quality_gate", "forbidden_speech", "judge_unknown")    # 品質で止める原因（模擬通話の合格で解く）
# 指標：名前 → （数え方, 基準の設定）。数え方 count＝件数の上限／ratio＝割合の上限
METRICS: dict[str, tuple[str, str]] = {
    "unknown_judgments": ("count", "quality.max_unknown_judgments"),
    "forbidden_speech": ("count", "quality.max_forbidden_speech"),
    "ear_disagreement": ("ratio", "quality.max_ear_disagreement"),
    "complaint": ("ratio", "quality.max_complaint"),
    "technical_error": ("ratio", "quality.max_technical_error"),
}


# 段と指標に数える通話（1か所）：その版の通話のうち、その版の品質の停止を管理者か契約者が最後に解いた時刻に、まだ締めていなかったもの。
# 決まった文面だけの電話（いまはお詫びの電話）は数えない（業務の話し方を使わない。決めるのは salescall.fixed_text_kind の1か所＝
# 門・候補を出す処理と同じ。⛔ 見直し17b：前は purpose を直書きしていた）。
# 引数は (版, 版)。解いたことが無ければ、その版の通話すべて。
_WINDOW = (
    "SELECT a.* FROM salescall.attempts a WHERE a.version_id = %s"
    " AND NOT salescall.fixed_text_kind(salescall.keeping_kind(a.purpose, a.promised)) AND NOT EXISTS ("
    " SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = a.attempt_id AND r.kind = 'judged'"
    " AND r.decided_at <= (SELECT coalesce(max(z.at), '-infinity') FROM salescall.stop_events z"
    "                      JOIN salescall.quality_evaluations q ON q.stop_event_id = z.resolves_event_id"
    "                      WHERE q.version_id = %s))")


def current(tx: Tx, version_id: UUID) -> dict[str, Any] | None:
    """いまの段（いちばん新しい記録）。無ければ None。"""
    return tx.one("SELECT stage_id, stage_no, cap, evidence, opened_at FROM salescall.version_stages"
                  " WHERE version_id = %s ORDER BY stage_id DESC LIMIT 1", (version_id,))


def used(tx: Tx, version_id: UUID) -> int:
    """いまの段の件数に数える通話：その版で、お客様が出た（応答まで進んだ）試行と、生きている進行中の試行（出るかもしれない）。"""
    return tx.one(
        f"SELECT count(*) AS n FROM ({_WINDOW}) a"
        " WHERE salescall.is_live(a.state, a.claim_expires_at) OR EXISTS (SELECT 1 FROM salescall.attempt_state_history h"
        "      WHERE h.attempt_id = a.attempt_id AND h.to_state = 'answered')", (version_id, version_id))["n"]


def _verification(tx: Tx, version_id: UUID) -> bool:
    """その版の業務が検証モードか（読むのは承認の部分の1か所。I-13）。段階的な立ち上げ・1件で止める品質の門は、検証モードだけ。"""
    return approval.human_for_business(tx, _version(tx, version_id)["business_id"])


def allowance(tx: Tx, version_id: UUID) -> tuple[bool, str | None]:
    """その版で、いま新しく予約してよいか（よい, 理由の名前）。理由：version_not_ready・stage_full。"""
    stage = current(tx, version_id)
    if stage is None:
        return False, "version_not_ready"
    if stage["cap"] is not None and _verification(tx, version_id) and used(tx, version_id) >= stage["cap"]:
        return False, "stage_full"
    return True, None


def open_stage(tx: Tx, version_id: UUID, stage_no: int, cap: int | None, *, evidence: str) -> int:
    """段を開く（記録を足す）。cap は会話に至ってよい通話の数（None＝全部）。"""
    return tx.one(
        "INSERT INTO salescall.version_stages (tenant_id, client_id, version_id, stage_no, cap, evidence, opened_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING stage_id", (version_id, stage_no, cap, evidence))["stage_id"]


@dataclass(frozen=True)
class MockResult:
    """模擬通話の結果（流すのは CL-1。お客様役が正解を知っている台本で流し、正解と比べた数）。"""
    calls: int
    value_hits: int
    value_total: int
    judgment_hits: int          # 業務の判定が正解と一致した本数（1本に判定は1つ ⇒ judgment_total ＝ calls）
    judgment_total: int
    forbidden_speech: int       # 言ってはいけないことの数（2つの耳で確かめたもの）
    evidence: str               # 台本の版・流した記録の場所など（お客様の個人情報は無い：模擬通話）
    scenes: tuple[UUID, ...] = ()   # 台本に入れた、実際の通話の場面（試行）。判定できないで止めた業務の再開に要る


def _version(tx: Tx, version_id: UUID) -> dict[str, Any]:
    row = tx.one("SELECT tenant_id, client_id, business_id FROM salescall.business_versions WHERE version_id = %s",
                 (version_id,))
    if row is None:
        raise LookupError(f"その版は無い（または、取引の範囲の外）：{version_id}")
    return row


def _get(tx: Tx, v: dict[str, Any], key: str) -> Any:
    return settings.resolve(tx, key, tenant_id=v["tenant_id"], client_id=v["client_id"], business_id=v["business_id"])


def _quality_stop(tx: Tx, business_id: UUID) -> dict[str, Any] | None:
    """その業務の、まだ解かれていない品質の停止と、止めた版（無ければ None）。"""
    return tx.one(
        "SELECT s.event_id, s.cause::text AS cause, (SELECT q.version_id FROM salescall.quality_evaluations q"
        "  WHERE q.stop_event_id = s.event_id) AS version_id FROM salescall.open_stop_events s"
        " WHERE s.scope = 'business' AND s.business_id = %s AND s.cause::text = ANY(%s)"
        " ORDER BY s.event_id LIMIT 1", (business_id, list(STOP_CAUSES)))


def prepare(tx: Tx, version_id: UUID) -> str:
    """版を始める（実施回に版を割り当てたときに呼ぶ）。答え：already（もう段がある）／stopped_version（品質で止まった版そのもの。
    再開しない）／mock_needed（模擬通話の合格を待つ）／opened（最初の段を開いた）。"""
    if current(tx, version_id) is not None:
        return "already"
    v = _version(tx, version_id)
    stop = _quality_stop(tx, v["business_id"])
    if stop is not None and stop["cause"] != "judge_unknown" and stop["version_id"] == version_id:
        return "stopped_version"
    passed_before = tx.one("SELECT 1 FROM salescall.mock_runs WHERE passed LIMIT 1") is not None   # この依頼者（取引の範囲）
    if stop is not None or not passed_before:
        return "mock_needed"
    sizes = list(_get(tx, v, "quality.stage.sizes"))
    open_stage(tx, version_id, 1, sizes[0], evidence="この依頼者は模擬通話に合格済み（依頼者ごとに最初のみ）")
    return "opened"


def record_mock(tx: Tx, version_id: UUID, r: MockResult) -> dict[str, Any]:
    """模擬通話の結果の合否を決めて記録する。合格なら、品質の停止を解き（止まった版でなければ）、最初の段を開く。"""
    v = _version(tx, version_id)
    need = int(_get(tx, v, "quality.mock.calls"))
    min_value, min_judgment = float(_get(tx, v, "quality.mock.min_value_match")), float(_get(tx, v, "quality.mock.min_judgment_match"))
    if r.judgment_total != r.calls:
        raise ValueError("判定は1本に1つです（judgment_total は calls と同じ）")
    passed = (r.calls >= need and r.value_hits / r.value_total >= min_value
              and r.judgment_hits / r.judgment_total >= min_judgment and r.forbidden_speech == 0)
    mock_id = tx.one(
        "INSERT INTO salescall.mock_runs (tenant_id, client_id, version_id, calls, value_hits, value_total, judgment_hits,"
        " judgment_total, forbidden_speech, passed, thresholds, evidence, recorded_by, scenes)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        " salescall.cur_actor(), %s) RETURNING mock_id",
        (version_id, r.calls, r.value_hits, r.value_total, r.judgment_hits, r.judgment_total, r.forbidden_speech, passed,
         json.dumps({"calls": need, "min_value_match": min_value, "min_judgment_match": min_judgment}), r.evidence,
         [UUID(str(x)) for x in r.scenes]))["mock_id"]
    outcome = "failed"
    if passed:
        stop = _quality_stop(tx, v["business_id"])
        if stop is not None and not set(_incidents(tx, stop["event_id"])) <= {str(x) for x in r.scenes}:
            # 判定できなかった通話の場面を入れていない模擬通話では、直ったと言えない。止めた原因が判定できないでなくても同じ
            # （2026-10-16 見直し：品質の停止がもうあるあいだに締めた通話が判定できなかったとき、前は場面を求めずに再開していた）
            outcome = "scenes_missing"
        elif stop is not None and stop["cause"] != "judge_unknown" and stop["version_id"] == version_id:
            outcome = "stopped_version"          # 止まった版そのもの：合格でも再開しない（新しい版で）
        else:
            if stop is not None:
                stops.resume(tx, stop["event_id"], reason=f"新しい版が模擬通話に合格（模擬通話 {mock_id}）",
                             evidence=f"mock_id={mock_id}")
            if current(tx, version_id) is None:
                sizes = list(_get(tx, v, "quality.stage.sizes"))
                open_stage(tx, version_id, 1, sizes[0], evidence=f"模擬通話に合格（模擬通話 {mock_id}）")
            outcome = "opened"
    return {"mock_id": mock_id, "passed": passed, "outcome": outcome}


def _incidents(tx: Tx, stop_event_id: int) -> list[str]:
    """品質の停止の、模擬通話に入れる場面：判定できなかった通話すべて（原因を問わない）。数える範囲は、止めたときと同じ決まり＝止めた版の
    いまの測り（`_WINDOW`。品質で止めるのは検証モードの業務だけ）。⚠️ 止めたときの記録だけを見ると、止めたあとに締めた別の通話（回線が
    複数のとき）の判定できないを、入れないまま再開してしまう。⛔ 見直し23 の (3)：本番で止めた停止（2026-10-16 の撤回された決定）の
    場面を、その日の数え方で読む道が残っていた（本番は止めないので、もう起きない）。"""
    row = tx.one(
        "SELECT q.version_id FROM salescall.quality_evaluations q WHERE q.stop_event_id = %s", (stop_event_id,))
    if row is None:
        return []
    return metrics(tx, row["version_id"])["unknown_judgments"]["all_attempts"]


def metrics(tx: Tx, version_id: UUID) -> dict[str, dict[str, Any]]:
    """その版の指標（どれも記録から数える。数え方はここだけ）。値：hits・total・value・limit・inside。"""
    v = _version(tx, version_id)
    row = tx.one(
        f"WITH a AS ({_WINDOW}),"
        " judged AS (SELECT r.* FROM salescall.task_results r JOIN a ON a.attempt_id = r.attempt_id WHERE r.kind = 'judged'),"
        " talked AS (SELECT a.attempt_id FROM a WHERE salescall.reached_answer(a.attempt_id)"
        "            AND EXISTS (SELECT 1 FROM judged j WHERE j.attempt_id = a.attempt_id))"
        " SELECT (SELECT coalesce(sum(v.forbidden_speech), 0) FROM salescall.verification_summaries v JOIN a USING (attempt_id))"
        "          AS forbidden,"
        "        (SELECT coalesce(sum(v.items_disagreed), 0) FROM salescall.verification_summaries v JOIN a USING (attempt_id))"
        "          AS ear_hits,"
        "        (SELECT coalesce(sum(v.items_checked), 0) FROM salescall.verification_summaries v JOIN a USING (attempt_id))"
        "          AS ear_total,"
        "        (SELECT count(*) FROM judged j JOIN talked t ON t.attempt_id = j.attempt_id WHERE j.complaint) AS complaint,"
        "        (SELECT count(*) FROM talked) AS talked,"
        "        (SELECT count(*) FROM a WHERE state = 'technical_error') AS technical,"
        f"        (SELECT count(*) FROM a WHERE {attempts.DIALED} AND NOT salescall.is_active(state))"
        "          AS dialed", (version_id, version_id))
    by_cause = {r["c"]: [str(x) for x in r["a"]] for r in tx.all(
        f"SELECT r.detail ->> 'unknown_cause' AS c, array_agg(r.attempt_id ORDER BY r.attempt_id) AS a"
        f" FROM salescall.task_results r JOIN ({_WINDOW}) a ON a.attempt_id = r.attempt_id"
        " WHERE r.kind = 'judged' AND r.business_state = 'unknown' GROUP BY 1", (version_id, version_id))}
    worst = max(by_cause, key=lambda c: (len(by_cause[c]), c or ""), default=None)
    raw = {"unknown_judgments": (0 if worst is None else len(by_cause[worst]), None), "forbidden_speech": (row["forbidden"], None),
           "ear_disagreement": (row["ear_hits"], row["ear_total"]), "complaint": (row["complaint"], row["talked"]),
           "technical_error": (row["technical"], row["dialed"])}
    out = {}
    for name, (how, key) in METRICS.items():
        hits, total = raw[name]
        limit = _get(tx, v, key)
        value = hits if how == "count" else (None if not total else hits / total)
        inside = value is None or value <= float(limit)
        out[name] = {"kind": how, "hits": int(hits), "total": None if total is None else int(total), "value": value,
                     "limit": limit, "inside": inside}
    # 判定できない：同じ原因の件数（いちばん多い原因で比べる）。原因ごとの件数と、いちばん多い原因の通話を残す（再開の確かめに使う）
    out["unknown_judgments"].update(causes={c or "": len(a) for c, a in by_cause.items()}, cause=worst,
                                    attempts=[] if worst is None else by_cause[worst],
                                    all_attempts=sorted(a for x in by_cause.values() for a in x))
    return out


def evaluate(tx: Tx, version_id: UUID) -> dict[str, Any] | None:
    """段を評価して、続ける／次の段へ進める／業務を止める（締めの処理が通話ごとに呼ぶ。検証モードの業務だけ）。記録した評価を返す
    （段が無い・もう品質で止まっている・本番の業務なら None）。"""
    stage = current(tx, version_id)
    if stage is None:
        return None
    v = _version(tx, version_id)
    if _quality_stop(tx, v["business_id"]) is not None:
        return None
    if not _verification(tx, version_id):   # 本番：段も基準も見ない（1件ごとの知らせと修正依頼は、締めの処理が通話ごとに出す）
        return None
    m = metrics(tx, version_id)
    n_used = used(tx, version_id)
    outcome, stop_id, cause = "continue", None, None
    if not m["forbidden_speech"]["inside"]:
        cause = "forbidden_speech"
    elif not m["unknown_judgments"]["inside"]:
        cause = "judge_unknown"
    else:
        # 締めていない通話が残れば、段を進めない（結果が出そろってから比べる）。ただし、お客様のところで鳴らずに終わった電話（会話の品質に
        # かかわらない。決まりは attempts.rang の1か所）は待たない：番号のせいか回線のせいかが決まるまで締めない電話（settle.settle）を
        # 待つと、段が進まず、残りの行に永久にかけなかった（見直し22 の 2。場面 n11）
        unsettled = tx.one(
            f"SELECT 1 FROM ({_WINDOW}) a WHERE {attempts.dialed('a')}"
            " AND NOT salescall.is_settled(a.attempt_id)"
            f" AND (salescall.is_active(a.state) OR {attempts.rang('a')}) LIMIT 1", (version_id, version_id)) is not None
        if stage["cap"] is not None and n_used >= stage["cap"] and not unsettled:
            if all(x["inside"] for x in m.values()):
                sizes = list(_get(tx, v, "quality.stage.sizes"))
                nxt = stage["stage_no"] + 1
                cap = sizes[nxt - 1] if nxt <= len(sizes) else None
                sid = open_stage(tx, version_id, nxt, cap, evidence=f"段 {stage['stage_no']} の指標がすべて基準の内側")
                notify.emit(tx, "quality_stage", key=f"stage:{sid}",
                            summary=f"品質の段が進みました（段 {stage['stage_no']} → {nxt}。件数：{cap or '全部'}）",
                            refs={"version_id": version_id, "stage_id": sid})
                outcome = "advance"
            else:
                cause = "quality_gate"
    if cause is not None:
        bad = ", ".join(f"{k}={x['hits']}/{x['total']}" if x["total"] is not None else f"{k}={x['hits']}"
                        for k, x in m.items() if not x["inside"])
        reason = f"品質の基準を外れた（{bad}）"
        if cause == "judge_unknown":   # 何が原因か（決まった名前と意味・直す担当）を、止める理由に入れて知らせる（(c)）
            unk = m["unknown_judgments"]
            reason = (f"判定できない通話がありました（原因：{unk['cause']}＝{UNKNOWN_CAUSES.get(unk['cause'], '不明')}。"
                      f"通話 {len(unk['attempts'])} 件）。その行は判定待ちで残しています。原因を直し、その場面を入れた模擬通話に"
                      "合格したら再開します")
        stop_id = _stop(tx, v, version_id, cause, reason, unknown_cause=m["unknown_judgments"]["cause"])
        outcome = "stop"
    row = tx.one(
        "INSERT INTO salescall.quality_evaluations (tenant_id, client_id, version_id, stage_no, cap, used, metrics, outcome,"
        " stop_event_id) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING eval_id",
        (version_id, stage["stage_no"], stage["cap"], n_used, json.dumps(m, default=str), outcome, stop_id))
    return {"eval_id": row["eval_id"], "outcome": outcome, "metrics": m, "stop_event_id": stop_id}


def _stop(tx: Tx, v: dict[str, Any], version_id: UUID, cause: str, reason: str, *, unknown_cause: str | None) -> int:
    """品質で業務を止める（検証モードの業務だけ。ここだけ）。管理者にも知らせるか：判定できない原因がプログラムの誤り
    （`judge.PROGRAM_CAUSES`。直す担当へは締めの処理が修正依頼を出す。管理者は確かめ・承認）のとき。"""
    admin = cause == "judge_unknown" and unknown_cause in PROGRAM_CAUSES
    return stops.stop(tx, scope="business", cause=cause, reason=reason, evidence=f"version_id={version_id}",
                      tenant_id=v["tenant_id"], client_id=v["client_id"], business_id=v["business_id"],
                      admin=admin)["event_id"]

