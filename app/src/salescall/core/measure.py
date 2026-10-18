"""誤判定率の正解と抜き取り（Intent.md 16-3・11-4 第2版。DESIGN.md 10-3。表の作り 0066）。持ち主 CL-2。

Intent.md 16-3：「誤判定率：通話のサンプルを管理者か契約者が聞いて正解を付け（検証モードで契約者が直した結果も、正解として使う）、
システムの判定と比べて出す。サンプルの件数と抜き方を、記録に残す」。

- **正解を入れるのは `record_check` の1か所**（管理者か契約者だけ＝データベースが守る）。呼ぶのは2つ：
  - 検証モード：契約者が結果を承認した（`approval.approve_result`＝システムの判定どおりが正解）・直した（`approval.correct_result`＝
    直した中身が正解）とき。
  - 本番：抜き取った通話の録音を、管理者か契約者が聞いて正解を付けたとき（画面＝CL-3 が呼ぶ）。
  正解は「正しい業務の状態」と「システムが渡した項目のうち、違っていたもの（質問の番号）」。同じ版に付け直したら、新しい正解を
  足す（最新が正本）。
- **正解を付けたら、その通話の「測り終わるまで消さない」印を外す**（`recordings.release`。2026-10-17 ユーザー 案A：測り終わったら、
  ふつうの決まりに戻す）。測り終わる＝その通話に正解が付いた（誤判定率は正解から数えるので、録音はもう要らない）。
- **抜き取りは `draw` の1か所**（見守り役が決まった間隔で呼ぶ）：業務ごと・週ごとに1回、前の週（日本時間の月曜〜日曜）の、
  お客様と話せた本番の通話（会話に入った・検証モードでない・お詫びの電話でない・判定した版がある）から、設定の本数
  （`measure.sample_per_week`）を無作為に選ぶ。母数・本数・乱数の種・選び方を記録し、録音に「測り終わるまで消さない」印を付け、
  契約者へ知らせる。週の終わりから1日あけて選ぶ（その週の通話の締めが済むのを待つ）。
- **誤判定率は `rates` の1か所**：正解の付いた版（同じ版は最新の正解）のうち、業務の状態が違った・項目が1つでも違ったものの割合と、
  本数が少ないうちの不確かさを入れた上の限度（95%の確かさ。正解の数が少ないほど大きく出る＝少ない本数で「誤りなし」と言わない）。
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import notify, recordings, settings
from salescall.core.access import Tx

# 正解の出どころ：検証モードの確かめ（すべての結果を確かめる）・抜き取り（無作為）は、誤判定率の材料（録音の印の理由と同じ名前）。
# 本番で契約者が気づいて直したもの（correction）は、気づいたものだけ＝偏った材料なので、記録はするが誤判定率には混ぜない。
SOURCES = ("verification", "sample", "correction")
MEASURED = ("verification", "sample")
DRAW_AFTER = timedelta(days=1)          # 週の終わりから、これだけあけて選ぶ（その週の通話の締めが済むのを待つ）
METHOD = "前の週（日本時間の月曜〜日曜）の、お客様と話せた本番の通話（会話に入った・検証モードでない・お詫びの電話でない・判定した版がある）から、乱数の種で無作為に選んだ"


def record_check(tx: Tx, task_id: UUID, version_no: int, *, truth_state: str, wrong_items: list[str] | tuple[str, ...],
                 source: str) -> int:
    """システムが判定した結果の版に、正解を付ける（取引の依頼者の行。管理者か契約者だけ）。その出どころの印（検証モード・抜き取り）を
    外す。正解の番号を返す。"""
    if source not in SOURCES:
        raise ValueError(f"正解の出どころは {SOURCES} のどれかです（{source!r}）")
    v = tx.one("SELECT attempt_id FROM salescall.task_results WHERE task_id = %s AND version_no = %s", (task_id, version_no))
    if v is None or v["attempt_id"] is None:
        raise LookupError(f"正解を付けられる結果の版がありません：{task_id} の版 {version_no}")
    check_id = tx.one(
        "INSERT INTO salescall.judgement_checks (tenant_id, client_id, task_id, version_no, attempt_id, source, truth_state,"
        "                                        wrong_items, checked_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING check_id",
        (task_id, version_no, v["attempt_id"], source, truth_state, sorted(set(wrong_items))))["check_id"]
    if source in recordings.HOLD_REASONS:
        recordings.release(tx, [v["attempt_id"]], source, measurement=f"誤判定率の正解（{check_id}）")
    return check_id


def review_source(tx: Tx, task_id: UUID, version_no: int) -> str:
    """契約者が結果を承認・直したときの、正解の出どころ（決めるのはここだけ）：その版の通話が検証モードでかけたもの（かけたときに
    検証の印が付いた＝gate）なら verification、本番の通話なら correction（気づいて直した＝偏った材料）。"""
    held = tx.one("SELECT 1 FROM salescall.task_results r JOIN salescall.recording_holds h ON h.attempt_id = r.attempt_id"
                  " WHERE r.task_id = %s AND r.version_no = %s AND h.reason = 'verification'", (task_id, version_no))
    return "verification" if held is not None else "correction"


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
            " WHERE r.business_id = %(b)s AND a.purpose <> 'apology'"
            "   AND salescall.jst(a.claimed_at) >= %(from)s AND salescall.jst(a.claimed_at) < %(to)s"
            "   AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h WHERE h.attempt_id = a.attempt_id"
            "               AND h.to_state = 'in_conversation')"
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


def rates(tx: Tx, business_id: UUID, *, sources: tuple[str, ...] = MEASURED) -> dict[str, Any]:
    """その業務の誤判定率（取引の依頼者の業務）。正解の付いた版ごとに、最新の正解とシステムの判定を比べる（判定できなかった版は
    数えない：誤った結果を出したのではなく、結果を出さなかった＝無人完結率の側。Intent.md 16-3）。
    checked＝正解の付いた版の数、wrong＝誤った数（業務の状態が違った state_wrong・項目が違った items_wrong。両方なら両方に数える）、
    rate＝割合、upper＝95%の確かさで言える上の限度（正解が無ければ None）。sources＝数える正解の出どころ（初めは検証モードと
    抜き取り。本番で気づいて直したものは偏るので混ぜない）。"""
    if not sources or set(sources) - set(SOURCES):
        raise ValueError(f"正解の出どころは {SOURCES} から選びます（{sources!r}）")
    rows = tx.all(
        "SELECT DISTINCT ON (c.task_id, c.version_no) c.truth_state::text AS truth, c.wrong_items, r.business_state::text AS said"
        " FROM salescall.judgement_checks c"
        " JOIN salescall.task_results r ON r.task_id = c.task_id AND r.version_no = c.version_no"
        " JOIN salescall.tasks t ON t.client_id = c.client_id AND t.task_id = c.task_id"
        " JOIN salescall.runs u ON u.client_id = t.client_id AND u.run_id = t.run_id"
        " WHERE u.business_id = %s AND c.source = ANY(%s) AND r.business_state <> 'unknown'"
        " ORDER BY c.task_id, c.version_no, c.check_id DESC", (business_id, list(sources)))
    state_wrong = sum(1 for r in rows if r["truth"] != r["said"])
    items_wrong = sum(1 for r in rows if r["wrong_items"])
    wrong = sum(1 for r in rows if r["truth"] != r["said"] or r["wrong_items"])
    n = len(rows)
    return {"checked": n, "wrong": wrong, "state_wrong": state_wrong, "items_wrong": items_wrong,
            "rate": None if n == 0 else wrong / n, "upper": None if n == 0 else upper_bound(wrong, n)}


def upper_bound(wrong: int, n: int, confidence: float = 0.95) -> float:
    """n 本のうち wrong 本が誤りだったとき、本当の誤りの割合が、この値より大きい見込みは (1 − confidence) 以下（片側の正確な上の
    限度＝Clopper-Pearson）。例：60本で誤り0本 ⇒ 約0.049。"""
    if not 0 <= wrong <= n or n == 0:
        raise ValueError("本数が合いません")
    if wrong == n:
        return 1.0
    alpha = 1 - confidence

    def at_most(p: float) -> float:   # 誤りの割合が p のとき、誤りが wrong 本以下になる確率
        if p <= 0:
            return 1.0
        logs = [math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * math.log(p) + (n - k) * math.log1p(-p)
                for k in range(wrong + 1)]
        top = max(logs)
        return math.exp(top) * sum(math.exp(x - top) for x in logs)

    lo, hi = wrong / n, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if at_most(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi
