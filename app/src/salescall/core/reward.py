"""報酬の決まりと、今月の報酬・費用（「報酬に対して、費用が何％かかっているか」。DESIGN.md 12-1-1）。持ち主 CL-2。

- 報酬の決まりを足すのは `set_terms` だけ（I-12。app/checks が見張る）。業務ごと・版つき（いつから使うか）。報酬の形は、
  成果1件あたり・かけた1本あたり・話せた1本あたり・月額・時給の組み合わせ（依頼者・業務によって違う。2026-10-15 ユーザー。✅ 2026-11-18 管理者
  「報酬の決まりは『業務』ごと」＝0256。⛔ 前は依頼者ごと）。今月の数・費用の割合・赤字の判断は依頼者ごと（その依頼者の業務を合わせる＝計画書 12-2）。
- 今月（日本時間の月）の数を数えるのは `month` の1か所：報酬の内訳・費用・かけた本数。上限・赤字の判断は core/usage.py。
  - 成果1件＝いまの業務の状態（最新の結果の版）が「完了」の行を、はじめて「完了」になった時刻で数える（契約者が直した版も
    含む。完了でないと直した行は数えない）。かけた1本＝発信した（dialing に入った時刻で数える。お客様のところで鳴りえた発信だけ＝
    salescall.is_placed の1か所：PC がかけずに取り消した発信・回線の部品が番号を受け付けなかった発信は数えない。⛔ 見直し15）。
    話せた1本＝会話に入った（in_conversation に入った）。どれも、その出来事の時刻に効いていた決まりの単価で数える
    （あとから決まりを変えても、過去の分は変わらない）。単価は、その出来事の業務の決まり（`_PRICE_AT`）。
  - 月額＝その月のうち、決まりが効いていた時間で日割り（月の途中の割合を、月末の見込みと比べられるように）。
  - 時給＝依頼者から見て、頼んだ先（管理者や契約者）が架電作業をしていた時間に当たる時間（2026-10-15 ユーザー）。人と同じ速さで
    数える（2026-10-17 ユーザー：「時給換算では、人がやるのと同じスピードが重要」）：電話1本ごとに、かけ始め（予約）から終わりまで
    ＋「電話と電話の間」（人が報告を書き、リストを確かめ、次にかけるまでの時間。依頼者ごとに報酬の決まりで決める＝
    `Terms.hourly_gap_seconds`。ユーザー：「1から2分くらい」「依頼者ごとに上下で設定できる」）。数えるのはデータベースの1か所
    （salescall.work_seconds）。
  - お詫びの電話（こちらの誤りのため）は、どの報酬にも数えない（かけた1本・話せた1本・時給）。費用には数える。
  - 途中で切れたあと、お詫びしてすぐかけ直した電話（目的 resume）は、1本あたりの報酬には数えない（1人のお客様に1本分）。時給には
    その時間も入れる（1人のお客様への総時間）が、電話と電話の間は足さない（2026-10-18 ユーザー）。費用には数える。
- **余裕を足した報酬**（`upper`）：成果と話せた本数は偶然でぶれる（月の初めに成果0件でも、本当の成果の割合が低いとは限らない）。
  そこで、今月の報酬に「成果1件分・話せた1本分の報酬の、赤字の知らせの早さの割合（4分の1・半分・1件分）」を余裕として足す
  （依頼者ごとの報酬の決まりの「赤字の知らせの早さ」＝`CAUTIONS`。2026-10-17 ユーザー：案B「成果が0件のまま、使った費用が成果1件分
  の報酬の4分の1・半分・1件分を超えたら知らせる」）。判断する側は、これでも上限・赤字になるときだけ判断する（例：成果1件 1,000円・
  1本 約2円・早めなら、成果0件のまま約125本＝約250円で赤字と判断）。成果が出たあとも、同じ余裕のまま（2026-10-18 の見直し：前は
  ぶれの計算で余裕が成果の数とともに変わり、早めでは成果1件目から余裕が0になって、画面の説明と合わなかった）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from salescall.core import attempts
from salescall.core.access import Tx


# 赤字の知らせの早さ（2026-10-17 ユーザー：「初めての契約者でも分かるように」・案B「成果が0件のまま、使った費用が成果1件分の報酬の
# 4分の1・半分・1件分を超えたら知らせる」）。画面に出す名前と説明、中の数は、ここだけ。
# 中の数＝報酬に足す余裕（成果1件分・話せた1本分の報酬の何倍か＝margin）。成果が出たあとも同じ余裕を足す。
# 例は「成果1件 1,000円・電話1本 約2円の依頼者」（どれも計算で出した数）
CAUTION_EXAMPLE = "例：成果1件の報酬が1,000円、電話1本の費用が約2円の依頼者なら"
CAUTIONS: dict[str, tuple[str, str, float]] = {
    "early": ("早め（成果1件分の4分の1）",
              "成果が0件のまま、使った費用が成果1件分の報酬の4分の1を超えたら、赤字をお知らせします（約125本）", 0.25),
    "normal": ("中くらい（成果1件分の半分）",
               "成果が0件のまま、使った費用が成果1件分の報酬の半分を超えたら、赤字をお知らせします（約250本）", 0.5),
    "careful": ("遅め（成果1件分）",
                "成果が0件のまま、使った費用が成果1件分の報酬を超えたら、赤字をお知らせします（約500本）", 1.0),
}


def caution_margin(name: str) -> Decimal:
    """その早さで報酬に足す余裕（成果1件分・話せた1本分の報酬の何倍か）。"""
    return Decimal(str(CAUTIONS[name][2]))

# 時給の「電話と電話の間」の画面の初めの値と、上下のボタン1回の幅（秒。2026-10-17 ユーザー：「デフォルト2分、ボタン1回で30秒ずつ」）
HOURLY_GAP_INITIAL_SECONDS = 120
HOURLY_GAP_STEP_SECONDS = 30


@dataclass(frozen=True)
class Terms:
    """報酬の決まり（円）。使わない形は 0。deficit_caution（赤字の知らせの早さ：`CAUTIONS` の名前）は必ず書く。
    hourly_gap_seconds（電話と電話の間の秒数）は、時給の決まりがあるときは必ず書く（どちらも入れ忘れを既定値で埋めない。
    画面は初めの値を見せて、契約者が確かめて登録する）。"""
    deficit_caution: str | None = None
    per_completed_yen: Decimal = Decimal(0)
    per_dial_yen: Decimal = Decimal(0)
    per_talk_yen: Decimal = Decimal(0)
    monthly_yen: Decimal = Decimal(0)
    hourly_yen: Decimal = Decimal(0)
    hourly_gap_seconds: int | None = None


def set_terms(tx: Tx, business_id: UUID, terms: Terms, *, valid_from: datetime, note: str) -> int:
    """取引の依頼者の業務に、報酬の決まりを足す（valid_from から使う）。どれも 0 の決まりは足せない（入れ忘れと見分けられない）。"""
    if not any(v > 0 for v in (terms.per_completed_yen, terms.per_dial_yen, terms.per_talk_yen, terms.monthly_yen,
                               terms.hourly_yen)):
        raise ValueError("報酬の決まりが、どれも 0 円です")
    if terms.deficit_caution not in CAUTIONS:
        raise ValueError(f"赤字の知らせの早さは {list(CAUTIONS)} のどれかです（{terms.deficit_caution!r}）")
    if terms.hourly_yen > 0 and terms.hourly_gap_seconds is None:
        raise ValueError("時給の決まりには、電話と電話の間の秒数（hourly_gap_seconds）が要ります")
    return tx.one(
        "INSERT INTO salescall.reward_terms (tenant_id, client_id, business_id, valid_from, per_completed_yen, per_dial_yen, per_talk_yen,"
        " monthly_yen, hourly_yen, hourly_gap_seconds, deficit_caution, note, set_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING terms_id",
        (business_id, valid_from, terms.per_completed_yen, terms.per_dial_yen, terms.per_talk_yen, terms.monthly_yen, terms.hourly_yen,
         terms.hourly_gap_seconds, terms.deficit_caution, note))["terms_id"]


# 1本あたりの報酬（かけた1本・話せた1本）に数える電話：お詫びの電話（こちらの誤りのため。時給の時間にも入れない＝salescall.counts_for_reward。
# 2026-10-30 管理者：今の作りのまま確定）と、途中で切れたあとにお詫びして
# すぐかけ直した電話は数えない（2026-10-18 ユーザー：「回線が切れてもシステムが途切れても、1人のお客さんに対して、50円ですよね」）。
# 決まりはデータベースの1か所 salescall.counts_per_call（電話の種類 keeping_kind から。時給の「電話と電話の間」・回線の応答率も同じ。
# ⛔ 見直し17b：前は目的の名前をここに直に書いていた）。時給の時間は salescall.work_seconds（かけ直した電話の時間も入れる＝1人の
# お客様への総時間。電話と電話の間は足さない）
_BILLED_PER_CALL = "salescall.counts_per_call(salescall.keeping_kind(a.purpose, a.promised))"


# その出来事の業務の、その時刻に効いていた決まりの、ある形の単価（無ければ 0。1か所）
_PRICE_AT = ("coalesce((SELECT t.{col} FROM salescall.reward_terms t WHERE t.business_id = x.business_id AND t.valid_from <= x.at"
             " ORDER BY t.valid_from DESC LIMIT 1), 0)")


def month(tx: Tx) -> dict[str, Any]:
    """取引の依頼者の、今月（日本時間）の数。cost・revenue（合計）・breakdown（形ごとの数と円。時給は秒）・
    upper（余裕を足した報酬。余裕は、いま決まりが効いている業務ごとに足す）・has_terms（いま効いている報酬の決まりがあり、今月かけた業務の
    どれにも、いま効いている決まりがあるか。先の日付からの決まりだけなら無い）・businesses_without_terms（今月かけたのに、いま効いている
    決まりの無い業務。報酬が足りずに数えられ、赤字を見誤るので、判断しない＝`usage._deficit` の UNKNOWN）・
    unpriced_entries（単価が設定されておらず金額の無い利用量の数。費用はその分を含まない＝画面で注意として出す）・
    cost_by_kind（費用の、実測と推定の内訳 {"measured": 円, "estimated": 円}。合計は cost と同じ＝同じ1つの問い合わせで数える。計画書 5-2
    「今月の費用（実測と推定を分けて）」・CL-3 の 23）。
    余裕は、いまの報酬の決まりの赤字の知らせの早さ（`CAUTIONS`。決まりが無ければ、足さない）。"""
    b = tx.one("SELECT salescall.from_jst(date_trunc('month', salescall.jst(now()))) AS s,"
               " salescall.from_jst(date_trunc('month', salescall.jst(now())) + interval '1 month') AS e, now() AS now")
    events = {
        "completed": ("per_completed_yen", "SELECT min(r.decided_at) AS at, rn.business_id FROM salescall.task_results r"
                                           " JOIN salescall.latest_task_results l ON l.task_id = r.task_id"
                                           " JOIN salescall.tasks t ON t.task_id = r.task_id JOIN salescall.runs rn ON rn.run_id = t.run_id"
                                           " WHERE r.business_state = 'completed' AND l.business_state = 'completed'"
                                           " GROUP BY r.task_id, rn.business_id"),
        "dial": ("per_dial_yen", "SELECT h.at, salescall.attempt_business(a.attempt_id) AS business_id FROM salescall.attempt_state_history h"
                                 " JOIN salescall.attempts a ON a.attempt_id = h.attempt_id WHERE h.to_state = 'dialing' AND " + attempts.placed("a")
                 + " AND " + _BILLED_PER_CALL),
        "talk": ("per_talk_yen", "SELECT h.at, salescall.attempt_business(a.attempt_id) AS business_id FROM salescall.attempt_state_history h"
                                 " JOIN salescall.attempts a ON a.attempt_id = h.attempt_id WHERE h.to_state = 'in_conversation' AND "
                 + _BILLED_PER_CALL),
    }
    breakdown: dict[str, dict[str, Any]] = {}
    for name, (col, sql) in events.items():
        row = tx.one(f"SELECT count(*) AS n, coalesce(sum({_PRICE_AT.format(col=col)}), 0) AS yen"
                     f" FROM ({sql}) x WHERE x.at >= %s AND x.at <= %s", (b["s"], b["now"]))
        breakdown[name] = {"count": row["n"], "yen": Decimal(row["yen"])}
    work = tx.one(f"SELECT coalesce(sum(x.seconds), 0) AS s, coalesce(sum(x.seconds / 3600 * {_PRICE_AT.format(col='hourly_yen')}),"
                  " 0) AS yen FROM salescall.work_seconds(%s, %s) x", (b["s"], b["now"]))
    breakdown["hourly"] = {"count": None, "seconds": Decimal(work["s"]), "yen": Decimal(work["yen"])}
    terms = tx.all("SELECT business_id, valid_from, monthly_yen, per_completed_yen, per_talk_yen, deficit_caution FROM salescall.reward_terms"
                   " ORDER BY business_id, valid_from")
    by_business: dict[Any, list[dict[str, Any]]] = {}
    for t in terms:
        by_business.setdefault(t["business_id"], []).append(t)
    monthly, span = Decimal(0), (b["e"] - b["s"]).total_seconds()
    for rows in by_business.values():   # 月額は業務ごとに、その業務の決まりが効いていた時間で日割り
        for i, t in enumerate(rows):
            start = max(t["valid_from"], b["s"])
            end = min(rows[i + 1]["valid_from"] if i + 1 < len(rows) else b["now"], b["now"])
            if end > start:
                monthly += Decimal(t["monthly_yen"]) * Decimal((end - start).total_seconds()) / Decimal(span)
    breakdown["monthly"] = {"count": None, "yen": monthly}
    c = tx.one("SELECT coalesce(sum(amount_yen), 0) AS y, count(*) FILTER (WHERE amount_yen IS NULL) AS unpriced,"
               "       coalesce(sum(amount_yen) FILTER (WHERE kind = 'measured'), 0) AS measured,"
               "       coalesce(sum(amount_yen) FILTER (WHERE kind = 'estimated'), 0) AS estimated"
               " FROM salescall.usage_entries WHERE occurred_at >= %s AND occurred_at <= %s", (b["s"], b["now"]))
    cost = Decimal(c["y"])
    revenue = sum((x["yen"] for x in breakdown.values()), Decimal(0))
    # ぶれる2つ（成果・話せた本数）の余裕：業務ごとに、いまの決まりの単価 × 赤字の知らせの早さの割合
    now_terms = {bid: t for bid, rows in by_business.items()
                 if (t := next((r for r in reversed(rows) if r["valid_from"] <= b["now"]), None)) is not None}
    upper = revenue
    for t in now_terms.values():
        upper += (Decimal(t["per_completed_yen"]) + Decimal(t["per_talk_yen"])) * caution_margin(t["deficit_caution"])
    called = {r["business_id"] for r in tx.all(
        "SELECT DISTINCT salescall.attempt_business(a.attempt_id) AS business_id FROM salescall.attempts a"
        " WHERE a.claimed_at >= %s AND a.claimed_at <= %s", (b["s"], b["now"]))}
    missing = sorted(called - set(now_terms), key=str)
    return {"month_start": b["s"], "cost": cost, "revenue": revenue, "upper": upper, "breakdown": breakdown,
            "has_terms": bool(now_terms) and not missing, "businesses_without_terms": missing, "unpriced_entries": c["unpriced"],
            "cost_by_kind": {"measured": Decimal(c["measured"]), "estimated": Decimal(c["estimated"])}}
