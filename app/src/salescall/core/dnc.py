"""発信禁止台帳（Q3「このお客様は、かけてはいけないお客様か」）。持ち主 CL-2。DESIGN.md 3-6・5-2・Intent.md 原則14。

- 登録するのは、この部品の `register`（依頼者の台帳）と `register_contractor`（契約者の共通の台帳。その契約者のすべての依頼者に効く。2026-10-16 ユーザー）だけ（I-12。app/checks が見張る）。
  呼ぶ側：会話の「もうかけないで」（その場で＝`register_declared`。広げるのは締め）・通話後の検証の拒否の聞き逃し・判定の番号違い・発信禁止の一覧の取込（core/dnc_import.py。
  依頼者の一覧は理由 client_list・取込を指す）・契約者。
- 番号は番号の正規化（Q1）を通す。そろえられない番号は登録できない（ValueError）。
- **どこまで止めるかを決めるのは `stop_set` の1か所だけ**（`register` も、一覧の取込のプレビューも、これを呼ぶ。2026-10-14 ユーザー決定。DESIGN.md 3-6）：
  お客様ごとに止める理由（`CUSTOMER_WIDE`）のとき、そのお客様の行（どの通話で分かったか／依頼者の一覧なら、その番号・管理番号を
  持つ行）について、
  (b) その行（タスク）の番号すべてを止める（お客様が言った別の番号も。番号の一覧は tasks.numbers の1か所）。
  (c) 契約者が依頼者ごとの設定で「管理番号はお客様を表す変わらない番号」を「はい」にしていたら（設定 `dnc.client_ref_is_stable_id`）、
      その行の管理番号も止める（次の一覧で、同じお客様が別の番号で入ってきても止まる）。
  番号違い（wrong_party）は、その番号の持ち主が別のお客様だったということなので、その番号だけ（行・管理番号へ広げない）。
  FAX の回線（fax_line）も、その番号だけ（お客様に届かない番号。Intent.md 9-4「その番号は以後かけない」）。
- 同じ番号・同じ管理番号の有効な登録は1つ（2回目の登録は、前の登録をそのまま返す。何度呼んでも同じ結果）。
- **登録した取引の中で、止めた番号・管理番号のまだ発信していない予約を取り消す**（DESIGN.md 5-2-1 の2）。
- 取り消し（revoke）は管理者か契約者だけ・記録つき・1回だけ（データベースが守る）。自動では取り消さない（Intent.md 10-3）。
  依頼者からの正式な求めのときだけの操作。1件ずつ（`revoke`）と、出どころの通話ごとにまとめて（`revoke_source`。断りで広げた登録を
  まとめて取り消す）の2つの入口があり、どちらも `_revoke` の1か所を通る。取り消したあと（2026-10-30 管理者のご決定 (7)）：その通話から
  入れた発信禁止がすべて取り消されたら、その通話の決定（断り・番号違い・FAX）を報告から外して締め直す（core/approval.withdraw_revoked。
  その発信禁止で終えた行は予定に戻り、依頼者へ渡す結果の「営業拒否」も外れる）。取り消した番号・管理番号で門が終えたほかの行は、
  終える前の予定に戻す（core/next_action.reopen。まだほかの登録で止まっていれば、門がまた終える）。
- 通話の途中の「もうかけないで」の申告（`register_declared`）は、検証モードの業務では台帳へ入れない（契約者が承認を押した場合だけ
  営業拒否と確定＝2026-10-30 (6)。承認の部分 approval.decides_on_spot が答える）。
- 引くのは `lookup`。門と取込は、`target_policy` を通して引く。
- **断られたら止める範囲**（`reach`）も、ここの1か所。門は、締めがまだの発信（断られたかがまだ決まっていない）の範囲に入る
  番号・管理番号へかけない（`undecided_reach`。止める前にかけてしまう隙を作らない。DESIGN.md 5-2-2）。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import approval, attempts, next_action, settings, tasks
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber, normalize

Reason = Literal["refused_in_call", "refused_unclear_checked", "wrong_party", "fax_line", "manual", "client_list"]
# お客様が断った：通話の中ではっきり断られた（refused_in_call）・切れ方の分からない通話のあとの確かめのかけ直しが鳴ったのに出なかった
# ＝営業拒否とみなす（refused_unclear_checked。2026-10-30 管理者の決定。広げ方は営業拒否と同じ）。⛔ 2026-10-30 に外した：
# refused_found_after（通話後の検証で見つけた断り。書く入口がどこにも無かった。あとから見つける道は録音から作り直した報告の1本）
REFUSALS = frozenset({"refused_in_call", "refused_unclear_checked"})
# お客様（行）ごと止める理由：お客様が断った・依頼者の一覧（依頼者が受けた、お客様の「かけないで」。Intent.md 原則14「止めるのは
# お客様ごと」。誤って広げても「かけない」側の誤りで害は無い＝Intent.md 10-3）
CUSTOMER_WIDE = REFUSALS | {"client_list"}


class DncEntryNotFound(Exception):
    """その登録は無い（取り消し済み・取引の依頼者の範囲の外を含む）。"""


def _number(phone: PhoneNumber | str) -> PhoneNumber:
    n = normalize(phone) if isinstance(phone, str) else phone
    if n.e164 is None:
        raise ValueError(f"そろえられない番号は発信禁止に登録できません：{n.reason}")
    return n


def stop_set(tx: Tx, phone: PhoneNumber | str | None, reason: Reason, *, source_attempt_id: UUID | None = None,
             client_ref: str | None = None, in_call: bool = False) -> tuple[list[str], list[str]]:
    """その登録で止める番号と管理番号（**広げ方の正本**。書かない。`register` と、一覧の取込のプレビューが呼ぶ）。
    番号違い・FAX はその番号だけ。お客様ごとに止める理由（`CUSTOMER_WIDE`）は、そのお客様の行それぞれの `reach` まで広げる：
    断ったお客様の行＝どの通話で分かったか（source_attempt_id）、依頼者の一覧のお客様の行＝その番号か管理番号を持つ行（取引の
    依頼者の行だけ。依頼者をまたいでは広げない）。
    in_call＝通話の途中の申告（`register_declared`）：まだ判定の前で、断ったのが行のお客様か（番号違いの方の断りでないか）が
    決まっていないので、その番号だけ（広げるのは締め）。"""
    if in_call and (reason != "refused_in_call" or source_attempt_id is None or phone is None):
        raise ValueError("通話の途中の申告は、その通話の番号を、理由 refused_in_call で止めるだけです")
    if phone is None and client_ref is None:
        raise ValueError("番号か管理番号のどちらかが要ります")
    if client_ref is not None and reason not in ("client_list", "manual"):
        raise ValueError(f"管理番号を直接止めるのは、依頼者の一覧か契約者の登録だけです（{reason}）")
    numbers = [] if phone is None else [_number(phone).e164]
    refs = [] if client_ref is None else [client_ref.strip()]
    rows: list[UUID] = []
    if source_attempt_id is not None:
        a = tx.one("SELECT task_id, phone_e164 FROM salescall.attempts WHERE attempt_id = %s", (source_attempt_id,))
        if a is None:
            raise attempts.AttemptNotFound(str(source_attempt_id))
        if reason in CUSTOMER_WIDE and not in_call:
            numbers.append(a["phone_e164"])
            rows.append(a["task_id"])
    if reason == "client_list":
        rows += [r["task_id"] for r in tx.all(
            "SELECT DISTINCT task_id FROM salescall.task_numbers WHERE phone_e164 = ANY(%s)"
            " UNION SELECT task_id FROM salescall.tasks WHERE client_ref = ANY(%s) ORDER BY 1", (numbers, refs))]
    for task_id in dict.fromkeys(rows):
        row_numbers, ref = reach(tx, task_id)
        numbers += row_numbers
        refs += [] if ref is None else [ref]
    return list(dict.fromkeys(numbers)), list(dict.fromkeys(refs))


def register(tx: Tx, phone: PhoneNumber | str | None, reason: Reason, *, source_attempt_id: UUID | None = None,
             client_ref: str | None = None, import_id: UUID | None = None, in_call: bool = False) -> dict[str, Any]:
    """取引の依頼者の発信禁止台帳に登録する（どこまで広げるかは `stop_set` の1か所で決める）。import_id＝一覧の取込から入れるとき
    （理由 client_list は必ず取込から。データベースが守る）。

    返すもの：entry_id（渡された番号の登録。番号が無ければ管理番号の登録）・reason・numbers（止めた番号）・client_refs（止めた
    管理番号）・released_claims。
    """
    numbers, refs = stop_set(tx, phone, reason, source_attempt_id=source_attempt_id, client_ref=client_ref, in_call=in_call)
    for e164 in numbers:
        tx.execute(
            "INSERT INTO salescall.dnc_entries (tenant_id, client_id, phone_e164, reason, source_attempt_id, registered_by,"
            "                                   import_id)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor(), %s)"
            " ON CONFLICT (client_id, phone_e164) WHERE revoked_at IS NULL DO NOTHING",
            (e164, reason, source_attempt_id, import_id),
        )
    for ref in refs:
        tx.execute(
            "INSERT INTO salescall.dnc_entries (tenant_id, client_id, client_ref, reason, source_attempt_id, registered_by,"
            "                                   import_id)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor(), %s)"
            " ON CONFLICT (client_id, client_ref) WHERE revoked_at IS NULL DO NOTHING",
            (ref, reason, source_attempt_id, import_id),
        )
    entry = (tx.one("SELECT entry_id, reason FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL",
                    (_number(phone).e164,)) if phone is not None else
             tx.one("SELECT entry_id, reason FROM salescall.dnc_entries WHERE client_ref = %s AND revoked_at IS NULL",
                    (client_ref.strip(),)))
    claimed = [r["attempt_id"] for r in tx.all(
        "SELECT a.attempt_id FROM salescall.attempts a"
        " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
        " WHERE a.state = 'claimed' AND (a.phone_e164 = ANY(%s) OR t.client_ref = ANY(%s))", (numbers, refs))]
    return {"entry_id": entry["entry_id"], "reason": entry["reason"], "numbers": numbers, "client_refs": refs,
            "released_claims": attempts.release(tx, claimed)}


def register_declared(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """通話の途中の「もうかけないで」の申告を、**その場で**台帳へ（原則14。CL-1 の連絡帳 4-22。会話の担当が申告を聞いた
    ときに呼ぶ＝書き込む入口 core/conversation_io.py）。根拠の発話は、会話の報告の通話の事実（do_not_call）に残る。

    止めるのは、その通話でかけた番号だけ（理由 refused_in_call・どの通話かつき）：その番号の相手が断ったことは、相手が誰でも
    確か。行の番号すべて・管理番号へ広げるのは締め（判定のあと。判定が「発信の拒否」のとき）：相手が番号違いの方だったなら、
    行のお客様の断りではないので広げない（判定の (1)）。締めまでは、門が、締めがまだの発信の範囲へかけない
    （`undecided_reach`）ので、広げるのを締めまで待っても、かけてしまう隙は無い。
    ⇒ 会話の処理が報告を書く前に落ちても、申告はもう台帳にある。何度呼んでも同じ（2回目は前の登録のまま）。
    会話の側は、会話AIの報告と2つ目の判定（意味の判定）がそろったときだけ呼ぶ（計画書 9-5 の 7。2026-10-30 管理者のご決定 (6)。
    CL-2 の連絡帳 4-168）。検証モードの業務では台帳へ入れない（契約者が承認を押した場合だけ営業拒否と確定。報告の事実 do_not_call から
    締めが保留にする）：entry_id は None、held は True。"""
    a = tx.one("SELECT phone_e164, task_id, salescall.reached_answer(attempt_id) AS reached FROM salescall.attempts"
               " WHERE attempt_id = %s", (attempt_id,))
    if a is None:
        raise attempts.AttemptNotFound(str(attempt_id))
    if not a["reached"]:
        raise ValueError("お客様が出ていない通話には、申告はありません")
    if not approval.decides_on_spot(tx, a["task_id"]):
        return {"entry_id": None, "reason": "refused_in_call", "numbers": [], "client_refs": [], "released_claims": 0,
                "held": True}
    return {**register(tx, a["phone_e164"], "refused_in_call", source_attempt_id=attempt_id, in_call=True), "held": False}


def refused_in(tx: Tx, attempt_id: UUID, reason: Reason) -> bool:
    """その通話の拒否が、台帳に入っているか（1か所。refused_in_call＝通話の途中の申告）。取り消した登録は数えない（取り消し＝契約者が誤りと確かめた。数えると、判定し直しで「発信の拒否」に
    戻り、取り消した番号を締めがまた止めた）。"""
    return tx.one("SELECT 1 FROM salescall.dnc_entries WHERE source_attempt_id = %s AND reason = %s"
                  " AND revoked_at IS NULL LIMIT 1", (attempt_id, reason)) is not None


def active(tx: Tx, numbers: list[str], refs: list[str], *, contractor: bool = False) -> tuple[set[str], set[str]]:
    """そのうち、もう止まっている（取り消されていない登録がある）番号と管理番号。contractor＝契約者の共通の台帳で見る
    （依頼者の台帳と契約者の台帳は別の登録。片方にあっても、もう片方の登録は要る）。"""
    if contractor:
        return {r["p"] for r in tx.all("SELECT phone_e164 AS p FROM salescall.contractor_dnc_entries"
                                       " WHERE revoked_at IS NULL AND phone_e164 = ANY(%s)", (numbers,))}, set()
    return ({r["p"] for r in tx.all("SELECT phone_e164 AS p FROM salescall.dnc_entries"
                                    " WHERE revoked_at IS NULL AND phone_e164 = ANY(%s)", (numbers,))},
            {r["r"] for r in tx.all("SELECT client_ref AS r FROM salescall.dnc_entries"
                                    " WHERE revoked_at IS NULL AND client_ref = ANY(%s)", (refs,))})


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


UndecidedKind = Literal["unsettled", "decision_pending"]


def undecided_reach(tx: Tx, task_id: UUID) -> UndecidedKind | None:
    """この行と同じお客様の、ほかの行の発信で、断られたかがまだ決まっていないもの（予約・通話中・締め前・判定できないで
    判定待ち・取り消せない決定がまだ決まっていない。データベースの1か所 salescall.refusal_undecided）があるか。取引の依頼者の分
    （発信禁止は依頼者ごと）。あれば、その種類（decision_pending＝取り消せない決定がまだ決まっていない＝データベースの
    salescall.decision_pending の1か所：切れ方の分からない通話の確かめのかけ直し・2つ目の判定とそろわず録音の確かめ・検証モードの契約者の
    承認を待っている。2026-10-30 管理者の決定：決まるまでのあいだに、断ったかもしれない会社へほかの行からかけない／unsettled＝そのほか）。
    無ければ None。門の理由の名前は gate の1か所（`gate.RELATED_REASONS`）。
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
    found: UndecidedKind | None = None
    for p in tx.all(
            "SELECT a.task_id, bool_or(salescall.decision_pending(a.attempt_id)) AS pending FROM salescall.attempts a"
            " WHERE a.task_id <> %s"
            "   AND (salescall.is_dialed(a.state) OR salescall.is_live(a.state, a.claim_expires_at))"   # かけた・生きている予約
            "   AND (EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = a.task_id AND n.phone_e164 = ANY(%s))"
            "        OR (%s::text IS NOT NULL AND EXISTS (SELECT 1 FROM salescall.tasks o WHERE o.task_id = a.task_id"
            "                                             AND o.client_ref = %s)))"
            "   AND salescall.refusal_undecided(a.attempt_id)"
            " GROUP BY a.task_id ORDER BY a.task_id",
            (task_id, list(near), t["client_ref"], t["client_ref"])):
        numbers, ref = reach(tx, p["task_id"])
        if mine.intersection(numbers) or (ref is not None and ref == t["client_ref"]):
            if p["pending"]:
                return "decision_pending"
            found = "unsettled"
    return found


