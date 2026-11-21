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

from collections.abc import Iterable
from datetime import datetime, timedelta
from uuid import UUID

from salescall.core import business_settings, dnc, settings, tasks
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber, normalize

_KIND_SETTING = {"toll_free": "calling.allow_toll_free", "navi_dial": "calling.allow_navi_dial"}
_KIND_NAME = {"toll_free": "フリーダイヤル", "navi_dial": "ナビダイヤル"}


def block_reason(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                 business_id: UUID | None, task_id: UUID | None) -> tuple[str, str] | None:
    """かけない理由（理由の名前, 日本語の説明）。かけてよければ None。client_ref：その行の依頼者の管理番号（無ければ None）。task_id：その行
    （まだ登録していない取込の行は None。同じ業務・同じ依頼者で断られたお客様かは、その行そのものの通話を数えない＝`customer_block_reason`）。"""
    if number.kind == "unknown":
        return ("number_unknown", number.reason or "番号が不明です")
    if number.system_forbidden:
        return ("number_kind_forbidden", number.reason or "かけない種類の番号です")
    key = _KIND_SETTING.get(number.kind)
    if key is not None and not settings.resolve(tx, key, tenant_id=tenant_id, client_id=client_id,
                                                business_id=business_id):
        return ("number_kind_not_allowed", f"{_KIND_NAME[number.kind]}へは、この業務の設定でかけません")
    customer = customer_block_reason(tx, number, client_ref=client_ref, tenant_id=tenant_id, client_id=client_id,
                                     business_id=business_id, task_id=task_id)
    if customer is not None:
        return customer
    # ほかの契約者がかけた番号に「かけない」と、契約者が決めていれば（2026-10-16 ユーザー。間隔のときは門が見る）
    if settings.resolve(tx, "gate.other_contractor_interval", tenant_id=tenant_id, client_id=client_id,
                        business_id=business_id) == "never" \
            and tx.one("SELECT salescall.number_dialed_by_other_tenant(%s) AS d", (number.e164,))["d"]:
        return ("other_contractor_called", "ほかの契約者がかけた番号です（契約者の設定で、かけないことにしています）")
    return None


def row_block_reason(tx: Tx, dial: PhoneNumber, others: Iterable[PhoneNumber], *, client_ref: str | None, tenant_id: UUID,
                     client_id: UUID, business_id: UUID | None, task_id: UUID | None) -> tuple[str, str, PhoneNumber | None] | None:
    """行（お客様）へかけない理由（1か所。取込と門と、発信禁止を取り消した行を戻す処理が読む）：かける番号の理由（`block_reason`）か、
    行のほかの番号（記録だけの番号・お客様が言った別の番号など＝かけない番号）のどれかにある、お客様の側の理由（`customer_block_reason`。
    そのお客様が断っていれば、どの番号でかけても同じお客様＝原則14）。ほかの番号のその番号だけの発信禁止（番号違い・FAX の回線＝
    dnc.NUMBER_ONLY）は、お客様を止めない。答え：(理由の名前, 日本語の説明, 当たったほかの番号＝かける番号の理由なら None)。
    ⛔ 2026-11-15 の見直しまで：ほかの番号は取込のときだけ確かめ、門はかける番号だけを見た＝取込のあとに同じ業務・同じ依頼者のほかの業務で
    断られたお客様（記録だけの携帯電話番号が、断られた行の番号）や、手で発信禁止にした記録だけの番号の行へ、門が電話を通した。"""
    scope = dict(client_ref=client_ref, tenant_id=tenant_id, client_id=client_id, business_id=business_id, task_id=task_id)
    hit = block_reason(tx, dial, **scope)
    if hit is not None:
        return (*hit, None)
    for other in others:
        if other.e164 == dial.e164:
            continue
        hit = customer_block_reason(tx, other, customer_only=True, **scope)
        if hit is not None:
            return (*hit, other)
    return None


