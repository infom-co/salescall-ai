"""業務ごとの送り先（2026-11-19。CL-3 の連絡帳 4-1b の 19。表の作り 0254。DESIGN.md 4-5）。

送り先そのもの（destinations。名前・方式・「同じ鍵なら上書き」で受けられるか・自動で書き戻すか・認証情報の参照）を書くのは出力の部品（output/。CL-3）。
ここは、どの業務の結果を、どの送り先へ渡すかを決める1か所だけ（✅ 2026-11-18 管理者「業務によって変わりえます」）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core.access import Tx


def for_business(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """その業務の結果を渡す送り先（1か所。出力の部品が、渡すたびに読む）：業務が送り先を選んでいれば（いちばん新しい選びが「選ぶ」の送り先が1つ
    以上）その送り先だけ、選んでいなければ、その依頼者の送り先のすべて。[{destination_id, name, method, upsert_capable, auto_writeback}]（名前の順）。"""
    return tx.all(
        "WITH latest AS (SELECT DISTINCT ON (d.destination_id) d.destination_id, d.chosen FROM salescall.business_destinations d"
        "                WHERE d.business_id = %s ORDER BY d.destination_id, d.seq DESC),"
        "     picked AS (SELECT destination_id FROM latest WHERE chosen)"
        " SELECT x.destination_id, x.name, x.method, x.upsert_capable, x.auto_writeback FROM salescall.destinations x"
        " WHERE x.client_id = (SELECT b.client_id FROM salescall.businesses b WHERE b.business_id = %s)"
        "   AND (NOT EXISTS (SELECT 1 FROM picked) OR x.destination_id IN (SELECT destination_id FROM picked))"
        " ORDER BY x.name, x.destination_id", (business_id, business_id))


def choose(tx: Tx, business_id: UUID, destination_id: UUID, chosen: bool) -> None:
    """その業務が、その送り先へ渡すかを選ぶ・選ぶのをやめる（管理者か契約者だけ＝データベースが守る。書くのはここだけ）。選んだ送り先が1つも
    無くなれば、依頼者の送り先のすべてへ渡す形に戻る（`for_business`）。"""
    tx.execute("INSERT INTO salescall.business_destinations (tenant_id, client_id, business_id, destination_id, chosen, set_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())",
               (business_id, destination_id, chosen))
