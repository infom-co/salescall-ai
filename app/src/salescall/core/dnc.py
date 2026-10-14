"""発信禁止台帳（Q3「この相手は、かけてはいけない相手か」）。持ち主 CL-2。DESIGN.md 2章・5-2・Intent.md 原則14。

- 登録するのは、この部品の `register`（依頼主の台帳）と `register_system`（システム共通）だけ（I-12。app/checks が見張る）。
  呼ぶ側：会話の「もうかけないで」（その場で）・通話後の検証の拒否の聞き逃し・判定の番号違い・依頼主の発信禁止の一覧の取込・運営者。
- 番号は番号の正規化（Q1）を通す。そろえられない番号は登録できない（ValueError）。
- 同じ番号の有効な登録は1つ（2回目の登録は、前の登録をそのまま返す。何度呼んでも同じ結果）。
- **登録した取引の中で、その番号のまだ発信していない予約を取り消す**（DESIGN.md 5-2-1 の2。登録の直前に取った予約で発信しない）。
- 取り消し（revoke）は人だけ・記録つき・1回だけ（データベースが守る）。自動では取り消さない（Intent.md 10-3）。
- 引くのは `lookup`（依頼主の台帳とシステム共通の両方）。門と取込は、`number_policy` を通して引く。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import attempts
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber, normalize

Reason = Literal["refused_in_call", "refused_found_after", "wrong_party", "manual", "client_list"]


def _number(phone: PhoneNumber | str) -> PhoneNumber:
    n = normalize(phone) if isinstance(phone, str) else phone
    if n.e164 is None:
        raise ValueError(f"そろえられない番号は発信禁止に登録できません：{n.reason}")
    return n


def register(tx: Tx, phone: PhoneNumber | str, reason: Reason, *, source_attempt_id: UUID | None = None) -> dict[str, Any]:
    """取引の依頼主の発信禁止台帳に登録する。登録（または既にある登録）と、取り消した予約の数を返す。"""
    n = _number(phone)
    tx.execute(
        "INSERT INTO salescall.dnc_entries (tenant_id, client_id, phone_e164, reason, source_attempt_id, registered_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())"
        " ON CONFLICT (client_id, phone_e164) WHERE revoked_at IS NULL DO NOTHING",
        (n.e164, reason, source_attempt_id),
    )
    entry = tx.one("SELECT entry_id, reason, registered_at FROM salescall.dnc_entries"
                   " WHERE phone_e164 = %s AND revoked_at IS NULL", (n.e164,))
    claimed = [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE phone_e164 = %s AND state = 'claimed'", (n.e164,))]
    released = attempts.release(tx, claimed)
    return {"entry_id": entry["entry_id"], "reason": entry["reason"], "released_claims": released}


def register_system(tx: Tx, phone: PhoneNumber | str, reason: str) -> int:
    """システム共通の発信禁止に登録する（全依頼主に効く）。取り消した予約の数を返す。"""
    n = _number(phone)
    tx.execute(
        "INSERT INTO salescall.system_dnc_entries (phone_e164, reason, registered_by)"
        " VALUES (%s, %s, salescall.cur_actor()) ON CONFLICT (phone_e164) DO NOTHING",
        (n.e164, reason),
    )
    return tx.one("SELECT salescall.release_claims_for_system_dnc(%s) AS n", (n.e164,))["n"]


def lookup(tx: Tx, e164: str) -> dict[str, Any] | None:
    """その番号が発信禁止なら、どの台帳か（client / system）と理由。そうでなければ None。"""
    row = tx.one("SELECT reason::text AS reason FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL",
                 (e164,))
    if row is not None:
        return {"ledger": "client", "reason": row["reason"]}
    row = tx.one("SELECT reason FROM salescall.system_dnc_entries WHERE phone_e164 = %s", (e164,))
    if row is not None:
        return {"ledger": "system", "reason": row["reason"]}
    return None


class DncEntryNotFound(Exception):
    """その登録は無い（取り消し済み・取引の依頼主の範囲の外を含む）。"""


def revoke(tx: Tx, entry_id: UUID, reason: str) -> None:
    """発信禁止の登録を取り消す（人だけ・理由つき・1回だけ。データベースが守る）。自動では呼ばない（Intent.md 10-3）。"""
    row = tx.one(
        "UPDATE salescall.dnc_entries SET revoked_at = now(), revoked_by = salescall.cur_actor(), revoke_reason = %s"
        " WHERE entry_id = %s AND revoked_at IS NULL RETURNING entry_id",
        (reason, entry_id),
    )
    if row is None:
        raise DncEntryNotFound(str(entry_id))