def register_contractor(tx: Tx, phone: PhoneNumber | str, reason: str, *, import_id: UUID | None = None) -> dict[str, Any]:
    """取引の契約者の、共通の発信禁止に登録する（その契約者のすべての依頼者に効く。2026-10-16 ユーザー：「システム共通の
    発信禁止　契約者」）。登録できるのは管理者か契約者だけ（データベースが守る）。その番号だけを止める（お客様の行は依頼者ごとに
    あり、依頼者をまたいで広げない）。import_id＝契約者の一覧の取込から入れるとき。登録（entry_id）と、取り消した予約の数を返す。"""
    n = _number(phone)
    tx.execute(
        "INSERT INTO salescall.contractor_dnc_entries (tenant_id, phone_e164, reason, registered_by, import_id)"
        " VALUES (salescall.cur_tenant(), %s, %s, salescall.cur_actor(), %s)"
        " ON CONFLICT (tenant_id, phone_e164) WHERE revoked_at IS NULL DO NOTHING",
        (n.e164, reason, import_id),
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


def revoke(tx: Tx, entry_id: UUID, reason: str) -> dict[str, Any]:
    """発信禁止の登録を1件取り消す（管理者か契約者だけ・理由つき・1回だけ。データベースが守る）。依頼者からの正式な求めのときだけ呼ぶ
    （自動では呼ばない。Intent.md 10-3）。取り消したあとは `_revoke` の1か所（締め直し・行を予定に戻す）。"""
    return _revoke(tx, [entry_id], reason)


def revoke_source(tx: Tx, attempt_id: UUID, reason: str) -> dict[str, Any]:
    """その通話から入れた発信禁止（断りで行の番号・管理番号へ広げた登録も）を、まとめて取り消す（2026-10-30 管理者のご決定 (7)。⛔ 前は
    広げた登録を1件ずつしか取り消せなかった）。呼ぶ人・守りは `revoke` と同じ。取り消す登録が無ければ DncEntryNotFound。"""
    ids = [r["entry_id"] for r in tx.all("SELECT entry_id FROM salescall.dnc_entries WHERE source_attempt_id = %s AND revoked_at IS NULL"
                                         " ORDER BY entry_id", (attempt_id,))]
    if not ids:
        raise DncEntryNotFound(f"その通話から入れた、取り消されていない発信禁止はありません：{attempt_id}")
    return _revoke(tx, ids, reason)


# 発信禁止の理由 → その通話の報告から外す決定の中身（call_report.DECISION_PARTS。取り消したときに締め直す＝`_revoke`）
REVOKED_PARTS = {"refused_in_call": "refusal", "refused_unclear_checked": "refusal", "wrong_party": "wrong_party", "fax_line": "fax"}


def _revoke(tx: Tx, entry_ids: list[UUID], reason: str) -> dict[str, Any]:
    """取り消しの1か所（2026-10-30 管理者のご決定 (7)「案A」：誤って入れた発信禁止を取り消したら、その発信禁止で終えた行を予定に戻し、
    依頼者へ渡す結果の「営業拒否」も外す）：
    (1) 登録を取り消す（データベースが、管理者か契約者だけ・1回だけを守る）
    (2) 出どころの通話から入れた登録が、もう1つも残っていなければ、その通話の決定（断り・番号違い・FAX）を報告から外して締め直す
        （core/approval.withdraw_revoked → 締めの処理 settle.resettle。結果の版を足し直し、その行が終わっていれば、ふつうの次の動きへ）。
        一部だけを取り消したときは、断りそのものは誤りではない（ほかの番号は止めたまま）ので、締め直さない
    (3) 取り消した番号・管理番号を持つ行で、門が発信禁止で終えた行を、終える前の予定に戻す（core/next_action.reopen）
    取り消した登録・締め直した通話・予定に戻した行を返す。"""
    rows = []
    for entry_id in entry_ids:
        row = tx.one(
            "UPDATE salescall.dnc_entries SET revoked_at = now(), revoked_by = salescall.cur_actor(), revoke_reason = %s"
            " WHERE entry_id = %s AND revoked_at IS NULL RETURNING entry_id, phone_e164, client_ref, reason::text AS reason,"
            " source_attempt_id", (reason, entry_id))
        if row is None:
            raise DncEntryNotFound(str(entry_id))
        rows.append(row)
    resettled = []
    for source in dict.fromkeys(r["source_attempt_id"] for r in rows if r["source_attempt_id"] is not None):
        if tx.one("SELECT 1 FROM salescall.dnc_entries WHERE source_attempt_id = %s AND revoked_at IS NULL LIMIT 1", (source,)):
            continue
        reasons = {r["reason"] for r in tx.all("SELECT DISTINCT reason::text AS reason FROM salescall.dnc_entries"
                                               " WHERE source_attempt_id = %s", (source,))}
        parts = sorted({REVOKED_PARTS[x] for x in reasons if x in REVOKED_PARTS})
        if parts:
            approval.withdraw_revoked(tx, source, parts, reason)
            resettled.append(source)
    numbers = [r["phone_e164"] for r in rows if r["phone_e164"] is not None]
    refs = [r["client_ref"] for r in rows if r["client_ref"] is not None]
    reopened = []
    for t in tx.all("SELECT DISTINCT t.task_id FROM salescall.tasks t WHERE t.client_ref = ANY(%s)"
                    " OR EXISTS (SELECT 1 FROM salescall.task_numbers n WHERE n.task_id = t.task_id AND n.phone_e164 = ANY(%s))"
                    " ORDER BY 1", (refs, numbers)):
        if next_action.reopen(tx, t["task_id"], cause=f"発信禁止の取り消し（{reason}）：終える前の予定に戻す") is not None:
            reopened.append(t["task_id"])
    return {"revoked": [r["entry_id"] for r in rows], "resettled": resettled, "reopened": reopened}
