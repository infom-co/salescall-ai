"""そのお客様・番号へ、かけてよいか（データで決まる分だけ）。持ち主 CL-2。

データで決まる「かけない理由」を答えるのは、この1か所だけ。取込（プレビューと登録）と門（Q2）の両方がこれを呼ぶ
（取込と門が別々に判断すると、取込では通ったのに門で止まる・その逆、が起きる＝二重化。2026-10-14 の見直しで見つけた）。
時間帯・間隔・進行中などの「いま」で決まる理由は、門だけが見る。

理由の名前（門の不許可の理由の一覧と同じ名前。DESIGN.md 5-2-1 の11）：
  number_unknown（番号が不明）／number_kind_forbidden（システムが決してかけない種類）／
  number_kind_not_allowed（業務の設定でかけない種類：フリーダイヤル・ナビダイヤル）／
  dnc_client（依頼者の発信禁止の番号）／dnc_contractor（契約者の共通の発信禁止。その契約者のすべての依頼者に効く）／
  other_contractor_called（ほかの契約者がかけた番号で、契約者の設定が「かけない」）／
  dnc_client_ref（断ったお客様の管理番号。依頼者ごとの設定で、管理番号を「お客様を表す変わらない番号」としたとき。DESIGN.md 3-6）／
  declined_in_business（同じ業務で、用件・最終目的を断られたお客様。業務の設定 business.call_declined_again が「いいえ」（初め）のとき。
  2026-11-01 管理者：伺い8。DESIGN.md 5-2-1）／declined_in_client（同じ依頼者のほかの業務で用件を断られた・苦情があったお客様。依頼者の設定
  calling.call_after_refusal が「いいえ」（初め）のとき。✅ 2026-11-08 ユーザー＝計画書 7-3 (1)「用件の断り・苦情のあとは、その依頼者からはかけない」。0216）
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
    customer = customer_block_reason(tx, number, client_ref=client_ref, tenant_id=tenant_id, client_id=client_id,
                                     business_id=business_id)
    if customer is not None:
        return customer
    # ほかの契約者がかけた番号に「かけない」と、契約者が決めていれば（2026-10-16 ユーザー。間隔のときは門が見る）
    if settings.resolve(tx, "gate.other_contractor_interval", tenant_id=tenant_id, client_id=client_id,
                        business_id=business_id) == "never" \
            and tx.one("SELECT salescall.number_dialed_by_other_tenant(%s) AS d", (number.e164,))["d"]:
        return ("other_contractor_called", "ほかの契約者がかけた番号です（契約者の設定で、かけないことにしています）")
    return None


def customer_block_reason(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                          business_id: UUID | None) -> tuple[str, str] | None:
    """お客様の側の「かけない理由」（発信禁止の番号・管理番号・同じ業務で断られたお客様）。かけてよければ None。`block_reason` の
    一部で、かけない番号（記録だけの番号＝お客様が指定していない携帯電話番号など。0175）にも当てる（取込が、その行のほかの番号を
    確かめるとき。そのお客様が断っていれば、どの番号でかけても同じお客様＝原則14）。番号の種類の決まりは当てない（かけない番号なので）。"""
    hit = dnc.lookup(tx, number.e164, client_ref=client_ref)
    if hit is not None:
        return {
            "client": ("dnc_client", "発信禁止に登録されている番号です"),
            "contractor": ("dnc_contractor", "契約者の共通の発信禁止に登録されている番号です"),
            "client_ref": ("dnc_client_ref", "この管理番号のお客様は、発信禁止です（もうかけないでと言われたお客様）"),
        }[hit["ledger"]]
    if business_id is not None and _declined_in_business(tx, number, client_ref=client_ref, tenant_id=tenant_id,
                                                         client_id=client_id, business_id=business_id):
        return ("declined_in_business", "同じ業務で、用件か最終目的を断られたお客様です（業務の設定で、次の実施回でもかけない）")
    if business_id is not None and _declined_in_client(tx, number, client_ref=client_ref, tenant_id=tenant_id, client_id=client_id,
                                                       business_id=business_id):
        return ("declined_in_client", "同じ依頼者のほかの業務で、用件を断られた・苦情があったお客様です（依頼者の設定で、その依頼者からはかけない）")
    return None


def _declined_in_client(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                        business_id: UUID) -> bool:
    """同じ依頼者のほかの業務で用件・最終目的を断られた・どの業務でも苦情があったお客様か（依頼者の設定 calling.call_after_refusal が「はい」なら、
    いつも いいえ）。同じ業務で用件を断られたお客様は、業務の設定（`_declined_in_business`）が受け持つ。同じお客様かは、データベースの1か所
    salescall.declined_in_client（断られた・苦情で終わった行の番号のどれか・依頼者が管理番号をお客様を表す変わらない番号としたときは管理番号＝
    `_declined_in_business` と同じ決め方）。✅ 2026-11-08 ユーザー＝計画書 7-3 (1)「用件の断り・苦情のあとは、その依頼者からはかけない（契約者が
    依頼者ごとに変えられる）」。⛔ 0215 まで：同じ依頼者のほかの業務は、断られた10分後でも同じ会社名でかけた。0216。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    if settings.resolve(tx, "calling.call_after_refusal", **scope):
        return False
    ref = client_ref if client_ref is not None and settings.resolve(tx, "dnc.client_ref_is_stable_id", **scope) else None
    return tx.one("SELECT salescall.declined_in_client(%s, %s, %s, %s) AS d", (client_id, business_id, number.e164, ref))["d"]


def _declined_in_business(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                          business_id: UUID) -> bool:
    """同じ業務で、用件・最終目的を断られたお客様か（業務の設定 business.call_declined_again が「はい」なら、いつも いいえ）。同じお客様かは
    データベースの1か所 salescall.declined_in_business（断られて終わった行の番号のどれか・依頼者が管理番号をお客様を表す変わらない番号と
    したときは管理番号＝発信禁止を広げるときと同じ設定 dnc.client_ref_is_stable_id）。2026-11-01 管理者：伺い8「業務ごとに選択できるように
    する。デフォルトは案1（同じ業務では、次の実施回でもかけない）。…非推奨で案2も選べるように」。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    if settings.resolve(tx, "business.call_declined_again", **scope):
        return False
    ref = client_ref if client_ref is not None and settings.resolve(tx, "dnc.client_ref_is_stable_id", **scope) else None
    return tx.one("SELECT salescall.declined_in_business(%s, %s, %s) AS d", (business_id, number.e164, ref))["d"]
