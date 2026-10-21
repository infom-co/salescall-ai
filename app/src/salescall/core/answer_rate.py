"""番号ごとの応答率の見張り（Intent.md 8-6。DESIGN.md 5-2-3。表の作り 0068）。持ち主 CL-2。

Intent.md 8-6：「同じ番号から短時間に大量にかけると、お客様側の迷惑電話の判定（端末やアプリ）に載り、応答率が落ちることがある。
番号ごとの1日の上限を設け、番号ごとの応答率の急な低下を監視する」。1日の上限は門（line.max_dials_per_day）。ここは低下の見張り。

- **数えるのはデータベースの1か所**（`salescall.line_answer_counts`：回線の、日・時間帯ごとの、かけた本数と出てもらえた本数）。
- **落ちたかの判断は `judge` の1か所**（データベースを読まない＝数だけで決まる）：今日の出てもらえた割合を、その回線の前の
  `BASELINE_DAYS` 日の**同じ時間帯の割合**から見込んだ割合と比べる（昼休み・夕方は出てもらいにくいので、時間帯の違いで知らせない）。
  次の3つがそろったときだけ「落ちた」：(1) 今日の本数が設定（line.answer_drop_min_dials）以上 (2) 今日の割合が、見込みの設定の比
  （line.answer_drop_ratio）より下 (3) 見込みどおりの割合なら、偶然では `SIGNIFICANCE`（100回に1回）も起きない下がり方。
  前の日々の本数が `BASELINE_MIN_DIALS` に足りない回線（新しい番号など）は、いつもの割合が分からないので判断しない。
- **落ちたら、その契約者へ知らせる**（1回線1日1回。種類 answer_rate_drop）。**発信は止めない**（お客様に影響が出るものではない＝
  原則6。番号を休ませるか替えるかは、契約者が知らせを見て決める）。呼ぶのは見守り役（契約者ごとの仕事）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from salescall.core import notify, settings, stats
from salescall.core.access import Tx

BASELINE_DAYS = 14          # いつもの割合を見る日数（今日を除く）。曜日の偏りを2回ずつならす
BASELINE_MIN_DIALS = 100    # いつもの割合を決めるのに要る、前の日々の本数
HOUR_MIN_DIALS = 20         # 時間帯ごとの割合を使うのに要る、前の日々のその時間帯の本数（足りなければ、前の日々全体の割合）
SIGNIFICANCE = 0.01         # 見込みどおりなら、この確率でしか起きない下がり方のときだけ知らせる


@dataclass(frozen=True)
class Drop:
    """今日の応答率が落ちた。"""
    dials: int
    answers: int
    observed: float       # 今日の割合
    expected: float       # 前の日々の同じ時間帯から見込んだ割合
    chance: float         # 見込みどおりなら、今日の本数以下しか出てもらえない確率


def judge(rows: list[dict[str, Any]], today: date, *, ratio: float, min_dials: int) -> Drop | None:
    """日・時間帯ごとの数（line_answer_counts の行）から、今日の応答率が落ちたかを決める（1か所）。"""
    now = [r for r in rows if r["day"] == today]
    base = [r for r in rows if today - timedelta(days=BASELINE_DAYS) <= r["day"] < today]
    n, k = sum(r["dials"] for r in now), sum(r["answers"] for r in now)
    base_n = sum(r["dials"] for r in base)
    if n < min_dials or base_n < BASELINE_MIN_DIALS:
        return None
    overall = sum(r["answers"] for r in base) / base_n
    by_hour: dict[int, list[int]] = {}
    for r in base:
        got = by_hour.setdefault(r["hour"], [0, 0])
        got[0] += r["dials"]
        got[1] += r["answers"]
    expected = sum(r["dials"] * (by_hour[r["hour"]][1] / by_hour[r["hour"]][0]
                                 if by_hour.get(r["hour"], [0, 0])[0] >= HOUR_MIN_DIALS else overall) for r in now) / n
    observed = k / n
    if expected <= 0 or observed >= expected * ratio:
        return None
    chance = stats.at_most(k, n, expected)
    return Drop(n, k, observed, expected, chance) if chance < SIGNIFICANCE else None


def check(tx: Tx, tenant_id: UUID, line_id: UUID, *, today: date | None = None) -> Drop | None:
    """その回線の今日（日本時間）を見る（契約者の取引）。"""
    today = today or tx.today()
    rows = tx.all("SELECT * FROM salescall.line_answer_counts(%s, %s, %s)",
                  (line_id, today - timedelta(days=BASELINE_DAYS), today))
    value = lambda key: settings.resolve(tx, key, tenant_id=tenant_id, client_id=None, business_id=None)   # noqa: E731
    return judge(rows, today, ratio=float(value("line.answer_drop_ratio")), min_dials=int(value("line.answer_drop_min_dials")))


def watch(tx: Tx, tenant_id: UUID, *, today: date | None = None) -> dict[UUID, Drop]:
    """その契約者の、使っている回線すべてを見て、落ちた回線を知らせる（見守り役が呼ぶ。1回線1日1回）。落ちた回線を返す。"""
    today = today or tx.today()
    dropped = {}
    for line in tx.all("SELECT line_id, name FROM salescall.lines WHERE retired_at IS NULL ORDER BY line_id"):
        got = check(tx, tenant_id, line["line_id"], today=today)
        if got is None:
            continue
        dropped[line["line_id"]] = got
        notify.emit(tx, "answer_rate_drop", key=f"answer_drop:{line['line_id']}:{today}",
                    summary=f"回線「{line['name']}」で、今日お客様に出てもらえた割合が、いつもの同じ時間帯より大きく下がっています"
                            f"（今日 {got.dials}本中 {got.answers}本＝{got.observed:.0%}・いつもなら約 {got.expected:.0%}）。"
                            "この番号が迷惑電話として扱われているおそれがあります。発信は止めていません。",
                    refs={"line_id": line["line_id"], "dials": got.dials, "answers": got.answers,
                          "observed": round(got.observed, 4), "expected": round(got.expected, 4)})
    return dropped
