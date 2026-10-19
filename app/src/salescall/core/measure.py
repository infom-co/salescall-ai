"""誤判定率の正解と抜き取り（Intent.md 16-3・11-4 第2版。DESIGN.md 10-3。表の作り 0066）。持ち主 CL-2。

Intent.md 16-3：「誤判定率：通話のサンプルを管理者か契約者が聞いて正解を付け（検証モードで契約者が直した結果も、正解として使う）、
システムの判定と比べて出す。サンプルの件数と抜き方を、記録に残す」。

- **測る通話は、データベースの1か所**（`salescall.call_to_measure`：お客様と話せた通話＝会話に入った・お詫びの電話でない）。
  抜き取りの母数・正解を付けられるか・測らない通話の印を外すかは、どれもこれを読む（2026-10-18 の見直し：前は抜き取りの中に
  しか無く、話していない通話の検証の印が外れなかった）。
- **正解を入れるのは `record_check` の1か所**（管理者か契約者だけ＝データベースが守る）。**正解は通話ごと**：その通話の、システムの
  最新の判定の版にだけ付ける（1つの通話に判定の版が2つあっても、比べる相手は1つ）。呼ぶのは2つ：
  - 検証モード：契約者が結果を承認した（`approval.approve_result`＝システムの判定どおりが正解）・直した（`approval.correct_result`＝
    直した中身が正解）とき。測らない通話（話していない・お詫び）なら、何も入れない。
  - 本番：抜き取った通話の録音を、管理者か契約者が聞いて正解を付けたとき（画面＝CL-3 が呼ぶ）。
  正解は「正しい業務の状態」と「システムが渡した項目のうち、違っていたもの（質問の番号）」。同じ通話に付け直したら、新しい正解を
  足す（最新が正本）。**正解の出どころ（検証モード・抜き取り・本番で気づいて直した）は、データベースがその通話から決めて入れる**
  （呼ぶ側は渡さない。前は呼ぶ側が渡せたので、抜き取っていない通話を抜き取りとして入れられた＝2026-10-18 の見直し）。
- **正解を付けたら、その通話の「測り終わるまで消さない」印を外す**（`recordings.release`。2026-10-17 ユーザー 案A：測り終わったら、
  ふつうの決まりに戻す）。測り終わる＝その通話に正解が付いた（誤判定率は正解から数えるので、録音はもう要らない）。
  **測らない通話の検証の印は、通話が終わったら外す**（`release_unmeasured`。見守り役が抜き取りと一緒に呼ぶ）：検証の印は、かけた
  ときのモードで付ける（gate）ので、話していない通話・かけずに取り消した予約にも付く。
- **抜き取りは `draw` の1か所**（見守り役が決まった間隔で呼ぶ）：業務ごと・週ごとに1回、前の週（日本時間の月曜〜日曜）の、
  測る通話のうち本番のもの（検証モードでない・判定した版がある）から、設定の本数（`measure.sample_per_week`）を無作為に選ぶ。
  母数・本数・乱数の種・選び方を記録し、録音に「測り終わるまで消さない」印を付け、契約者へ知らせる。週の終わりから1日あけて
  選ぶ（その週の通話の締めが済むのを待つ）。
- **誤判定率は `rates` の1か所**：出どころごとに出す（検証モードは、検証の段階のすべての通話＝本番とは別の時期の材料。抜き取りは、
  本番の通話からの無作為の材料。性質の違う材料を1つの割合にまとめない）。正解の付いた通話（同じ通話は最新の正解）のうち、業務の
  状態が違った・項目が1つでも違ったものの割合と、本数が少ないうちの不確かさを入れた上の限度（95%の確かさ。正解の数が少ないほど
  大きく出る＝少ない本数で「誤りなし」と言わない）。
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import notify, recordings, settings, stats
from salescall.core.access import Tx

# 正解の出どころ（決めるのはデータベース＝0074）：検証モードの確かめ（すべての結果を確かめる）・抜き取り（無作為）は、誤判定率の
# 材料（録音の印の理由と同じ名前）。本番で契約者が気づいて直したもの（correction）は、気づいたものだけ＝偏った材料なので、記録は
# するが誤判定率には混ぜない。
SOURCES = ("verification", "sample", "correction")
DRAW_AFTER = timedelta(days=1)          # 週の終わりから、これだけあけて選ぶ（その週の通話の締めが済むのを待つ）
METHOD = "前の週（日本時間の月曜〜日曜）の、お客様と話せた本番の通話（会話に入った・お詫びの電話でない・検証モードでない・判定した版がある）から、乱数の種で無作為に選んだ"
SYSTEM_KINDS = ("judged", "rejudged")   # システムが判定した版（正解を付ける相手）
UNMEASURED = "測らない通話（お客様と話していない・お詫びの電話・かけずに取り消した予約）"


def record_check(tx: Tx, task_id: UUID, version_no: int, *, truth_state: str,
                 wrong_items: list[str] | tuple[str, ...]) -> int | None:
    """システムが判定した結果の版に、正解を付ける（取引の依頼者の行。管理者か契約者だけ）。その通話の出どころの印（検証モード・
    抜き取り）を外す。正解の番号を返す。人の版・測らない通話の版なら、何もせず None（承認・直したことが正解にならない場合）。
    その通話の最新の判定の版でなければ LookupError（比べる相手は、通話ごとに1つ）。"""
    v = tx.one("SELECT kind::text AS kind, attempt_id, salescall.call_to_measure(attempt_id) AS measured"
               " FROM salescall.task_results WHERE task_id = %s AND version_no = %s", (task_id, version_no))
    if v is None:
        raise LookupError(f"結果の版がありません：{task_id} の版 {version_no}")
    if v["kind"] not in SYSTEM_KINDS or v["attempt_id"] is None or not v["measured"]:
        return None
    if tx.one("SELECT 1 FROM salescall.task_results WHERE task_id = %s AND attempt_id = %s AND kind IN ('judged', 'rejudged')"
              " AND version_no > %s", (task_id, v["attempt_id"], version_no)) is not None:
        raise LookupError(f"その通話には、新しい判定の版があります（正解は、その通話の最新の判定の版に付けます）：{task_id} の版 {version_no}")
    got = tx.one(
        "INSERT INTO salescall.judgement_checks (tenant_id, client_id, task_id, version_no, attempt_id, source, truth_state,"
        "                                        wrong_items, checked_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, 'correction', %s, %s, salescall.cur_actor())"
        " RETURNING check_id, source",       # 出どころはデータベースが決めて入れる（ここで渡す値は使われない）
        (task_id, version_no, v["attempt_id"], truth_state, sorted(set(wrong_items))))
    if got["source"] in recordings.HOLD_REASONS:
        recordings.release(tx, [v["attempt_id"]], got["source"], measurement=f"誤判定率の正解（{got['check_id']}）")
    return got["check_id"]


def release_unmeasured(tx: Tx) -> int:
    """取引の依頼者の、終わった測らない通話の検証の印を外す（見守り役が呼ぶ）。外した数を返す。"""
    ids = [r["attempt_id"] for r in tx.all(
        "SELECT h.attempt_id FROM salescall.recording_holds h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id"
        " WHERE h.reason = 'verification' AND h.released_at IS NULL AND NOT salescall.is_active(a.state)"
        "   AND NOT salescall.call_to_measure(h.attempt_id) ORDER BY h.attempt_id")]
    return recordings.release(tx, ids, "verification", measurement=UNMEASURED) if ids else 0


def week_start(day: date) -> date:
    """その日を含む週の月曜。"""
    return day - timedelta(days=day.weekday())


def draw(tx: Tx, *, now: datetime | None = None) -> list[UUID]:
    """取引の依頼者の、業務ごとに、前の週の抜き取りをする（まだしていなくて、週の終わりから1日たっていれば）。した抜き取りの番号を返す。
    now＝いま（日本時間。試験で週を進めるときだけ渡す。ふだんはデータベースの時刻）。"""
    now = now or tx.one("SELECT salescall.jst(now()) AS t")["t"]
    week = week_start(now.date()) - timedelta(days=7)
    if now < _jst_midnight(week + timedelta(days=7)) + DRAW_AFTER:
        return []
    done = []
    for b in tx.all("SELECT business_id, tenant_id, client_id FROM salescall.businesses"
                    " WHERE NOT EXISTS (SELECT 1 FROM salescall.sample_draws d WHERE d.business_id = businesses.business_id"
                    "                   AND d.week_start = %s) ORDER BY business_id", (week,)):
        wanted = int(settings.resolve(tx, "measure.sample_per_week", tenant_id=b["tenant_id"], client_id=b["client_id"],
                                      business_id=b["business_id"]))
        pool = [r["attempt_id"] for r in tx.all(
            "SELECT a.attempt_id FROM salescall.attempts a"
            " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
            " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
            " WHERE r.business_id = %(b)s AND salescall.call_to_measure(a.attempt_id)"
            "   AND salescall.jst(a.claimed_at) >= %(from)s AND salescall.jst(a.claimed_at) < %(to)s"
            "   AND NOT EXISTS (SELECT 1 FROM salescall.recording_holds k WHERE k.attempt_id = a.attempt_id"
            "                   AND k.reason = 'verification')"
            "   AND EXISTS (SELECT 1 FROM salescall.task_results v WHERE v.task_id = a.task_id AND v.attempt_id = a.attempt_id"
            "               AND v.kind IN ('judged', 'rejudged'))"
            " ORDER BY a.attempt_id",
            {"b": b["business_id"], "from": _jst_midnight(week), "to": _jst_midnight(week + timedelta(days=7))})]
        seed = random.SystemRandom().getrandbits(62)
        chosen = sorted(random.Random(seed).sample(pool, min(wanted, len(pool))))
        draw_id = tx.one(
            "INSERT INTO salescall.sample_draws (tenant_id, client_id, business_id, week_start, pool_size, wanted, drawn, seed,"
            "                                    method)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s) RETURNING draw_id",
            (b["business_id"], week, len(pool), wanted, len(chosen), seed, METHOD))["draw_id"]
        for attempt_id in chosen:
            tx.execute("INSERT INTO salescall.sample_draw_calls (tenant_id, client_id, draw_id, attempt_id)"
                       " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s)", (draw_id, attempt_id))
            recordings.hold(tx, attempt_id, "sample")
        if chosen:
            notify.emit(tx, "sample_ready", key=f"sample:{draw_id}",
                        summary=f"誤判定率を測るために、{week:%m月%d日}からの1週間の通話から{len(chosen)}本を抜き取りました。"
                                "録音を聞いて、正解を付けてください。",
                        refs={"draw_id": draw_id, "business_id": b["business_id"]})
        done.append(draw_id)
    return done


def _jst_midnight(day: date) -> datetime:
    """その日の0時（日本時間。データベースの salescall.jst と同じ、時差を持たない日時）。"""
    return datetime(day.year, day.month, day.day)


def rates(tx: Tx, business_id: UUID, *, source: str) -> dict[str, Any]:
    """その業務の、出どころ1つの誤判定率（取引の依頼者の業務）。正解の付いた通話ごとに、最新の正解と、その正解が比べた判定の版を
    比べる（判定できなかった版は数えない：誤った結果を出したのではなく、結果を出さなかった＝無人完結率の側。Intent.md 16-3）。
    checked＝正解の付いた通話の数、wrong＝誤った数（業務の状態が違った state_wrong・項目が違った items_wrong。両方なら両方に
    数える）、rate＝割合、upper＝95%の確かさで言える上の限度（正解が無ければ None）。source＝verification（検証モード）・sample
    （抜き取り）・correction（本番で気づいて直した。偏った材料なので、誤判定率としては使わない。記録を見るため）。"""
    if source not in SOURCES:
        raise ValueError(f"正解の出どころは {SOURCES} のどれかです（{source!r}）")
    rows = [r for r in tx.all(
        "SELECT DISTINCT ON (c.attempt_id) c.truth_state::text AS truth, c.wrong_items, r.business_state::text AS said"
        " FROM salescall.judgement_checks c"
        " JOIN salescall.task_results r ON r.task_id = c.task_id AND r.version_no = c.version_no"
        " JOIN salescall.tasks t ON t.client_id = c.client_id AND t.task_id = c.task_id"
        " JOIN salescall.runs u ON u.client_id = t.client_id AND u.run_id = t.run_id"
        " WHERE u.business_id = %s AND c.source = %s"
        " ORDER BY c.attempt_id, c.check_id DESC", (business_id, source)) if r["said"] != "unknown"]
    state_wrong = sum(1 for r in rows if r["truth"] != r["said"])
    items_wrong = sum(1 for r in rows if r["wrong_items"])
    wrong = sum(1 for r in rows if r["truth"] != r["said"] or r["wrong_items"])
    n = len(rows)
    return {"checked": n, "wrong": wrong, "state_wrong": state_wrong, "items_wrong": items_wrong,
            "rate": None if n == 0 else wrong / n, "upper": None if n == 0 else upper_bound(wrong, n)}


def upper_bound(wrong: int, n: int, confidence: float = 0.95) -> float:
    """n 本のうち wrong 本が誤りだったとき、本当の誤りの割合が、この値より大きい見込みは (1 − confidence) 以下（計算は
    core/stats.py の1か所）。例：60本で誤り0本 ⇒ 約0.049。"""
    return stats.upper_bound(wrong, n, confidence)
