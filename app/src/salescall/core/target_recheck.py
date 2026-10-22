"""目的のお客様の確かめ直し（意味の判定なしで繋いだ通話。DESIGN.md 6-12 の (e)・CL-1 の連絡帳 4-25。0108）。持ち主 CL-2。

目的のお客様に繋ぐのは「ご本人」の読みが2つそろったときだけ。ただし判定が止まっていたときは、確かめへの「はい」をAIの読みだけで
受け取って繋ぐ（止まらない）。その通話は、通話後の検証（Q21。CL-1）が、繋いだ発話の録音を別の耳と判定で確かめ直す。

- 要るのは、会話の報告が `target_unverified` の通話だけ（`needed`。判断は core/call_report.py の1か所）。
- 結果をここで記録する（`record`。書くのはここだけ。変更不可）：target＝目的のお客様だった／not_target＝別の方だった／
  cannot_check＝確かめられなかった（録音が届かない など）。
- **target 以外なら、その通話で取った答えを全部、前の通話の答え（後の通話が上書きしたときに残したもの。目的のお客様だと確かめられ
  なかった通話のもの以外）に戻し、無ければ「-」にする**（データベースが記録と同じ取引でする＝0108 の仕掛けの1か所）。別の方の答え・
  確かめていない答えを依頼者へ渡さない（依頼者へ渡るのは、確かめた値か「-」だけ）。答えを書けるのは、そのタスクのいちばん新しい
  通話の番号でだけ（前の通話の番号のまま上書きすると、戻す答えを取り違える。データベースが守る）。
- 要る通話は、記録が無いと通話後の検証を済ませられない（core/attempts.py の `record_verification` が守る）。
  ⇒ 締めの処理は必ず記録を読める（`unconfirmed`：目的のお客様と話せたと数えない＝約束の日のお詫びの印を外さない）。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from salescall.core import call_report
from salescall.core.access import Tx

Outcome = Literal["target", "not_target", "cannot_check"]
OUTCOMES = ("target", "not_target", "cannot_check")
UNCONFIRMED = tuple(o for o in OUTCOMES if o != "target")   # 目的のお客様だと確かめられなかった（target 以外。0108 の仕掛けも同じ見方）


class RecheckInvalid(Exception):
    """記録できない確かめ直し（理由は日本語）。"""


def needed(tx: Tx, attempt_id: UUID) -> bool:
    """確かめ直しが要る通話か（判定を使う通話で、判定なしで繋いだ・繋いだあとの判定が「ご本人」でない）。"""
    report = call_report.load(tx, attempt_id)
    return report is not None and report.target_unverified


def missing(tx: Tx, attempt_id: UUID) -> bool:
    """確かめ直しが要るのに、まだ記録が無い（通話後の検証を済ませない・締めない・判定し直さない。判断はこの1か所）。
    録音から作り直した報告（call_report.save_rebuilt）に判定なしの繋ぎが入ったときも、ここで分かる。"""
    return needed(tx, attempt_id) and load(tx, attempt_id) is None


def record(tx: Tx, attempt_id: UUID, outcome: Outcome, *, evidence: str, checked_by: str) -> None:
    """確かめ直した結果を記録する（通話後の検証 Q21 だけが呼ぶ。1回だけ）。target 以外なら、その通話の答えは、前の通話の答え
    （目的のお客様だと確かめられなかった通話のもの以外）に戻り、無ければ「-」になる（データベースの1か所＝0108）。"""
    if not needed(tx, attempt_id):
        raise RecheckInvalid("目的のお客様の確かめ直しが要る通話ではありません（判定なしで繋いだ通話だけ）")
    if outcome not in OUTCOMES:
        raise RecheckInvalid(f"結果は {OUTCOMES} のどれかです（{outcome!r}）")
    if not evidence or not evidence.strip():
        raise RecheckInvalid("何で確かめたか（証拠）が要ります")
    tx.execute(
        "INSERT INTO salescall.target_rechecks (tenant_id, client_id, attempt_id, outcome, evidence, checked_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s)",
        (attempt_id, outcome, evidence, checked_by))


def load(tx: Tx, attempt_id: UUID) -> str | None:
    """記録した結果（無ければ None）。"""
    row = tx.one("SELECT outcome FROM salescall.target_rechecks WHERE attempt_id = %s", (attempt_id,))
    return None if row is None else row["outcome"]


def unconfirmed(tx: Tx, attempt_id: UUID) -> bool:
    """確かめ直して、目的のお客様だと確かめられなかった通話か（別の方だった・確かめられなかった）。"""
    return load(tx, attempt_id) in UNCONFIRMED
