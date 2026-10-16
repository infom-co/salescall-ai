"""そのお客様・番号へ、かけてよいか（データで決まる分だけ）。持ち主 CL-2。

データで決まる「かけない理由」を答えるのは、この1か所だけ。取込（プレビューと登録）と門（Q2）の両方がこれを呼ぶ
（取込と門が別々に判断すると、取込では通ったのに門で止まる・その逆、が起きる＝二重化。2026-10-14 の見直しで見つけた）。
時間帯・間隔・進行中などの「いま」で決まる理由は、門だけが見る。

理由の名前（門の不許可の理由の一覧と同じ名前。DESIGN.md 5-2-1 の11）：
  number_unknown（番号が不明）／number_kind_forbidden（システムが決してかけない種類）／
  number_kind_not_allowed（業務の設定でかけない種類：フリーダイヤル・ナビダイヤル）／
  dnc_client（依頼者の発信禁止の番号）／dnc_system（システム共通の発信禁止）／
  dnc_client_ref（断ったお客様の管理番号。依頼者ごとの設定で、管理番号を「お客様を表す変わらない番号」としたとき。DESIGN.md 3-6）
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import dnc, settings
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber

_KIND_SETTING = {"toll_free": "calling.allow_toll_free", "navi_dial": "calling.allow_navi_dial"}
_KIND_NAME = {"toll_free": "フリーダイヤル", "navi_dial": "ナビダイヤル"}


def block_reason(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                 business_id: UUID | None) -> tuple[str, str] | None:
    """かけない理由（理由の名前, 日本語の説明）。かけてよければ None。client_ref：その行の依頼者の管理番号（無ければ None）。"""
    if number.kind == "unknown":
        return ("number_unknown", number.reason or "番号が不明です")
    if number.system_forbidden:
        return ("number_kind_forbidden", number.reason or "かけない種類の番号です")
    key = _KIND_SETTING.get(number.kind)
    if key is not None and not settings.resolve(tx, key, tenant_id=tenant_id, client_id=client_id,
                                                business_id=business_id):
        return ("number_kind_not_allowed", f"{_KIND_NAME[number.kind]}へは、この業務の設定でかけません")
    hit = dnc.lookup(tx, number.e164, client_ref=client_ref)
    if hit is None:
        return None
    return {
        "client": ("dnc_client", "発信禁止に登録されている番号です"),
        "system": ("dnc_system", "システム共通の発信禁止に登録されている番号です"),
        "client_ref": ("dnc_client_ref", "この管理番号のお客様は、発信禁止です（もうかけないでと言われたお客様）"),
    }[hit["ledger"]]
