"""試行（1回の発信）の状態を移す処理（DESIGN.md 4-2）。持ち主 CL-2。

試行の行を書き換えるのは、この部品だけ（I-12・I-27。app/checks が見張る）。呼ぶ側と、使ってよい処理：
- 回線の部品（呼び出し中・応答・会話中・締め・終わり）と復旧（結果不明）  ⇒ `move`
- 発信の出口（予約 → 発信）                                              ⇒ `start_dialing`
- 門（期限切れの予約を戻す・門の前提が変わった予約を取り消す）           ⇒ `release`
- 通話後の検証（Q21。待ち → 済み／確かめられなかった）                    ⇒ `record_verification`
- 締めの処理（core/settle.py。判定が決めた切れ方を1回だけ）                ⇒ `record_disconnect`
移ってよい組・期限・時刻・検証の状態の決まりは、データベースが持つ（ここで2つ目の決まりを作らない）。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import number_recheck
from salescall.core.access import Tx

ACTIVE_STATES = ("claimed", "dialing", "ringing", "answered", "in_conversation", "ending")

# 回線の部品と復旧が移してよい先（予約 → 発信・予約の取り消しは、それぞれ専用の処理だけ）
LineState = Literal["ringing", "answered", "in_conversation", "ending", "ended",
                    "busy", "no_answer", "call_failed", "dropped", "technical_error"]
HangupSide = Literal["callee", "line", "unknown"]


class AttemptNotFound(Exception):
    """その試行は無い（または、取引の依頼主の範囲の外）。"""


def move(tx: Tx, attempt_id: UUID, to_state: LineState, *, hangup_side: HangupSide | None = None) -> dict[str, Any]:
    """回線の部品・復旧が、試行の状態を移す。移れない組なら、データベースが拒む（Rejected）。"""
    if to_state in ("claimed", "dialing", "released"):
        raise ValueError(f"{to_state} へは、この処理では移せません（予約は門、発信は出口、取り消しは門の処理）")
    row = tx.one(
        "UPDATE salescall.attempts SET state = %s, hangup_side = %s WHERE attempt_id = %s"
        " RETURNING attempt_id, state, verification, hangup_side, ended_at",
        (to_state, hangup_side, attempt_id),
    )
    if row is None:
        raise AttemptNotFound(str(attempt_id))
    return row


def start_dialing(tx: Tx, attempt_id: UUID) -> bool:
    """発信の出口だけが呼ぶ。予約が「まだ予約のまま・期限内」なら発信中にし True。それ以外は False（発信しない）。

    確かめと書き換えを1つの書き換えで行う（間に、ほかの処理が割り込めない。DESIGN.md 5-3）。
    """
    row = tx.one(
        "UPDATE salescall.attempts SET state = 'dialing'"
        " WHERE attempt_id = %s AND state = 'claimed' AND claim_expires_at > now() RETURNING attempt_id",
        (attempt_id,),
    )
    return row is not None


def release(tx: Tx, attempt_ids: list[UUID]) -> int:
    """門だけが呼ぶ。まだ発信していない予約を取り消す（期限切れ・門の前提が変わった。DESIGN.md 5-2-1 の2・3）。"""
    if not attempt_ids:
        return 0
    return tx.execute(
        "UPDATE salescall.attempts SET state = 'released' WHERE attempt_id = ANY(%s) AND state = 'claimed'",
        (list(attempt_ids),),
    ).rowcount


class RecheckMissing(Exception):
    """番号の確かめ（DESIGN.md 3-5）が要る通話なのに、記録が無い。"""


def record_verification(tx: Tx, attempt_id: UUID, result: Literal["done", "failed"]) -> None:
    """通話後の検証（Q21）だけが呼ぶ。「待ち」から「済み」か「確かめられなかった」へ。
    相手が言った番号へかけて番号違いだった通話は、番号の確かめ（core/number_recheck.py）を記録してからでないと済ませられない
    （付け合わせられなかったときも cannot_check として記録する）。"""
    if number_recheck.needed(tx, attempt_id) and number_recheck.load(tx, attempt_id) is None:
        raise RecheckMissing("番号違いだった番号の確かめ（証拠との付け合わせ）を、先に記録してください")
    row = tx.one(
        "UPDATE salescall.attempts SET verification = %s WHERE attempt_id = %s RETURNING attempt_id",
        (result, attempt_id),
    )
    if row is None:
        raise AttemptNotFound(str(attempt_id))


def record_disconnect(tx: Tx, attempt_id: UUID, disconnect: Literal["normal", "refused", "interrupted"]) -> None:
    """締めの処理（core/settle.py）だけが呼ぶ。判定が決めた切れ方（DESIGN.md 7-1c）を、終わった試行に1回だけ記録する。"""
    row = tx.one("UPDATE salescall.attempts SET disconnect = %s WHERE attempt_id = %s RETURNING attempt_id",
                 (disconnect, attempt_id))
    if row is None:
        raise AttemptNotFound(str(attempt_id))
