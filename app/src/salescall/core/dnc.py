"""発信禁止台帳（Q3「この相手は、かけてはいけない相手か」）。持ち主 CL-2。DESIGN.md 3-6・5-2・Intent.md 原則14。

- 登録するのは、この部品の `register`（依頼主の台帳）と `register_system`（システム共通）だけ（I-12。app/checks が見張る）。
  呼ぶ側：会話の「もうかけないで」（その場で）・通話後の検証の拒否の聞き逃し・判定の番号違い・依頼主の発信禁止の一覧の取込・運営者。
- 番号は番号の正規化（Q1）を通す。そろえられない番号は登録できない（ValueError）。
- **どこまで止めるかを決めるのは `register` の1か所だけ**（2026-10-14 ユーザー決定。DESIGN.md 3-6）：
  相手が断った（`REFUSALS`）ことが、どの通話（試行）で分かったかが渡されたら、
  (b) その行（タスク）の番号すべてを止める（相手が言った別の番号も。番号の一覧は tasks.numbers の1か所）。
  (c) 運営者が依頼主ごとの設定で「管理番号は相手を表す変わらない番号」を「はい」にしていたら（設定 `dnc.client_ref_is_stable_id`）、
      その行の管理番号も止める（次の一覧で、同じ相手が別の番号で入ってきても止まる）。
  番号違い（wrong_party）は、その番号の持ち主が別の相手だったということなので、その番号だけ（行・管理番号へ広げない）。
- 同じ番号・同じ管理番号の有効な登録は1つ（2回目の登録は、前の登録をそのまま返す。何度呼んでも同じ結果）。
- **登録した取引の中で、止めた番号・管理番号のまだ発信していない予約を取り消す**（DESIGN.md 5-2-1 の2）。
- 取り消し（revoke）は人だけ・記録つき・1回だけ（データベースが守る）。自動では取り消さない（Intent.md 10-3）。
- 引くのは `lookup`。門と取込は、`target_policy` を通して引く。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import attempts, settings, tasks
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber, normalize

Reason = Literal["refused_in_call", "refused_found_after", "wrong_party", "manual", "client_list"]
REFUSALS = frozenset({"refused_in_call", "refused_found_after"})   # 相手が断った ⇒ 相手（行）ごと止める


class DncEntryNotFound(Exception):
    """その登録は無い（取り消し済み・取引の依頼主の範囲の外を含む）。"""


def _number(phone: PhoneNumber | str) -> PhoneNumber:
    n = normalize(phone) if isinstance(phone, str) else phone
    if n.e164 is None:
        raise ValueError(f"そろえられない番号は発信禁止に登録できません：{n.reason}")
    return n


def register(tx: Tx, phone: PhoneNumber | str, reason: Reason, *, source_attempt_id: UUID | None = None) -> dict[str, Any]:
    """取引の依頼主の発信禁止台帳に登録する（どこまで広げるかは、この中の1か所で決める）。

    返すもの：entry_id（渡された番号の登録）・reason・numbers（止めた番号）・client_refs（止めた管理番号）・released_claims。
    """
    n = _number(phone)
    numbers, refs = [n.e164], []
    if source_attempt_id is not None:
        a = tx.one(
            "SELECT a.tenant_id, a.client_id, a.task_id, a.phone_e164, t.client_ref, r.business_id"
            " FROM salescall.attempts a"
            " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
            " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
            " WHERE a.attempt_id = %s", (source_attempt_id,))
        if a is None:
            raise attempts.AttemptNotFound(str(source_attempt_id))
        if reason in REFUSALS:
            numbers += [a["phone_e164"], *tasks.numbers(tx, a["task_id"])]
            if a["client_ref"] is not None and settings.resolve(
                    tx, "dnc.client_ref_is_stable_id", tenant_id=a["tenant_id"], client_id=a["client_id"],
                    business_id=a["business_id"]):
                refs.append(a["client_ref"])
    numbers = list(dict.fromkeys(numbers))
    for e164 in numbers:
        tx.execute(
            "INSERT INTO salescall.dnc_entries (tenant_id, client_id, phone_e164, reason, source_attempt_id, registered_by)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())"
            " ON CONFLICT (client_id, phone_e164) WHERE revoked_at IS NULL DO NOTHING",
            (e164, reason, source_attempt_id),
        )
    for ref in refs:
        tx.execute(
            "INSERT INTO salescall.dnc_entries (tenant_id, client_id, client_ref, reason, source_attempt_id, registered_by)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())"
            " ON CONFLICT (client_id, client_ref) WHERE revoked_at IS NULL DO NOTHING",
            (ref, reason, source_attempt_id),
        )
    entry = tx.one("SELECT entry_id, reason FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL",
                   (n.e164,))
    claimed = [r["attempt_id"] for r in tx.all(
        "SELECT a.attempt_id FROM salescall.attempts a"
        " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
        " WHERE a.state = 'claimed' AND (a.phone_e164 = ANY(%s) OR t.client_ref = ANY(%s))", (numbers, refs))]
    return {"entry_id": entry["entry_id"], "reason": entry["reason"], "numbers": numbers, "client_refs": refs,
            "released_claims": attempts.release(tx, claimed)}


def register_system(tx: Tx, phone: PhoneNumber | str, reason: str) -> int:
    """システム共通の発信禁止に登録する（全依頼主に効く）。取り消した予約の数を返す。"""
    n = _number(phone)
    tx.execute(
        "INSERT INTO salescall.system_dnc_entries (phone_e164, reason, registered_by)"
        " VALUES (%s, %s, salescall.cur_actor()) ON CONFLICT (phone_e164) DO NOTHING",
        (n.e164, reason),
    )
    return tx.one("SELECT salescall.release_claims_for_system_dnc(%s) AS n", (n.e164,))["n"]


def lookup(tx: Tx, e164: str, *, client_ref: str | None = None) -> dict[str, Any] | None:
    """その番号（または、その行の管理番号）が発信禁止なら、どの台帳か（client / system / client_ref）と理由。

    管理番号の登録は、設定を「いいえ」に戻しても効く（止める側に倒す。取り消しは人が。DESIGN.md 3-6）。
    """
    row = tx.one("SELECT reason::text AS reason FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL",
                 (e164,))
    if row is not None:
        return {"ledger": "client", "reason": row["reason"]}
    row = tx.one("SELECT reason FROM salescall.system_dnc_entries WHERE phone_e164 = %s", (e164,))
    if row is not None:
        return {"ledger": "system", "reason": row["reason"]}
    if client_ref is not None:
        row = tx.one("SELECT reason::text AS reason FROM salescall.dnc_entries WHERE client_ref = %s AND revoked_at IS NULL",
                     (client_ref,))
        if row is not None:
            return {"ledger": "client_ref", "reason": row["reason"]}
    return None


def revoke(tx: Tx, entry_id: UUID, reason: str) -> None:
    """発信禁止の登録を取り消す（人だけ・理由つき・1回だけ。データベースが守る）。自動では呼ばない（Intent.md 10-3）。
    行ごとに広げて登録したものも、1つずつ取り消す（番号と管理番号は別の登録）。"""
    row = tx.one(
        "UPDATE salescall.dnc_entries SET revoked_at = now(), revoked_by = salescall.cur_actor(), revoke_reason = %s"
        " WHERE entry_id = %s AND revoked_at IS NULL RETURNING entry_id",
        (reason, entry_id),
    )
    if row is None:
        raise DncEntryNotFound(str(entry_id))
