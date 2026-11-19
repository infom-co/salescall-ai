"""結果の送り先（docs/DESIGN.md 4-5。持ち主 CL-3。表 destinations のデータの形・業務ごとの選びは CL-2 の core/destinations＝4-435）。

- 送り先を作る・直すのは、ここ1か所だけ（見張り checks/rules.py の destinations の書き手）。管理者か契約者の取引だけ（データベースの destination_guard）。
- **送り方は、送り方の部品があるものだけ選べる**（`METHODS` の1か所）。部品の無い送り方で送り先を作ると、契約者は渡しているつもりで
  何も渡らないため。部品は、依頼者の仕組みの種類ごとに1つ（4-5。例：kintone・依頼者の独自の仕組み）で、最初にその仕組みを使う依頼者が
  決まったときに作る。Google スプレッドシートは、書き方（A 丸ごと書き直す／B 鍵の列で探して上書き）をユーザーに選んでいただいてから（4-5）。
- **「同じ鍵なら上書き」で受けられるかは、送り方の部品が決める**（契約者には分からないことなので、画面では聞かない）。
  自動で書き戻せるのは、受けられる送り先だけ（決めるのはデータベースの destinations_auto_only_upsert の1か所。画面は受けられない送り先に、その欄を出さない）。
- 認証情報は、値を表に入れず参照だけを持つ（credential_ref）。⏳ 参照の付け方（サーバーの秘密の名前）は、最初の送り方の部品と一緒に決める。
- どの業務の結果をどの送り先へ渡すかを決めるのは core の destinations.for_business の1か所（ここでは決め直さない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import access
from salescall.core.access import Tx


@dataclass(frozen=True)
class Method:
    label: str              # 画面に出す送り方の名前（例：「kintone」）
    upsert_capable: bool    # 「同じ鍵なら上書き、無ければ作る」で受けられるか（部品が決める）


# 送り方の部品があるもの（送り方の名前 → 中身）。部品を作ったら、ここに足す。いまは、まだ1つも無い（結果はファイルで渡す）。
METHODS: dict[str, Method] = {}


class DestinationInvalid(access.Refused):
    """送り先を作れない・直せない理由（文はそのまま画面に出せる）。"""


def method_label(method: str) -> str:
    """送り方の画面の名前。部品の無い送り方（前に作った送り先など）は、そのことを言う。"""
    m = METHODS.get(method)
    return m.label if m else "送る部品がまだ無い送り方"


def of_client(tx: Tx) -> list[dict[str, Any]]:
    """依頼者の取引で、その依頼者の送り先の一覧（名前の順）。部品があるか（sendable）も添える。"""
    rows = tx.all("SELECT destination_id, name, method, upsert_capable, auto_writeback FROM salescall.destinations"
                  " ORDER BY name, destination_id")
    return [dict(r, method_label=method_label(r["method"]), sendable=r["method"] in METHODS) for r in rows]


def _name(raw: str) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise DestinationInvalid("送り先の名前を入れてください")
    if len(name) > 60:
        raise DestinationInvalid("送り先の名前は60字までにしてください")
    return name


def create(tx: Tx, name: str, method: str, auto_writeback: bool) -> UUID:
    """依頼者の取引で、送り先を作る。送り方は部品のあるものだけ。上書きで受けられるかは部品が決める。"""
    m = METHODS.get(method)
    if m is None:
        raise DestinationInvalid("この送り方は、まだ選べません（送る部品がありません）")
    return tx.one("INSERT INTO salescall.destinations (tenant_id, client_id, name, method, upsert_capable, auto_writeback)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s) RETURNING destination_id",
                  (_name(name), method, m.upsert_capable, auto_writeback))["destination_id"]


def update(tx: Tx, destination_id: UUID, name: str, auto_writeback: bool) -> None:
    """依頼者の取引で、送り先の名前と、自動で書き戻すかを直す（送り方は変えない＝変えるなら新しい送り先に。4-5）。"""
    if tx.one("SELECT 1 AS x FROM salescall.destinations WHERE destination_id = %s", (destination_id,)) is None:
        raise LookupError(destination_id)
    tx.execute("UPDATE salescall.destinations SET name = %s, auto_writeback = %s WHERE destination_id = %s",
               (_name(name), auto_writeback, destination_id))
