"""依頼者・業務・実施回・回線を作る・直す部品（Intent.md 4章 A 依頼者管理・C 業務設定管理・5-3 (4)(11)）。持ち主 CL-2。

- 依頼者・業務・実施回・回線を作るのは、この部品だけ（I-12。app/checks が見張る）。画面（CL-3）は、ここを呼ぶ。
- 名前を直すと、データベースが前と後の名前・誰が・いつを記録する（表の作り 0051）。名前のほかは直さない。
- 依頼者の有効／無効：記録を足すだけ（最新が正本）。無効にしたら、まだ発信していない予約を取り消す（門が無効の依頼者を通さない）。
- 業務の複製（Intent.md 4章 C「新規・編集・複製」）：もとの業務の版の中身から、新しい業務の最初の版を作る（確かめと置き方は
  業務設定の1か所 `business_settings.save_version`）。
- 実施回は、作るときに業務設定の版を割り当てる（割り当ては `business_settings.assign` の1か所。版の無い実施回を作らない）。
- 回線の発信番号は、作るときに必ず入れる（日本の携帯か固定電話の番号だけ。番号の見分けは `phone.normalize` の1か所）。
  あとから変えない（番号を替えるなら新しい回線）。使わなくなった回線は印を付ける（消さない）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import business_settings, health, phone
from salescall.core.access import Refused, Rejected, Tx

CALLER_KINDS = ("mobile", "fixed")   # 発信番号にしてよい番号の種類（Rakuten Link は携帯の番号。固定の回線を足すときも、ここ）


class NameTaken(Refused):
    """同じ名前の依頼者（同じ契約者の中）・業務（同じ依頼者の中）が、もうある（何も書いていない。CL-3 の 44。0248）。"""


# 名前が同じかを守るデータベースの一意の約束（0248。比べるのは表記をそろえた形＝salescall.compare_form）→ 断りの文（言い換えはここだけ）
_SAME_NAME = {"clients_same_name": "同じ名前の依頼者が、もうあります（全角と半角・空白の違いは同じ名前です）。別の名前にしてください",
              "businesses_same_name": "この依頼者に、同じ名前の業務が、もうあります（全角と半角・空白の違いは同じ名前です）。別の名前にしてください",
              "client_groups_same_name": "同じ名前のグループが、もうあります（全角と半角・空白の違いは同じ名前です）。別の名前にしてください"}


def _name(name: str) -> str:
    n = name.strip()
    if not n:
        raise Refused("名前が空です")
    return n


def _named(tx: Tx, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    """名前を書く1か所（依頼者・業務を作る・名前を変える）。同じ名前があれば、データベースの断りを `NameTaken` にする。"""
    try:
        return tx.one(sql, params)
    except Rejected as e:
        if e.constraint in _SAME_NAME:
            raise NameTaken(_SAME_NAME[e.constraint]) from e
        raise


def _one(row: dict[str, Any] | None, what: str, key: UUID) -> dict[str, Any]:
    if row is None:
        raise LookupError(f"その{what}は無い（または、取引の範囲の外）：{key}")
    return row


# ═════════ 依頼者（契約者の取引で） ═════════
def tenants(tx: Tx) -> list[dict[str, Any]]:
    """すべての契約者の一覧（[{tenant_id, name}]。作った順）。読めるのは、全体の取引の管理者だけ（データベースの salescall.admin_tenants の1か所＝
    ほかは断る。✅ 2026-11-18 管理者「管理者は全てを見れて操作できる」。CL-3 の 9＝管理者が画面で契約者を選ぶ。0247）。"""
    return tx.all("SELECT tenant_id, name FROM salescall.admin_tenants()")


def create_client(tx: Tx, name: str) -> UUID:
    """取引の契約者に、依頼者を作る。"""
    return _named(tx, "INSERT INTO salescall.clients (tenant_id, name) VALUES (salescall.cur_tenant(), %s) RETURNING client_id",
                  (_name(name),))["client_id"]


def rename_client(tx: Tx, client_id: UUID, name: str) -> None:
    _one(_named(tx, "UPDATE salescall.clients SET name = %s WHERE client_id = %s RETURNING client_id", (_name(name), client_id)),
         "依頼者", client_id)


def set_client_active(tx: Tx, active: bool, *, reason: str) -> None:
    """取引の依頼者を有効・無効にする（依頼者の取引で。管理者か契約者だけ＝データベースが守る）。無効にしたら、まだ発信して
    いない予約を取り消す。"""
    c = tx.one("SELECT salescall.cur_tenant() AS t, salescall.cur_client() AS c")
    if c["c"] is None:
        raise ValueError("依頼者の取引で呼んでください")
    tx.execute("INSERT INTO salescall.client_states (tenant_id, client_id, active, reason, set_by)"   # 番号は next_seq（0109）
               " VALUES (%s, %s, %s, %s, salescall.cur_actor())", (c["t"], c["c"], active, _name(reason)))
    if not active:
        tx.one("SELECT salescall.release_claims_in_scope('client', %s, %s, NULL)", (c["t"], c["c"]))


# ═════════ 依頼者のグループ（契約者の取引で。画面の並べ方だけ＝分離・権限には使わない。0255・CL-3 の 15） ═════════
def groups(tx: Tx) -> list[dict[str, Any]]:
    """その契約者の依頼者のグループ（並び順）と、入っている依頼者：[{group_id, name, position, clients: [依頼者]}]。1つの依頼者が複数のグループに
    入れる。どのグループにも入っていない依頼者は、画面が「グループなし」に並べる（ここでは返さない）。"""
    rows = tx.all("WITH now_in AS (SELECT DISTINCT ON (x.group_id, x.client_id) x.group_id, x.client_id, x.member"
                  "                FROM salescall.client_group_changes x ORDER BY x.group_id, x.client_id, x.seq DESC)"
                  " SELECT g.group_id, g.name, g.position, coalesce(array_agg(m.client_id ORDER BY c.created_at, c.client_id)"
                  "       FILTER (WHERE m.member), '{}') AS clients"
                  " FROM salescall.client_groups g LEFT JOIN now_in m ON m.group_id = g.group_id"
                  " LEFT JOIN salescall.clients c ON c.client_id = m.client_id WHERE g.removed_at IS NULL"
                  " GROUP BY g.group_id, g.name, g.position ORDER BY g.position, g.name, g.group_id")
    return [dict(r, clients=list(r["clients"])) for r in rows]


def create_group(tx: Tx, name: str) -> UUID:
    """グループを作る（いちばん後ろに並べる。管理者か契約者だけ＝データベースが守る。同じ名前は NameTaken）。"""
    return _named(tx, "INSERT INTO salescall.client_groups (tenant_id, name, position)"
                      " SELECT salescall.cur_tenant(), %s, coalesce(max(position), 0) + 1 FROM salescall.client_groups WHERE removed_at IS NULL"
                      " RETURNING group_id",
                  (_name(name),))["group_id"]


def rename_group(tx: Tx, group_id: UUID, name: str) -> None:
    _one(_named(tx, "UPDATE salescall.client_groups SET name = %s WHERE group_id = %s AND removed_at IS NULL RETURNING group_id",
                (_name(name), group_id)),
         "グループ", group_id)


def move_group(tx: Tx, group_id: UUID, position: int) -> None:
    """グループの並び順を変える（1 からの位置。ほかのグループは、詰めてずらす）。"""
    ids = [r["group_id"] for r in tx.all("SELECT group_id FROM salescall.client_groups WHERE removed_at IS NULL ORDER BY position, name, group_id")]
    if group_id not in ids:
        raise LookupError(f"そのグループは無い（または、取引の範囲の外）：{group_id}")
    ids.remove(group_id)
    ids.insert(max(0, min(position - 1, len(ids))), group_id)
    for n, g in enumerate(ids, start=1):
        tx.execute("UPDATE salescall.client_groups SET position = %s WHERE group_id = %s AND position <> %s", (n, g, n))


def remove_group(tx: Tx, group_id: UUID) -> None:
    """グループをやめる（消さずに、やめた時刻を残す。入っていた依頼者は、そのまま＝依頼者のデータには触れない。同じ名前のグループを、また作れる）。"""
    _one(tx.one("UPDATE salescall.client_groups SET removed_at = now() WHERE group_id = %s AND removed_at IS NULL RETURNING group_id",
                (group_id,)), "グループ", group_id)


def set_group_member(tx: Tx, group_id: UUID, client_id: UUID, member: bool) -> None:
    """依頼者をグループに入れる・外す（足していく記録。いまの組は、いちばん新しい記録＝`groups`）。やめたグループには入れられない。"""
    if tx.one("SELECT 1 FROM salescall.client_groups WHERE group_id = %s AND removed_at IS NULL", (group_id,)) is None:
        raise LookupError(f"そのグループは無い（または、やめた・取引の範囲の外）：{group_id}")
    tx.execute("INSERT INTO salescall.client_group_changes (tenant_id, group_id, client_id, member, set_by)"
               " VALUES (salescall.cur_tenant(), %s, %s, %s, salescall.cur_actor())", (group_id, client_id, member))


# ═════════ 業務・実施回（依頼者の取引で） ═════════
def businesses(tx: Tx) -> list[dict[str, Any]]:
    """その依頼者の業務の一覧（依頼者の下の業務のタブ＝CL-3 の 15）：[{business_id, name, created_at}]（作った順）。"""
    return tx.all("SELECT business_id, name, created_at FROM salescall.businesses ORDER BY created_at, business_id")



def create_business(tx: Tx, name: str) -> UUID:
    """取引の依頼者に、業務を作る（版は、業務設定を置いて作る＝business_settings.save_version・取込）。"""
    return _named(tx, "INSERT INTO salescall.businesses (tenant_id, client_id, name)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s) RETURNING business_id", (_name(name),))["business_id"]


def rename_business(tx: Tx, business_id: UUID, name: str) -> None:
    _one(_named(tx, "UPDATE salescall.businesses SET name = %s WHERE business_id = %s RETURNING business_id",
                (_name(name), business_id)), "業務", business_id)


def copy_business(tx: Tx, version_id: UUID, name: str) -> tuple[UUID, UUID]:
    """その版の業務設定（中身・質問・業務の決まり）から、新しい業務と、その最初の版を作る（同じ依頼者の中で）。
    （新しい業務, 新しい版）を返す。もとの業務は変えない。"""
    business_id = create_business(tx, name)               # 写す版が読めなければ、同じ取引なので業務も残らない
    return business_id, business_settings.save_from(tx, business_id, version_id)


def create_run(tx: Tx, business_id: UUID, name: str, version_id: UUID, *, reason: str) -> UUID:
    """業務に実施回を作り、業務設定の版を割り当てる（1つの取引。版は、その業務の版だけ＝データベースが守る）。"""
    run_id = tx.one("INSERT INTO salescall.runs (tenant_id, client_id, business_id, name, created_by)"
                    " SELECT b.tenant_id, b.client_id, b.business_id, %s, salescall.cur_actor() FROM salescall.businesses b"
                    " WHERE b.business_id = %s RETURNING run_id", (_name(name), business_id))
    run_id = _one(run_id, "業務", business_id)["run_id"]
    business_settings.assign(tx, run_id, version_id, reason=reason)
    return run_id


def rename_run(tx: Tx, run_id: UUID, name: str) -> None:
    _one(tx.one("UPDATE salescall.runs SET name = %s WHERE run_id = %s RETURNING run_id", (_name(name), run_id)),
         "実施回", run_id)


# ═════════ 回線（契約者の取引で） ═════════
def caller_number(said: str) -> str:
    """発信番号として受け付ける番号の国際の形（日本の携帯か固定電話だけ）。だめなら ValueError。"""
    n = phone.normalize(said)
    if n.kind not in CALLER_KINDS or n.e164 is None:
        raise ValueError(f"発信番号にできるのは、日本の携帯か固定電話の番号だけです（{n.reason or n.kind}）")
    return n.e164


def line_methods(tx: Tx) -> list[dict[str, Any]]:
    """回線の方式の一覧（画面で選ぶ。中の名前 method と画面の名前 label。0104）。回線を作るときも、回線の単価を入れるときも、
    この一覧の方式だけ（データベースが守る）。"""
    return tx.all("SELECT method, label FROM salescall.line_methods ORDER BY added_at, method")


def add_line_method(tx: Tx, method: str, label: str) -> None:
    """回線の方式を一覧に足す（新しい回線の方式を作って確かめたとき。管理者だけ＝データベースが守る）。"""
    tx.execute("INSERT INTO salescall.line_methods (method, label) VALUES (%s, %s)", (method, _name(label)))


def create_line(tx: Tx, name: str, *, method: str, caller: str, client_id: UUID | None = None) -> UUID:
    """取引の契約者に、回線を作る（発信番号は必ず。client_id＝その依頼者の専用。None＝共有）。方式は `line_methods` から選ぶ。"""
    return tx.one("INSERT INTO salescall.lines (tenant_id, name, method, caller_e164, client_id)"
                  " VALUES (salescall.cur_tenant(), %s, %s, %s, %s) RETURNING line_id",
                  (_name(name), _name(method), caller_number(caller), client_id))["line_id"]


def rename_line(tx: Tx, line_id: UUID, name: str) -> None:
    _one(tx.one("UPDATE salescall.lines SET name = %s WHERE line_id = %s RETURNING line_id", (_name(name), line_id)),
         "回線", line_id)


def assign_line(tx: Tx, line_id: UUID, client_id: UUID | None) -> None:
    """回線を、その依頼者の専用にする（None＝共有に戻す）。まだ発信していない、その回線の予約は取り消す（門の前提が変わる）。"""
    _one(tx.one("UPDATE salescall.lines SET client_id = %s WHERE line_id = %s RETURNING line_id", (client_id, line_id)),
         "回線", line_id)
    _release_line_claims(tx, line_id)


def retire_line(tx: Tx, line_id: UUID, *, reason: str) -> None:
    """回線を使わなくなった（端末を返した など）。1回だけ。まだ発信していない、その回線の予約は取り消す。"""
    _one(tx.one("UPDATE salescall.lines SET retired_at = now(), retired_reason = %s WHERE line_id = %s AND retired_at IS NULL"
                " RETURNING line_id", (_name(reason), line_id)), "使っている回線", line_id)
    _release_line_claims(tx, line_id)
    # 回線を外した（E13）：状態の表を引く入口へ（DESIGN.md 17章）。その回線の、番号のせいか回線のせいかがまだ分からない続きは、表が
    # 回線のせいと決める（L-14。その回線はもうかけないので、次の電話で決まることは無い。番号には数えず、ほかの回線でかけ直して確かめる＝
    # 2026-10-24 ユーザー：「一つの行で止まり続けることがないように」）
    health.removed(tx, line_id, reason)


def _release_line_claims(tx: Tx, line_id: UUID) -> None:
    tx.one("SELECT salescall.release_claims_on_line(%s)", (line_id,))
