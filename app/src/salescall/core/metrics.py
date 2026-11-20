"""通話の指標を数える1か所（計画書 9-8「記録・比較する指標」。CL-3 の連絡帳 4-1c の 57。持ち主 CL-2）。

- **数え方はここだけ**（`counts`）：通話の範囲（SQL）を受けて、その範囲の通話を数える。品質の段（core/quality_stage.metrics＝検証モードの止める・進める）も、
  画面の業務ごと・声の型ごとの指標（`business`）も、これを読む（⛔ 画面や段ごとに数え方を持つと、判定・品質の段の数え方と食い違う＝CL-3 の 57）。
- 何を「話せた通話」とするか・かけた通話か・こちらの不具合か・別の耳の食い違い・言ってはいけないこと・苦情は、どれもデータベースと記録の1か所を読む
  （salescall.reached_answer・attempts.DIALED・判定の記録 task_results・通話後の検証のまとめ verification_summaries・利用量の台帳 usage_entries）。
- 必須の質問の「取れた」「-」「✕」の数は `required_items`（依頼者へ渡す結果と同じ答え・判定と同じ対象外）。
- ⏳ 記録が無いので数えないもの：AI の言葉の抜け・声の途切れ・黙り（通話後の検証の結果に足してもらう＝CL-1 への頼み。CL-2 の連絡帳 4-447）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from salescall.core import attempts
from salescall.core.access import Tx

# 業務・期間の通話の範囲（`business`）：その業務の通話のうち、予約した時刻が [since, until) のもの。決まった文面だけの電話（お詫びの電話）は数えない（業務の話し方を
# 使わない＝品質の段と同じ決まり。決めるのは salescall.fixed_text_kind の1か所）。声の型を渡せば、その声の型の通話だけ。引数は (業務, since, until, 声の型, 声の型)
_BUSINESS_WINDOW = (
    "SELECT a.* FROM salescall.attempts a WHERE salescall.attempt_business(a.attempt_id) = %s AND a.claimed_at >= %s AND a.claimed_at < %s"
    " AND NOT salescall.fixed_text_kind(salescall.keeping_kind(a.purpose, a.promised))"
    " AND (%s::text IS NULL OR a.voice_type = %s::text)")


# 範囲の通話のうち、行ごとのいちばん新しい判定（業務完了率・必須の質問の数の1か所）。a＝範囲の通話・judged＝その判定
_LATEST = (" latest AS (SELECT DISTINCT ON (j.task_id) j.task_id, j.attempt_id, j.business_state, j.detail FROM judged j"
           "            ORDER BY j.task_id, (SELECT x.claimed_at FROM a x WHERE x.attempt_id = j.attempt_id) DESC, j.version_no DESC)")
_JUDGED = " judged AS (SELECT r.* FROM salescall.task_results r JOIN a ON a.attempt_id = r.attempt_id WHERE r.kind = 'judged'),"


def counts(tx: Tx, window: str, params: tuple[Any, ...]) -> dict[str, Any]:
    """その通話の範囲（window＝通話の行を返す SQL・params＝その引数）の数（1か所）。
    - dialed＝お客様のところまでかけた通話（attempts.DIALED・終わったもの）／answered＝電話に出た通話（留守番電話・自動音声・FAX も入る＝salescall.reached_answer。判定のあるもの）
    - talked＝お客様（人）と話した通話（受付の方も入る。締めが写した attempts.talked＝会話の報告の人の証の表。0270）／reached＝目的のお客様と話せた通話（attempts.reached_target）
      （✅ 2026-11-20 管理者「「お客様と話した」と「目的のお客様と話せた」に加え、「留守番電話・自動音声」などを加えたトータル通話数もダッシュボードで出す」）
    - technical＝こちらの不具合で終えた通話（technical_error）／complaint＝話せた通話のうち、判定が苦情としたもの
    - ear_hits・ear_total＝通話後の検証で別の耳と食い違った項目・確かめた項目／forbidden＝言ってはいけないことを言った数
    - rows＝範囲に通話のある行／completed＝そのうち、行の範囲の中のいちばん新しい判定が「完了」の行
    - cut＝話せた通話のうち、記録した切れ方が「断った」「途中で切れた」（決まっていない切れ方は数えない）／talk_seconds＝話せた通話の、応答してから終わるまでの秒の合計（応答した時刻は状態の履歴 attempt_state_history）
    - redials＝かけた通話のうち、その行の2本目からのもの／yen・unpriced＝範囲の通話の利用量の金額の合計・単価の無い記録の数
    - voice_counted＝会話AIの声の数のある通話（通話後の検証が数えたもの）／voice_（種類）_checked・voice_（種類）_found＝その種類の確かめたターン・見つかったターンの合計
      （種類は attempts.VOICE_KINDS。CL-1 の連絡帳 4-221）"""
    row = tx.one(
        f"WITH a AS ({window}),{_JUDGED}"
        " answered AS (SELECT a.attempt_id, a.ended_at, a.disconnect, a.talked, a.reached_target, (SELECT min(h.at) FROM salescall.attempt_state_history h"
        "                   WHERE h.attempt_id = a.attempt_id AND h.to_state = 'answered') AS answered_at FROM a WHERE salescall.reached_answer(a.attempt_id)"
        "            AND EXISTS (SELECT 1 FROM judged j WHERE j.attempt_id = a.attempt_id)),"
        " talked AS (SELECT * FROM answered WHERE talked),"
        f"{_LATEST}"
        " SELECT (SELECT coalesce(sum(v.forbidden_speech), 0) FROM salescall.verification_summaries v JOIN a USING (attempt_id)) AS forbidden,"
        "        (SELECT coalesce(sum(v.items_disagreed), 0) FROM salescall.verification_summaries v JOIN a USING (attempt_id)) AS ear_hits,"
        "        (SELECT coalesce(sum(v.items_checked), 0) FROM salescall.verification_summaries v JOIN a USING (attempt_id)) AS ear_total,"
        "        (SELECT count(*) FROM judged j JOIN talked t ON t.attempt_id = j.attempt_id WHERE j.complaint) AS complaint,"
        "        (SELECT count(*) FROM answered) AS answered,"
        "        (SELECT count(*) FROM talked) AS talked,"
        "        (SELECT count(*) FROM answered WHERE reached_target) AS reached,"
        "        (SELECT count(*) FROM a WHERE state = 'technical_error') AS technical,"
        f"        (SELECT count(*) FROM a WHERE {attempts.DIALED} AND NOT salescall.is_active(state)) AS dialed,"
        "        (SELECT count(DISTINCT task_id) FROM a) AS rows,"
        "        (SELECT count(*) FROM latest WHERE business_state = 'completed') AS completed,"
        "        (SELECT count(*) FROM talked t WHERE t.disconnect IN ('refused', 'interrupted')) AS cut,"
        "        (SELECT coalesce(sum(extract(epoch FROM t.ended_at - t.answered_at)), 0) FROM talked t WHERE t.answered_at IS NOT NULL) AS talk_seconds,"
        f"        (SELECT count(*) FROM a WHERE {attempts.DIALED} AND NOT salescall.is_active(state) AND EXISTS ("
        f"           SELECT 1 FROM salescall.attempts b WHERE b.task_id = a.task_id AND b.claimed_at < a.claimed_at AND {attempts.dialed('b')})) AS redials,"
        "        (SELECT coalesce(sum(u.amount_yen), 0) FROM salescall.usage_entries u JOIN a USING (attempt_id)) AS yen,"
        "        (SELECT count(*) FROM salescall.usage_entries u JOIN a USING (attempt_id) WHERE u.amount_yen IS NULL) AS unpriced,"
        "        (SELECT count(DISTINCT x.attempt_id) FROM salescall.verification_voice x JOIN a USING (attempt_id)) AS voice_counted"
        + "".join(f", (SELECT coalesce(sum(x.{n}), 0) FROM salescall.verification_voice x JOIN a USING (attempt_id) WHERE x.kind = '{k}') AS voice_{k}_{n}"
                  for k in attempts.VOICE_KINDS for n in ("checked", "found")),
        params)
    return dict(row)


# 日本時間のある日の通話（`day`）：取引の依頼者の通話のうち、予約した時刻がその日のもの（お詫びの電話も入れる＝その日にかけた電話すべて）。業務を渡せば、その業務だけ。
# 引数は (日, 日, 業務, 業務)
_DAY_WINDOW = (
    "SELECT a.* FROM salescall.attempts a WHERE a.claimed_at >= salescall.jst_day_start(%s::date)"
    " AND a.claimed_at < salescall.jst_day_start(%s::date + 1) AND (%s::uuid IS NULL OR salescall.attempt_business(a.attempt_id) = %s::uuid)")


def day(tx: Tx, on: date, *, business_id: UUID | None = None) -> dict[str, int]:
    """ダッシュボードの「今日」の数（日本時間のその日。取引の依頼者の電話。業務で絞れる。CL-3 の 41）：かけた dialed・電話に出た answered（留守番電話・自動音声・FAX も入る
    ＝トータル通話数）・お客様と話した talked・目的のお客様と話せた reached。✅ 2026-11-20 管理者「「お客様と話した」と「目的のお客様と話せた」に加え、「留守番電話・
    自動音声」などを加えたトータル通話数もダッシュボードで出す」。数え方は `counts` の1か所（品質の段・業務の指標と同じ）。"""
    c = counts(tx, _DAY_WINDOW, (on, on, business_id, business_id))
    return {k: c[k] for k in ("dialed", "answered", "talked", "reached")}


def required_items(tx: Tx, business_id: UUID, *, since: datetime, until: datetime, voice_type: str | None = None) -> dict[str, dict[str, int]]:
    """業務（と声の型）・期間の、必須の質問ごとの「取れた」「-」「✕」の数（1か所。計画書 5-2・CL-3 の 42）：範囲（`_BUSINESS_WINDOW`）に通話のある行ごとに、行の
    いちばん新しい判定（`_LATEST`＝業務完了率と同じ）の、判定に使った答え（conversation_io.answers＝依頼者へ渡す結果と同じ1か所）を、印の分け方（judge.item_mark）で数える。
    判定が伺っていない（対象外）とした質問（判定の中身 not_asked）は not_asked に数える。{質問: {taken, dont_know, refused, open, not_asked}}（必須の質問はその判定の版の決まり）。"""
    from salescall.core import business_rules, conversation_io, judge   # 会話の入口・判定は業務設定（品質の段）を読むので、ここで読む（読み合いにしない）
    rows = tx.all(f"WITH a AS ({_BUSINESS_WINDOW}),{_JUDGED}{_LATEST}"
                  " SELECT l.task_id, l.attempt_id, l.detail, x.version_id FROM latest l JOIN a x ON x.attempt_id = l.attempt_id",
                  (business_id, since, until, voice_type, voice_type))
    required: dict[UUID, tuple[str, ...]] = {}
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["version_id"] not in required:
            required[r["version_id"]] = business_rules.load(tx, r["version_id"]).required
        items = conversation_io.answers(tx, r["task_id"], r["attempt_id"])
        skipped = set((r["detail"] or {}).get("not_asked") or ())
        for q in required[r["version_id"]]:
            mark = "not_asked" if q in skipped else judge.item_mark((items.get(q) or {}).get("state"))
            tally = out.setdefault(q, dict.fromkeys((*judge.ITEM_MARKS, "not_asked"), 0))
            tally[mark] += 1
    return out


def _rate(hits: Any, total: Any) -> dict[str, Any]:
    return {"hits": hits, "total": total, "value": None if not total else hits / total}


def business(tx: Tx, business_id: UUID, *, since: datetime, until: datetime, voice_type: str | None = None) -> dict[str, Any]:
    """業務（と声の型）・期間の指標（画面が読む1か所。CL-3 の 57）：予約した時刻が [since, until) の、その業務の通話（`_BUSINESS_WINDOW`）。
    率は {hits, total, value}（total が 0 なら value は None）。数え方は `counts` の1か所（品質の段と同じ）。"""
    c = counts(tx, _BUSINESS_WINDOW, (business_id, since, until, voice_type, voice_type))
    items = required_items(tx, business_id, since=since, until=until, voice_type=voice_type)
    asked = sum(n for t in items.values() for m, n in t.items() if m != "not_asked")
    return {"required_taken": _rate(sum(t["taken"] for t in items.values()), asked),   # 必須の質問の取得率（伺っていない＝対象外は数えない）
            "dialed": c["dialed"], "answered": c["answered"], "talked": c["talked"], "reached": c["reached"],   # 出た・お客様と話した・目的のお客様と話せた
            "completion": _rate(c["completed"], c["rows"]),          # 業務完了率（範囲に通話のある行のうち）
            "cut": _rate(c["cut"], c["talked"]),                      # 途中で切られた率（話せた通話のうち）
            "average_talk_seconds": None if not c["talked"] else float(c["talk_seconds"]) / c["talked"],
            "redial": _rate(c["redials"], c["dialed"]),               # 再発信率（かけた通話のうち、行の2本目から）
            "yen_per_call": None if not c["dialed"] else float(c["yen"]) / c["dialed"], "unpriced": c["unpriced"],
            "technical_error": _rate(c["technical"], c["dialed"]),    # 音声の問題・こちらの不具合の率
            "ear_disagreement": _rate(c["ear_hits"], c["ear_total"]), "forbidden_speech": c["forbidden"],
            "complaint": _rate(c["complaint"], c["talked"]),
            # 会話AIの声の率（黙り・声の途切れ・言葉の抜け・同じ文の2回・英語。見つかったターン÷確かめたターン）。範囲に数えた通話が無ければ None＝まだ数えていない
            # （0 と言わない＝原則10。CL-1 の連絡帳 4-221）
            "ai_voice": {k: None if not c["voice_counted"] else _rate(c[f"voice_{k}_found"], c[f"voice_{k}_checked"]) for k in attempts.VOICE_KINDS}}
