"""報酬の決まりと、今月の報酬・費用（「報酬に対して、費用が何％かかっているか」。DESIGN.md 12-1-1）。持ち主 CL-2。

- 報酬の決まりを足すのは `set_terms` だけ（I-12。app/checks が見張る）。依頼主ごと・版つき（いつから使うか）。報酬の形は、
  成果1件あたり・かけた1本あたり・話せた1本あたり・月額の組み合わせ（依頼主によって違う。2026-10-15 ユーザー）。
- 今月（日本時間の月）の数を数えるのは `month` の1か所：報酬の内訳・費用・かけた本数。上限・赤字の判断は core/usage.py。
  - 成果1件＝その行の業務の状態が、はじめて「完了」になった（人が直した版も含む）。かけた1本＝発信した（dialing に入った）。
    話せた1本＝会話に入った（in_conversation に入った）。どれも、その出来事の時刻に効いていた決まりの単価で数える
    （あとから決まりを変えても、過去の分は変わらない）。
  - 月額＝その月のうち、決まりが効いていた時間で日割り（月の途中の割合を、月末の見込みと比べられるように）。
  - ⏳ 時給は「何の時間か」をユーザーに確かめ中（まだ持たない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from salescall.core.access import Tx


@dataclass(frozen=True)
class Terms:
    """報酬の決まり（円）。使わない形は 0。"""
    per_completed_yen: Decimal = Decimal(0)
    per_dial_yen: Decimal = Decimal(0)
    per_talk_yen: Decimal = Decimal(0)
    monthly_yen: Decimal = Decimal(0)


def set_terms(tx: Tx, terms: Terms, *, valid_from: datetime, note: str) -> int:
    """取引の依頼主に、報酬の決まりを足す（valid_from から使う）。どれも 0 の決まりは足せない（入れ忘れと見分けられない）。"""
    if not any(v > 0 for v in (terms.per_completed_yen, terms.per_dial_yen, terms.per_talk_yen, terms.monthly_yen)):
        raise ValueError("報酬の決まりが、どれも 0 円です")
    return tx.one(
        "INSERT INTO salescall.reward_terms (tenant_id, client_id, valid_from, per_completed_yen, per_dial_yen, per_talk_yen,"
        " monthly_yen, note, set_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s,"
        " salescall.cur_actor()) RETURNING terms_id",
        (valid_from, terms.per_completed_yen, terms.per_dial_yen, terms.per_talk_yen, terms.monthly_yen, note))["terms_id"]


# その時刻に効いていた決まりの、ある形の単価（無ければ 0）
_PRICE_AT = ("coalesce((SELECT t.{col} FROM salescall.reward_terms t WHERE t.valid_from <= x.at"
             " ORDER BY t.valid_from DESC LIMIT 1), 0)")


def month(tx: Tx) -> dict[str, Any]:
    """取引の依頼主の、今月（日本時間）の数。cost・revenue（合計）・breakdown（形ごとの数と円）・dials（かけた本数）・
    ratio（費用 ÷ 報酬。報酬が 0 なら None）・deficit（費用 ＞ 報酬）・has_terms（報酬の決まりがあるか）。"""
    b = tx.one("SELECT salescall.from_jst(date_trunc('month', salescall.jst(now()))) AS s,"
               " salescall.from_jst(date_trunc('month', salescall.jst(now())) + interval '1 month') AS e, now() AS now")
    events = {
        "completed": ("per_completed_yen", "SELECT min(r.decided_at) AS at FROM salescall.task_results r"
                                           " WHERE r.business_state = 'completed' GROUP BY r.task_id"),
        "dial": ("per_dial_yen", "SELECT h.at FROM salescall.attempt_state_history h WHERE h.to_state = 'dialing'"),
        "talk": ("per_talk_yen", "SELECT h.at FROM salescall.attempt_state_history h WHERE h.to_state = 'in_conversation'"),
    }
    breakdown: dict[str, dict[str, Any]] = {}
    for name, (col, sql) in events.items():
        row = tx.one(f"SELECT count(*) AS n, coalesce(sum({_PRICE_AT.format(col=col)}), 0) AS yen"
                     f" FROM ({sql}) x WHERE x.at >= %s AND x.at <= %s", (b["s"], b["now"]))
        breakdown[name] = {"count": row["n"], "yen": Decimal(row["yen"])}
    terms = tx.all("SELECT valid_from, monthly_yen FROM salescall.reward_terms ORDER BY valid_from")
    monthly, span = Decimal(0), (b["e"] - b["s"]).total_seconds()
    for i, t in enumerate(terms):
        start = max(t["valid_from"], b["s"])
        end = min(terms[i + 1]["valid_from"] if i + 1 < len(terms) else b["now"], b["now"])
        if end > start:
            monthly += Decimal(t["monthly_yen"]) * Decimal((end - start).total_seconds()) / Decimal(span)
    breakdown["monthly"] = {"count": None, "yen": monthly}
    cost = Decimal(tx.one("SELECT coalesce(sum(amount_yen), 0) AS y FROM salescall.usage_entries"
                          " WHERE occurred_at >= %s AND occurred_at <= %s", (b["s"], b["now"]))["y"])
    revenue = sum((x["yen"] for x in breakdown.values()), Decimal(0))
    return {"month_start": b["s"], "cost": cost, "revenue": revenue, "breakdown": breakdown,
            "dials": breakdown["dial"]["count"], "ratio": None if revenue == 0 else cost / revenue,
            "deficit": cost > revenue, "has_terms": bool(terms)}
