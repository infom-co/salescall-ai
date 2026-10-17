"""報酬の決まりと、今月の報酬・費用（「報酬に対して、費用が何％かかっているか」。DESIGN.md 12-1-1）。持ち主 CL-2。

- 報酬の決まりを足すのは `set_terms` だけ（I-12。app/checks が見張る）。依頼者ごと・版つき（いつから使うか）。報酬の形は、
  成果1件あたり・かけた1本あたり・話せた1本あたり・月額・時給の組み合わせ（依頼者によって違う。2026-10-15 ユーザー）。
- 今月（日本時間の月）の数を数えるのは `month` の1か所：報酬の内訳・費用・かけた本数。上限・赤字の判断は core/usage.py。
  - 成果1件＝いまの業務の状態（最新の結果の版）が「完了」の行を、はじめて「完了」になった時刻で数える（契約者が直した版も
    含む。完了でないと直した行は数えない）。かけた1本＝発信した（dialing に入った）。
    話せた1本＝会話に入った（in_conversation に入った）。どれも、その出来事の時刻に効いていた決まりの単価で数える
    （あとから決まりを変えても、過去の分は変わらない）。
  - 月額＝その月のうち、決まりが効いていた時間で日割り（月の途中の割合を、月末の見込みと比べられるように）。
  - 時給＝依頼者から見て、頼んだ先（管理者や契約者）が架電作業をしていた時間に当たる時間（2026-10-15 ユーザー）。人と同じ速さで
    数える（2026-10-17 ユーザー：「時給換算では、人がやるのと同じスピードが重要」）：電話1本ごとに、かけ始め（予約）から終わりまで
    ＋「電話と電話の間」（人が報告を書き、リストを確かめ、次にかけるまでの時間。依頼者ごとに報酬の決まりで決める＝
    `Terms.hourly_gap_seconds`。ユーザー：「1から2分くらい」「依頼者ごとに上下で設定できる」）。数えるのはデータベースの1か所
    （salescall.work_seconds）。
  - お詫びの電話（こちらの誤りのため）は、どの報酬にも数えない（かけた1本・話せた1本・時給）。費用には数える。
- **ぶれを多めに見込んだ報酬**（`upper`）：成果と話せた本数は偶然でぶれる（月の初めに成果0件でも、本当の成果の割合が低いとは
  限らない）。そこで、この2つの数を、確かさ（依頼者ごとの報酬の決まりの「赤字の知らせの慎重さ」＝`CAUTIONS`。2026-10-17 ユーザー：「依頼者ごと」「初めての契約者
  でも分かるように」）の上側まで多めに見込んだ報酬も出す（ポアソン分布の
  上側の限界）。判断する側は、これでも上限・赤字になるときだけ判断する ⇒ 判断し始める本数を決め打ちにせず、報酬と費用から
  逆算される（例：成果1件 1,000円・1本 約2円なら、成果0件のまま約1,500本＝約3,000円で赤字と判断）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from salescall.core.access import Tx


# 赤字の知らせの慎重さ（2026-10-17 ユーザー：「初めての契約者でも分かるように」）。画面に出す名前と説明、中の確かさは、ここだけ。
# 例は「成果1件 1,000円・電話1本 約2円の依頼者で、まだ成果が0件のとき、何本かけたところで赤字と知らせるか」（どれも計算で出した数）
CAUTIONS: dict[str, tuple[str, str, float]] = {
    "early": ("早めに知らせる",
              "成果がまだ出ていない時期でも、早めに赤字をお知らせします。成果がたまたま遅れているだけのときも、お知らせが出ることが"
              "あります（例：約800本で）", 0.80),
    "normal": ("ふつう（おすすめ）",
               "早すぎず遅すぎず、赤字をお知らせします（例：約1,500本で）", 0.95),
    "careful": ("よく確かめてから知らせる",
                "成果がたまたま遅れているだけの時期には、ほとんどお知らせしません。そのぶん、お知らせは遅くなります（例：約2,300本で）",
                0.99),
}
# 時給の「電話と電話の間」の画面の初めの値と、上下のボタン1回の幅（秒。2026-10-17 ユーザー：「デフォルト2分、ボタン1回で30秒ずつ」）
HOURLY_GAP_INITIAL_SECONDS = 120
HOURLY_GAP_STEP_SECONDS = 30


@dataclass(frozen=True)
class Terms:
    """報酬の決まり（円）。使わない形は 0。deficit_caution（赤字の知らせの慎重さ：`CAUTIONS` の名前）は必ず書く。
    hourly_gap_seconds（電話と電話の間の秒数）は、時給の決まりがあるときは必ず書く（どちらも入れ忘れを既定値で埋めない。
    画面は初めの値を見せて、契約者が確かめて登録する）。"""
    deficit_caution: str | None = None
    per_completed_yen: Decimal = Decimal(0)
    per_dial_yen: Decimal = Decimal(0)
    per_talk_yen: Decimal = Decimal(0)
    monthly_yen: Decimal = Decimal(0)
    hourly_yen: Decimal = Decimal(0)
    hourly_gap_seconds: int | None = None


def set_terms(tx: Tx, terms: Terms, *, valid_from: datetime, note: str) -> int:
    """取引の依頼者に、報酬の決まりを足す（valid_from から使う）。どれも 0 の決まりは足せない（入れ忘れと見分けられない）。"""
    if not any(v > 0 for v in (terms.per_completed_yen, terms.per_dial_yen, terms.per_talk_yen, terms.monthly_yen,
                               terms.hourly_yen)):
        raise ValueError("報酬の決まりが、どれも 0 円です")
    if terms.deficit_caution not in CAUTIONS:
        raise ValueError(f"赤字の知らせの慎重さは {list(CAUTIONS)} のどれかです（{terms.deficit_caution!r}）")
    if terms.hourly_yen > 0 and terms.hourly_gap_seconds is None:
        raise ValueError("時給の決まりには、電話と電話の間の秒数（hourly_gap_seconds）が要ります")
    return tx.one(
        "INSERT INTO salescall.reward_terms (tenant_id, client_id, valid_from, per_completed_yen, per_dial_yen, per_talk_yen,"
        " monthly_yen, hourly_yen, hourly_gap_seconds, deficit_caution, note, set_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING terms_id",
        (valid_from, terms.per_completed_yen, terms.per_dial_yen, terms.per_talk_yen, terms.monthly_yen, terms.hourly_yen,
         terms.hourly_gap_seconds, terms.deficit_caution, note))["terms_id"]


def _upper_q(a: float, x: float) -> float:
    """上側の正則化ガンマ関数 Q(a, x)（a は正の整数で使う）。級数と連分数（よく知られた計算のしかた）。"""
    if x <= 0:
        return 1.0
    log_front = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1:                                    # 下側を級数で出して、1 から引く
        term = total = 1.0 / a
        n = a
        for _ in range(10_000):
            n += 1
            term *= x / n
            total += term
            if term < total * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_front))
    b, c, d = x + 1 - a, 1e300, 1.0 / (x + 1 - a)   # 上側を連分数で出す
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1) < 1e-15:
            break
    return math.exp(log_front) * h


def poisson_upper(k: int, confidence: float) -> float:
    """k 回起きたのを見たとき、起きる数の見込みの上側の限界（確かさ confidence）。P(X ≤ k | λ) ＝ 1 − confidence となる λ。
    例：k＝0、確かさ 0.95 ⇒ 約 3.0。"""
    target = 1 - confidence
    lo, hi = float(k), float(k) + 10 * math.sqrt(k + 1) + 10
    for _ in range(200):                             # P(X ≤ k | λ) ＝ Q(k + 1, λ) は λ で減る
        mid = (lo + hi) / 2
        if _upper_q(k + 1, mid) > target:
            lo = mid
        else:
            hi = mid
    return hi


# その時刻に効いていた決まりの、ある形の単価（無ければ 0）
_PRICE_AT = ("coalesce((SELECT t.{col} FROM salescall.reward_terms t WHERE t.valid_from <= x.at"
             " ORDER BY t.valid_from DESC LIMIT 1), 0)")


def month(tx: Tx) -> dict[str, Any]:
    """取引の依頼者の、今月（日本時間）の数。cost・revenue（合計）・breakdown（形ごとの数と円。時給は秒）・
    upper（成果と話せた本数を、確かさ confidence の上側まで多めに見込んだ報酬）・has_terms（報酬の決まりがあるか）・
    unpriced_entries（単価が設定されておらず金額の無い利用量の数。費用はその分を含まない＝画面で注意として出す）。
    確かさは、いまの報酬の決まりの赤字の知らせの慎重さ（`CAUTIONS`。決まりが無ければ、多めに見込まない）。"""
    b = tx.one("SELECT salescall.from_jst(date_trunc('month', salescall.jst(now()))) AS s,"
               " salescall.from_jst(date_trunc('month', salescall.jst(now())) + interval '1 month') AS e, now() AS now")
    events = {
        "completed": ("per_completed_yen", "SELECT min(r.decided_at) AS at FROM salescall.task_results r"
                                           " JOIN salescall.latest_task_results l ON l.task_id = r.task_id"
                                           " WHERE r.business_state = 'completed' AND l.business_state = 'completed'"
                                           " GROUP BY r.task_id"),
        "dial": ("per_dial_yen", "SELECT h.at FROM salescall.attempt_state_history h JOIN salescall.attempts a"
                                 " ON a.attempt_id = h.attempt_id WHERE h.to_state = 'dialing' AND a.purpose <> 'apology'"),
        "talk": ("per_talk_yen", "SELECT h.at FROM salescall.attempt_state_history h JOIN salescall.attempts a"
                                 " ON a.attempt_id = h.attempt_id WHERE h.to_state = 'in_conversation' AND a.purpose <> 'apology'"),
    }
    breakdown: dict[str, dict[str, Any]] = {}
    for name, (col, sql) in events.items():
        row = tx.one(f"SELECT count(*) AS n, coalesce(sum({_PRICE_AT.format(col=col)}), 0) AS yen"
                     f" FROM ({sql}) x WHERE x.at >= %s AND x.at <= %s", (b["s"], b["now"]))
        breakdown[name] = {"count": row["n"], "yen": Decimal(row["yen"])}
    work = tx.one(f"SELECT coalesce(sum(x.seconds), 0) AS s, coalesce(sum(x.seconds / 3600 * {_PRICE_AT.format(col='hourly_yen')}),"
                  " 0) AS yen FROM salescall.work_seconds(%s, %s) x", (b["s"], b["now"]))
    breakdown["hourly"] = {"count": None, "seconds": Decimal(work["s"]), "yen": Decimal(work["yen"])}
    terms = tx.all("SELECT valid_from, monthly_yen, per_completed_yen, per_talk_yen, deficit_caution FROM salescall.reward_terms"
                   " ORDER BY valid_from")
    monthly, span = Decimal(0), (b["e"] - b["s"]).total_seconds()
    for i, t in enumerate(terms):
        start = max(t["valid_from"], b["s"])
        end = min(terms[i + 1]["valid_from"] if i + 1 < len(terms) else b["now"], b["now"])
        if end > start:
            monthly += Decimal(t["monthly_yen"]) * Decimal((end - start).total_seconds()) / Decimal(span)
    breakdown["monthly"] = {"count": None, "yen": monthly}
    c = tx.one("SELECT coalesce(sum(amount_yen), 0) AS y, count(*) FILTER (WHERE amount_yen IS NULL) AS unpriced"
               " FROM salescall.usage_entries WHERE occurred_at >= %s AND occurred_at <= %s", (b["s"], b["now"]))
    cost = Decimal(c["y"])
    revenue = sum((x["yen"] for x in breakdown.values()), Decimal(0))
    # ぶれる2つ（成果・話せた本数）を多めに見込んだ分：見た数より多い分を、いまの決まりの単価で足す
    now_terms = next((t for t in reversed(terms) if t["valid_from"] <= b["now"]), None)
    upper = revenue
    for name, col in (("completed", "per_completed_yen"), ("talk", "per_talk_yen")):
        if now_terms is None:
            break
        k = breakdown[name]["count"]
        upper += Decimal(now_terms[col]) * Decimal(str(poisson_upper(k, CAUTIONS[now_terms["deficit_caution"]][2]) - k))
    return {"month_start": b["s"], "cost": cost, "revenue": revenue, "upper": upper, "breakdown": breakdown,
            "has_terms": bool(terms), "unpriced_entries": c["unpriced"]}
