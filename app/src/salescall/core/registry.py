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
from salescall.core.access import Tx

CALLER_KINDS = ("mobile", "fixed")   # 発信番号にしてよい番号の種類（Rakuten Link は携帯の番号。固定の回線を足すときも、ここ）


def _name(name: str) -> str:
    n = name.strip()
    if not n:
        raise ValueError("名前が空です")
    return n


def _one(row: dict[str, Any] | None, what: str, key: UUID) -> dict[str, Any]:
    if row is None:
        raise LookupError(f"その{what}は無い（または、取引の範囲の外）：{key}")
    return row


# ═════════ 依頼者（契約者の取引で） ═════════
def create_client(tx: Tx, name: str) -> UUID:
    """取引の契約者に、依頼者を作る。"""
    return tx.one("INSERT INTO salescall.clients (tenant_id, name) VALUES (salescall.cur_tenant(), %s) RETURNING client_id",
                  (_name(name),))["client_id"]


def rename_client(tx: Tx, client_id: UUID, name: str) -> None:
    _one(tx.one("UPDATE salescall.clients SET name = %s WHERE client_id = %s RETURNING client_id", (_name(name), client_id)),
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


# ═════════ 業務・実施回（依頼者の取引で） ═════════
def create_business(tx: Tx, name: str) -> UUID:
    """取引の依頼者に、業務を作る（版は、業務設定を置いて作る＝business_settings.save_version・取込）。"""
    return tx.one("INSERT INTO salescall.businesses (tenant_id, client_id, name)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s) RETURNING business_id", (_name(name),))["business_id"]


def rename_business(tx: Tx, business_id: UUID, name: str) -> None:
    _one(tx.one("UPDATE salescall.businesses SET name = %s WHERE business_id = %s RETURNING business_id",
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
