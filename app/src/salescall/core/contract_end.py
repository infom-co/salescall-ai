"""依頼者との契約が終わったとき（設計書 3-10。計画書 11-4。表の作り 0285）。持ち主 CL-2。

✅ 2026-11-22 管理者：「ハードディスクに残す。消すのはハードディスクで消すだけ。ただし、絶対に不要なデータは、イ（画面で消す）もできるように
しておく。」・サーバーは「(ア) HDに写してサーバーから外す」。

- 押す（`end`）・取り消す（`cancel`）は、管理者か契約者が依頼者の取引で（データベースが守る）。押すと新しい発信が止まる（依頼者を使わない形に
  する＝registry.set_client_active の1か所）。猶予（仮の設定 contract_end.forget_after）のあいだは取り消せる。
- 外せるかは、データベースの salescall.forget_blocked の1か所（`state` が理由の文を返す＝画面と見守り役が同じ答えを読む）：猶予が過ぎた・使わない形・
  かけている電話が無い・提出用のファイルをすべてハードディスクへ写した・契約終了のあとの AI用の控えを戻せることまで確かめた。
- 外すのは salescall.forget_client の1か所（`forget`。見守り役の仕事 contract_ends が、契約者の取引のシステムの処理で呼ぶ）。残す表・外す表・
  消してよいかの判断は、表の作り 0285 の説明のとおり（ほかの依頼者・契約者の判断に使う記録は残す）。外したあとは「使う」に戻せない。
- ハードディスクの控えは、契約終了になった依頼者の分を日数で消さない（backup.prune_rules が `ended_clients` を読む）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import notify, registry, settings
from salescall.core.access import Tx


@dataclass(frozen=True)
class State:
    """依頼者の契約終了の状態（画面と見守り役が読む）。end_id＝いまの契約終了（無ければ None）・forgotten＝サーバーから外した・
    blocked＝まだ外せない理由（外せる・契約終了でない・外したあと、は下の値）。"""
    end_id: int | None
    ended_at: datetime | None
    forget_at: datetime | None      # 猶予が過ぎる時刻（このあとで外せる）
    forgotten: bool
    blocked: str | None


def forget_after(tx: Tx) -> timedelta:
    """契約終了を押してから外すまでの猶予（仮の設定 contract_end.forget_after）。"""
    return timedelta(seconds=int(settings.system_value(tx, "contract_end.forget_after")))


def end(tx: Tx, *, reason: str) -> int:
    """取引の依頼者の契約を終える（管理者か契約者だけ）。新しい発信を止める（使わない形にする）。契約終了の番号を返す。"""
    row = tx.one("INSERT INTO salescall.client_contract_ends (tenant_id, client_id, reason)"
                 " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s) RETURNING end_id", (reason.strip(),))
    registry.set_client_active(tx, False, reason=f"契約終了：{reason.strip()}")
    return row["end_id"]


def cancel(tx: Tx, *, reason: str) -> None:
    """取引の依頼者の契約終了を取り消す（外す前だけ・管理者か契約者だけ）。使う形に戻す。"""
    end_id = tx.one("SELECT salescall.contract_ended(salescall.cur_client()) AS e")["e"]
    if end_id is None:
        raise ValueError("契約終了になっていません")
    tx.execute("INSERT INTO salescall.client_contract_end_cancels (end_id, tenant_id, client_id, reason)"
               " VALUES (%s, salescall.cur_tenant(), salescall.cur_client(), %s)", (end_id, reason.strip()))
    registry.set_client_active(tx, True, reason=f"契約終了の取り消し：{reason.strip()}")


def state(tx: Tx, client_id: UUID) -> State:
    """その依頼者の契約終了の状態（契約者の取引でも依頼者の取引でも）。"""
    after = forget_after(tx)
    row = tx.one(
        "SELECT e.end_id, e.ended_at, EXISTS (SELECT 1 FROM salescall.client_forgets f WHERE f.client_id = %(c)s) AS forgotten,"
        "       salescall.forget_blocked(%(c)s, %(after)s) AS blocked"
        " FROM (SELECT salescall.contract_ended(%(c)s) AS id) x LEFT JOIN salescall.client_contract_ends e ON e.end_id = x.id",
        {"c": client_id, "after": after})
    ended = row["ended_at"]
    return State(row["end_id"], ended, None if ended is None else ended + after, row["forgotten"],
                 None if row["end_id"] is None or row["forgotten"] else row["blocked"])


def ended_clients(tx: Tx) -> list[UUID]:
    """取引の契約者の、契約終了になっている依頼者（外したものも。ハードディスクの控えを日数で消さない依頼者）。"""
    return [r["client_id"] for r in tx.all(
        "SELECT c.client_id FROM salescall.clients c WHERE salescall.contract_ended(c.client_id) IS NOT NULL"
        " OR EXISTS (SELECT 1 FROM salescall.client_forgets f WHERE f.client_id = c.client_id) ORDER BY c.client_id")]


def forget_ready(tx: Tx) -> list[UUID]:
    """取引の契約者の、いま外せる依頼者（見守り役の仕事 contract_ends が読む）。"""
    after = forget_after(tx)
    return [r["client_id"] for r in tx.all(
        "SELECT c.client_id FROM salescall.clients c WHERE salescall.contract_ended(c.client_id) IS NOT NULL"
        " AND salescall.forget_blocked(c.client_id, %s) IS NULL ORDER BY c.client_id", (after,))]


def forget(tx: Tx, client_id: UUID) -> dict[str, Any]:
    """その依頼者をサーバーから外す（契約者の取引のシステムの処理で。外せなければデータベースが理由つきで断る）。外した表ごとの行の数を返す。
    契約者と管理者に知らせる。"""
    removed = tx.one("SELECT salescall.forget_client(%s, %s) AS r", (client_id, forget_after(tx)))["r"]
    notify.emit(tx, "client_forgotten", key=f"client_forgotten:{client_id}",
                summary="契約が終わったので、サーバーからこの依頼者のデータを外しました（ハードディスクの控えは残っています）。",
                refs={"client_id": client_id}, admin=True)
    return removed
