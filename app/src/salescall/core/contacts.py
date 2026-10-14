"""連絡先（正規化した番号。依頼主ごとに1つ。DESIGN.md 3-1）。持ち主 CL-2。

連絡先を作るのは、この部品の `get_or_create` だけ（I-12。取込も、相手が言った別の番号も、ここを通る）。
番号は番号の正規化（Q1）を通したものだけを受け取る（そろえられない番号の連絡先は作れない）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber


def get_or_create(tx: Tx, number: PhoneNumber) -> UUID:
    """その依頼主の、その番号の連絡先（無ければ作る）。取引の依頼主の範囲で動く。"""
    if number.e164 is None:
        raise ValueError(f"そろえられない番号の連絡先は作れません：{number.reason}")
    tx.execute(
        "INSERT INTO salescall.contacts (tenant_id, client_id, phone_e164)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s)"
        " ON CONFLICT (client_id, phone_e164) DO NOTHING",
        (number.e164,),
    )
    row = tx.one("SELECT contact_id FROM salescall.contacts WHERE phone_e164 = %s", (number.e164,))
    return row["contact_id"]
