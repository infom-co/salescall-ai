"""業務ごとの送り先（2026-11-19。CL-3 の連絡帳 4-1b の 19。表の作り 0254。DESIGN.md 4-5）。

送り先そのもの（destinations。名前・方式・「同じ鍵なら上書き」で受けられるか・自動で書き戻すか・認証情報の参照）を書くのは出力の部品（output/。CL-3）。
ここは、どの業務の結果を、どの送り先へ渡すかを決める1か所だけ（✅ 2026-11-18 管理者「業務によって変わりえます」）。業務が送り先を自分で選ぶか（0267。CL-3 の 56）：
選ぶ＝選んだ送り先だけ（1つも無ければ、どこへも書き戻さない＝ファイルだけで渡す）／選ばない＝その依頼者の送り先すべて。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core.access import Tx


def chosen(tx: Tx, business_id: UUID) -> frozenset[UUID] | None:
    """その業務が選んだ送り先（1か所。画面も読む）：業務が送り先を自分で選んでいなければ None（依頼者の送り先すべて）、選んでいれば選んだ送り先（いちばん新しい
    選びが「選ぶ」のもの。空＝どこへも書き戻さない＝ファイルだけ）。"""
    if not _choosing(tx, business_id):
        return None
    return frozenset(r["destination_id"] for r in tx.all(
        "SELECT destination_id FROM (SELECT DISTINCT ON (d.destination_id) d.destination_id, d.chosen FROM salescall.business_destinations d"
        "                            WHERE d.business_id = %s ORDER BY d.destination_id, d.seq DESC) l WHERE l.chosen", (business_id,)))


def for_business(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """その業務の結果を渡す送り先（1か所。出力の部品が、渡すたびに読む）：業務が送り先を選んでいれば（`chosen`）その送り先だけ（無ければ、どこへも渡さない＝
    ファイルだけ）、選んでいなければ、その依頼者の送り先のすべて。[{destination_id, name, method, upsert_capable, auto_writeback}]（名前の順）。"""
    picked = chosen(tx, business_id)
    rows = tx.all("SELECT x.destination_id, x.name, x.method, x.upsert_capable, x.auto_writeback FROM salescall.destinations x"
                  " WHERE x.client_id = (SELECT b.client_id FROM salescall.businesses b WHERE b.business_id = %s)"
                  " ORDER BY x.name, x.destination_id", (business_id,))
    return [r for r in rows if picked is None or r["destination_id"] in picked]


def set_choosing(tx: Tx, business_id: UUID, choosing: bool) -> None:
    """その業務が送り先を自分で選ぶか（管理者か契約者だけ＝データベースが守る。書くのはここだけ）。選ぶにして1つも選ばなければ、どこへも書き戻さない
    （ファイルだけで渡す）。選ばないに戻せば、依頼者の送り先のすべて（選んでいた送り先の記録は残り、また選ぶにすれば戻る）。"""
    tx.execute("INSERT INTO salescall.business_destination_choosing (tenant_id, client_id, business_id, choosing, set_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (business_id, choosing))


def choose(tx: Tx, business_id: UUID, destination_id: UUID, chosen: bool) -> None:
    """その業務が、その送り先へ渡すかを選ぶ・選ぶのをやめる（管理者か契約者だけ＝データベースが守る。書くのはここだけ）。送り先を選んだら、その業務は送り先を
    自分で選ぶ業務になる（`set_choosing`。選んだ送り先が1つも無くなっても、選ぶのまま＝ファイルだけ。依頼者の送り先すべてに戻すのは `set_choosing` の偽）。"""
    tx.execute("INSERT INTO salescall.business_destinations (tenant_id, client_id, business_id, destination_id, chosen, set_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())",
               (business_id, destination_id, chosen))
    if chosen and not _choosing(tx, business_id):
        set_choosing(tx, business_id, True)


def _choosing(tx: Tx, business_id: UUID) -> bool:
    """その業務が、いま送り先を自分で選んでいるか（いちばん新しい記録。無ければ選んでいない）。"""
    row = tx.one("SELECT choosing FROM salescall.business_destination_choosing WHERE business_id = %s ORDER BY seq DESC LIMIT 1", (business_id,))
    return bool(row and row["choosing"])