def task_block_reason(tx: Tx, task_id: UUID, dial: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                      business_id: UUID | None) -> tuple[str, str, PhoneNumber | None] | None:
    """登録した行へ、その番号でかけない理由（`row_block_reason` を、行のほかの番号のうち、かける番号と同じ方の番号で）。門と、発信禁止を取り消した
    行を戻す処理が読む。同じ方の番号：「担当の方は違う」で直した行では、前の担当の方の番号（tasks.former_numbers＝断りの範囲 dnc.refusal_reach と
    同じ1か所）と、いまの担当の方の番号は別の方の番号（前の担当の方が断っても、新しい担当の方へはかける・その逆も＝✅ 2026-11-07 ユーザー＝計画書 7-1 (22)）。
    かける番号が前の担当の方の番号なら前の担当の方の番号、ほかはいまの担当の方の番号（`same_person_numbers` の1か所）。"""
    return row_block_reason(tx, dial, same_person_numbers(tx, task_id, dial), client_ref=client_ref, tenant_id=tenant_id, client_id=client_id,
                            business_id=business_id, task_id=task_id)


def same_person_numbers(tx: Tx, task_id: UUID, dial: PhoneNumber) -> list[PhoneNumber]:
    """行のほかの番号のうち、かける番号と同じ方の番号（1か所。かけない理由 `task_block_reason`・待つ時刻 `declined_wait_until` が読む）：「担当の方は違う」で
    直した行では、前の担当の方の番号（tasks.former_numbers）と、いまの担当の方の番号は別の方の番号。"""
    former = set(tasks.former_numbers(tx, task_id))
    same = (lambda n: n in former) if dial.e164 in former else (lambda n: n not in former)
    return [normalize(n) for n in tasks.numbers(tx, task_id) if n != dial.e164 and same(n)]


def customer_block_reason(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                          business_id: UUID | None, task_id: UUID | None, customer_only: bool = False) -> tuple[str, str] | None:
    """お客様の側の「かけない理由」（発信禁止の番号・管理番号・同じ業務で断られたお客様）。かけてよければ None。`block_reason` の
    一部で、行のかけない番号（記録だけの番号＝お客様が指定していない携帯電話番号など。0175）にも当てる（`row_block_reason`。
    customer_only＝その番号だけの発信禁止は見ない）。番号の種類の決まりは当てない（かけない番号なので）。"""
    # その行の前の担当の方の「もうかけないで」の番号を、いまの担当の方へかけるか（業務の設定。0229。✅ 2026-11-16 ユーザー「デフォルトは…その番号でかける」）
    former_of = task_id if task_id is not None and business_id is not None and settings.resolve(
        tx, "dnc.former_refusal_calls_new_person", tenant_id=tenant_id, client_id=client_id, business_id=business_id) else None
    hit = dnc.lookup(tx, number.e164, client_ref=client_ref, customer_only=customer_only, former_of=former_of)
    if hit is not None:
        return {
            "client": ("dnc_client", "発信禁止に登録されている番号です"),
            "contractor": ("dnc_contractor", "契約者の共通の発信禁止に登録されている番号です"),
            "client_ref": ("dnc_client_ref", "この管理番号のお客様は、発信禁止です（もうかけないでと言われたお客様）"),
        }[hit["ledger"]]
    if business_id is not None and _declined_in_business(tx, number, client_ref=client_ref, tenant_id=tenant_id, task_id=task_id,
                                                         client_id=client_id, business_id=business_id):
        return ("declined_in_business", "同じ業務で、用件か最終目的を断られたお客様です（業務の設定で、次のお客様リストでもかけない）")
    if business_id is not None and _declined_in_client(tx, number, client_ref=client_ref, tenant_id=tenant_id, client_id=client_id, task_id=task_id,
                                                       business_id=business_id):
        return ("declined_in_client", "同じ依頼者のほかの業務で、用件を断られた・苦情があったお客様です（依頼者の設定で、その依頼者からはかけない）")
    return None


