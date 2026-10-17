"""発信禁止台帳（Q3「このお客様は、かけてはいけないお客様か」）。持ち主 CL-2。DESIGN.md 3-6・5-2・Intent.md 原則14。

- 登録するのは、この部品の `register`（依頼者の台帳）と `register_contractor`（契約者の共通の台帳。その契約者のすべての依頼者に効く。2026-10-16 ユーザー）だけ（I-12。app/checks が見張る）。
  呼ぶ側：会話の「もうかけないで」（その場で）・通話後の検証の拒否の聞き逃し・判定の番号違い・依頼者の発信禁止の一覧の取込・契約者。
- 番号は番号の正規化（Q1）を通す。そろえられない番号は登録できない（ValueError）。
- **どこまで止めるかを決めるのは `register` の1か所だけ**（2026-10-14 ユーザー決定。DESIGN.md 3-6）：
  お客様が断った（`REFUSALS`）ことが、どの通話（試行）で分かったかが渡されたら、
  (b) その行（タスク）の番号すべてを止める（お客様が言った別の番号も。番号の一覧は tasks.numbers の1か所）。
  (c) 契約者が依頼者ごとの設定で「管理番号はお客様を表す変わらない番号」を「はい」にしていたら（設定 `dnc.client_ref_is_stable_id`）、
      その行の管理番号も止める（次の一覧で、同じお客様が別の番号で入ってきても止まる）。
  番号違い（wrong_party）は、その番号の持ち主が別のお客様だったということなので、その番号だけ（行・管理番号へ広げない）。
- 同じ番号・同じ管理番号の有効な登録は1つ（2回目の登録は、前の登録をそのまま返す。何度呼んでも同じ結果）。
- **登録した取引の中で、止めた番号・管理番号のまだ発信していない予約を取り消す**（DESIGN.md 5-2-1 の2）。
- 取り消し（revoke）は管理者か契約者だけ・記録つき・1回だけ（データベースが守る）。自動では取り消さない（Intent.md 10-3）。
- 引くのは `lookup`。門と取込は、`target_policy` を通して引く。
- **断られたら止める範囲**（`reach`）も、ここの1か所。門は、締めがまだの発信（断られたかがまだ決まっていない）の範囲に入る
  番号・管理番号へかけない（`undecided_reach_overlaps`。止める前にかけてしまう隙を作らない。DESIGN.md 5-2-2）。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import attempts, settings, tasks
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber, normalize

Reason = Literal["refused_in_call", "refused_found_after", "wrong_party", "manual", "client_list"]
REFUSALS = frozenset({"refused_in_call", "refused_found_after"})   # お客様が断った ⇒ お客様（行）ごと止める


class DncEntryNotFound(Exception):
    """その登録は無い（取り消し済み・取引の依頼者の範囲の外を含む）。"""


def _number(phone: PhoneNumber | str) -> PhoneNumber:
    n = normalize(phone) if isinstance(phone, str) else phone
    if n.e164 is None:
        raise ValueError(f"そろえられない番号は発信禁止に登録できません：{n.reason}")
    return n


def register(tx: Tx, phone: PhoneNumber | str, reason: Reason, *, source_attempt_id: UUID | None = None) -> dict[str, Any]:
    """取引の依頼者の発信禁止台帳に登録する（どこまで広げるかは、この中の1か所で決める）。

    返すもの：entry_id（渡された番号の登録）・reason・numbers（止めた番号）・client_refs（止めた管理番号）・released_claims。
    """
    n = _number(phone)
    numbers, refs = [n.e164], []
    if source_attempt_id is not None:
        a = tx.one("SELECT task_id, phone_e164 FROM salescall.attempts WHERE attempt_id = %s", (source_attempt_id,))
        if a is None:
            raise attempts.AttemptNotFound(str(source_attempt_id))
        if reason in REFUSALS:
            row_numbers, ref = reach(tx, a["task_id"])
            numbers += [a["phone_e164"], *row_numbers]
            refs += [] if ref is None else [ref]
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


def reach(tx: Tx, task_id: UUID) -> tuple[list[str], str | None]:
    """その行のお客様が断ったときに止める範囲（広げ方の正本。DESIGN.md 3-6）：(b) その行の番号すべて（お客様が言った別の番号も。
    tasks.numbers の1か所）と、同じ依頼者のほかの行（ほかの一覧の行も）のうち、その行と番号を1つでも共有する行の番号すべて
    （2026-10-17 ユーザー決定：「広げる案でOK。断られた場合は、同じ依頼者、同じ発信番号でかけ直しは絶対にNG」。例：5月の一覧の
    代表番号で断られたら、4月の一覧で同じ代表番号を持つ行の担当者の携帯も止める。広げるのは1段だけ＝共有する行の番号まで）、
    (c) 契約者が依頼者ごとの設定で選んでいれば、その行の管理番号（無ければ None）。依頼者をまたいでは広げない（取引の依頼者の分
    しか見えない。別の依頼者はサービスも別なので、断られるとは限らない＝2026-10-17 ユーザー）。"""
    t = tx.one("SELECT t.tenant_id, t.client_id, t.client_ref, r.business_id FROM salescall.tasks t"
               " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id WHERE t.task_id = %s",
               (task_id,))
    if t is None:
        raise tasks.TaskNotFound(str(task_id))
    ref = None
    if t["client_ref"] is not None and settings.resolve(tx, "dnc.client_ref_is_stable_id", tenant_id=t["tenant_id"],
                                                        client_id=t["client_id"], business_id=t["business_id"]):
        ref = t["client_ref"]
    own = tasks.numbers(tx, task_id)
    shared = [r["phone_e164"] for r in tx.all(
        "SELECT DISTINCT o.phone_e164 FROM salescall.task_numbers o"
        " WHERE o.task_id IN (SELECT s.task_id FROM salescall.task_numbers s WHERE s.phone_e164 = ANY(%s) AND s.task_id <> %s)"
        " ORDER BY o.phone_e164", (own, task_id))]
    return list(dict.fromkeys(own + shared)), ref


def undecided_reach_overlaps(tx: Tx, task_id: UUID) -> bool:
    """この行と同じお客様の、ほかの行の発信で、断られたかがまだ決まっていないもの（予約・通話中・締め前・判定できないで
    判定待ち。データベースの1か所 salescall.refusal_undecided）があるか。取引の依頼者の分（発信禁止は依頼者ごと）。
    「同じお客様」＝その発信が断られていたら、この行の番号（または管理番号）が止まる（止める範囲は `reach` の1か所）。

    止めるのは締めの処理（判定のあと）なので、決まる前に同じお客様へかけると、断ったお客様にまたかけうる。
    2026-10-16 の見直しで直した：(1) かける番号だけでなく、この行の番号すべてで見る（両方向）(2) 予約中も見る
    (3) 判定できないの結果も「まだ決まっていない」。2026-10-17 の見直しで直した：(4) 期限の切れた予約（取り消されないまま
    残ったもの）は見ない (5) 重なりは「相手が断ったら、この行の番号が止まるか」で見る（前は両方の範囲の重なりで見て、
    2段離れた行まで待たせた）(6) 調べる行を、この行の範囲の番号を持つ行・同じ管理番号の行に絞る（試行が増えても遅くしない）。"""
    t = tx.one("SELECT client_ref FROM salescall.tasks WHERE task_id = %s", (task_id,))
    if t is None:
        raise tasks.TaskNotFound(str(task_id))
    mine = set(tasks.numbers(tx, task_id))
    near, _ = reach(tx, task_id)   # この行の番号と、番号を共有する行の番号（相手の範囲が、この行の番号に届きうる行の番号）
    for p in tx.all(
            "SELECT DISTINCT a.task_id FROM salescall.attempts a"
            " WHERE a.task_id <> %s AND a.state <> 'released'"
            "   AND NOT (a.state = 'claimed' AND a.claim_expires_at <= now())"
            "   AND (EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = a.task_id AND n.phone_e164 = ANY(%s))"
            "        OR (%s::text IS NOT NULL AND EXISTS (SELECT 1 FROM salescall.tasks o WHERE o.task_id = a.task_id"
            "                                             AND o.client_ref = %s)))"
            "   AND salescall.refusal_undecided(a.attempt_id)",
            (task_id, list(near), t["client_ref"], t["client_ref"])):
        numbers, ref = reach(tx, p["task_id"])
        if mine.intersection(numbers) or (ref is not None and ref == t["client_ref"]):
            return True
    return False


def register_contractor(tx: Tx, phone: PhoneNumber | str, reason: str) -> dict[str, Any]:
    """取引の契約者の、共通の発信禁止に登録する（その契約者のすべての依頼者に効く。2026-10-16 ユーザー：「システム共通の
    発信禁止　契約者」）。登録できるのは管理者か契約者だけ（データベースが守る）。登録（entry_id）と、取り消した予約の数を返す。"""
    n = _number(phone)
    tx.execute(
        "INSERT INTO salescall.contractor_dnc_entries (tenant_id, phone_e164, reason, registered_by)"
        " VALUES (salescall.cur_tenant(), %s, %s, salescall.cur_actor())"
        " ON CONFLICT (tenant_id, phone_e164) WHERE revoked_at IS NULL DO NOTHING",
        (n.e164, reason),
    )
    entry = tx.one("SELECT entry_id FROM salescall.contractor_dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL",
                   (n.e164,))
    return {"entry_id": entry["entry_id"],
            "released_claims": tx.one("SELECT salescall.release_claims_for_contractor_dnc(%s) AS n", (n.e164,))["n"]}


def revoke_contractor(tx: Tx, entry_id: UUID, reason: str) -> None:
    """契約者の共通の発信禁止の登録を取り消す（管理者か契約者だけ・理由つき・1回だけ。データベースが守る）。"""
    row = tx.one(
        "UPDATE salescall.contractor_dnc_entries SET revoked_at = now(), revoked_by = salescall.cur_actor(),"
        " revoke_reason = %s WHERE entry_id = %s AND revoked_at IS NULL RETURNING entry_id", (reason, entry_id))
    if row is None:
        raise DncEntryNotFound(str(entry_id))


def lookup(tx: Tx, e164: str, *, client_ref: str | None = None) -> dict[str, Any] | None:
    """その番号（または、その行の管理番号）が発信禁止なら、どの台帳か（client / contractor / client_ref）と理由。

    管理番号の登録は、設定を「いいえ」に戻しても効く（止める側に倒す。取り消しは契約者が。DESIGN.md 3-6）。
    """
    row = tx.one("SELECT reason::text AS reason FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL",
                 (e164,))
    if row is not None:
        return {"ledger": "client", "reason": row["reason"]}
    row = tx.one("SELECT reason FROM salescall.contractor_dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL", (e164,))
    if row is not None:
        return {"ledger": "contractor", "reason": row["reason"]}
    if client_ref is not None:
        row = tx.one("SELECT reason::text AS reason FROM salescall.dnc_entries WHERE client_ref = %s AND revoked_at IS NULL",
                     (client_ref,))
        if row is not None:
            return {"ledger": "client_ref", "reason": row["reason"]}
    return None


def revoke(tx: Tx, entry_id: UUID, reason: str) -> None:
    """発信禁止の登録を取り消す（管理者か契約者だけ・理由つき・1回だけ。データベースが守る）。自動では呼ばない（Intent.md 10-3）。
    行ごとに広げて登録したものも、1つずつ取り消す（番号と管理番号は別の登録）。"""
    row = tx.one(
        "UPDATE salescall.dnc_entries SET revoked_at = now(), revoked_by = salescall.cur_actor(), revoke_reason = %s"
        " WHERE entry_id = %s AND revoked_at IS NULL RETURNING entry_id",
        (reason, entry_id),
    )
    if row is None:
        raise DncEntryNotFound(str(entry_id))