def _declined_in_client(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                        business_id: UUID, task_id: UUID | None) -> bool:
    """同じ依頼者のほかの業務で用件・最終目的を断られた・どの業務でも苦情があったお客様か（依頼者の設定 calling.call_after_refusal が「はい」なら、
    いつも いいえ）。同じ業務で用件を断られたお客様は、業務の設定（`_declined_in_business`）が受け持つ。同じお客様かは、データベースの1か所
    salescall.declined_in_client（断られた・苦情で終わった行の番号のどれか・依頼者が管理番号をお客様を表す変わらない番号としたときは管理番号＝
    `_declined_in_business` と同じ決め方）。✅ 2026-11-08 ユーザー＝計画書 7-3 (1)「用件の断り・苦情のあとは、その依頼者からはかけない（契約者が
    依頼者ごとに変えられる）」。⛔ 0215 まで：同じ依頼者のほかの業務は、断られた10分後でも同じ会社名でかけた。0216。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    if settings.resolve(tx, "calling.call_after_refusal", **scope):
        return False
    ref = client_ref if client_ref is not None and settings.resolve(tx, "dnc.client_ref_is_stable_id", **scope) else None
    return tx.one("SELECT salescall.declined_in_client(%s, %s, %s, %s, %s) AS d", (client_id, business_id, number.e164, ref, task_id))["d"]


def _declines(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID, business_id: UUID,
              task_id: UUID | None, after: str) -> list[datetime]:
    """同じ業務で、用件・最終目的を断られた電話のうち、その電話の業務の版の電話の型の扱い（business_settings.DECLINED_NEXT）が after のものの、終わった時刻
    （1か所）。同じお客様かはデータベースの1か所 salescall.declined_in_business（断られて終わった行の番号のどれか・依頼者が管理番号をお客様を表す変わらない番号と
    したときは管理番号＝発信禁止を広げるときと同じ設定 dnc.client_ref_is_stable_id）。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    ref = client_ref if client_ref is not None and settings.resolve(tx, "dnc.client_ref_is_stable_id", **scope) else None
    rows = tx.all("SELECT version_id, ended_at FROM salescall.declined_in_business(%s, %s, %s, %s)", (business_id, number.e164, ref, task_id))
    kinds = {v: business_settings.DECLINED_NEXT[business_settings.call_type(tx, v)] for v in {r["version_id"] for r in rows}}
    return [r["ended_at"] for r in rows if kinds[r["version_id"]] == after]


def _declined_in_business(tx: Tx, number: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                          business_id: UUID, task_id: UUID | None) -> bool:
    """同じ業務で、用件・最終目的を断られたお客様か（業務の設定 business.call_declined_again が「はい」なら、いつも いいえ）。2026-11-01 管理者：伺い8
    「業務ごとに選択できるようにする。デフォルトは案1（同じ業務では、次の実施回でもかけない）。…非推奨で案2も選べるように」。数えるのは、断られた電話の
    業務の版の電話の型の扱いが setting（確認の電話）のものだけ（顧客獲得型の断りは行を止めず、門が日数を待つ＝`declined_wait_until`）。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    if settings.resolve(tx, "business.call_declined_again", **scope):
        return False
    return bool(_declines(tx, number, client_ref=client_ref, task_id=task_id, after="setting", **scope))


def declined_wait_until(tx: Tx, task_id: UUID, dial: PhoneNumber, *, client_ref: str | None, tenant_id: UUID, client_id: UUID,
                        business_id: UUID) -> datetime | None:
    """顧客獲得型で用件・最終目的（アポ）を断られたお客様へ、同じ業務の次の実施回でかけてよい最も早い時刻（門の間隔の1か所＝gate が読む。無ければ None）：
    断られた電話（電話の型の扱いが after_days のもの）のいちばん新しい終わりから、業務の設定 business.declined_wait_days の日数。同じお客様の番号は、かけない理由と
    同じく、かける番号と、その番号と同じ方の行のほかの番号（`same_person_numbers` の1か所）。✅ 2026-11-20 ユーザー（計画書 9-9）「伺い83」。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    ends = [at for n in [dial, *same_person_numbers(tx, task_id, dial)]
            for at in _declines(tx, n, client_ref=client_ref, task_id=task_id, after="after_days", **scope)]
    if not ends:
        return None
    return max(ends) + timedelta(days=int(settings.resolve(tx, "business.declined_wait_days", **scope)))
