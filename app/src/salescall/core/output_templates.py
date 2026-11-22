"""出力の型（依頼者へ渡す形）の置き場（2026-11-22。表の作り 0280。CL-3 の連絡帳 4-1c の 66 と 5-0000000000000000 の答え・CL-2 の連絡帳 4-468 の案。
設計書 7-3-5・7-3-5b。✅ 2026-11-21 管理者「依頼者への出力はエクセルの事もあれば、システムに登録する形もあるので業務ごとに設定します。」）。

型の中身の形と、登録するときの確かめ（設計書 7-3-5 の (1)〜(6)）は出力の部品（CL-3 の output/templates.check の1か所）。ここは、置き場・版・使う型を決める
1か所だけ：
- 型は、持ち主（依頼者／業務）ごとの版の並び。足すだけ（書き換えない・消さない）。いまの型は、その持ち主のいちばん新しい版。
- 使う型（`current`）：業務のいまの版に中身があれば、それ（依頼者の型を**まるごと置き換える**＝混ぜない。✅ CL-3 の答え）、無ければ依頼者のいまの版の中身、
  どちらも無ければ None（＝既定の型。CL-3）。
- 登録（`save`）は、確かめる処理 check を必ず受け取り、問題が1つでもあれば登録しない（確かめを飛ばして登録できる道を作らない）。
- 型を持たない形に戻す（`stop_using`）＝業務なら依頼者の型に従う・依頼者なら既定の型。前の版へ戻す（`restore`）＝前の版を新しい版として足す（確かめ直す）。
- 書けるのは管理者か契約者（いま操作している方）だけ・持ち主の範囲・版の番号の振り方は、データベースが守る（0280）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from salescall.core.access import Tx

# 確かめる処理の形（CL-3 の output/templates.check）：(取引, 業務（None＝依頼者の型）, 型の中身) → 問題の並び（空＝登録してよい。文は契約者が読む言葉）
Check = Callable[[Tx, "UUID | None", dict[str, Any]], Sequence[str]]


@dataclass(frozen=True)
class Template:
    """出力の型の1つの版。business_id が None＝依頼者の型。body が None＝その持ち主は型を持たない（上に従う）。"""
    template_id: int
    client_id: UUID
    business_id: UUID | None
    version_no: int
    body: dict[str, Any] | None
    reason: str
    made_by: str
    made_at: datetime


class TemplateRejected(Exception):
    """確かめで問題が見つかったので、登録しなかった（problems＝問題の並び。契約者が読む言葉）。"""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("出力の型を登録しませんでした：" + "／".join(problems))
        self.problems = tuple(problems)


_COLUMNS = "template_id, client_id, business_id, version_no, body, reason, made_by, made_at"


def _template(row: dict[str, Any]) -> Template:
    return Template(row["template_id"], row["client_id"], row["business_id"], row["version_no"], row["body"], row["reason"],
                    row["made_by"], row["made_at"])


def current(tx: Tx, business_id: UUID) -> Template | None:
    """その業務の結果を出すときに使う型（1か所。出力の部品が、出すたびに読む）：業務のいまの版に中身があれば、それ・無ければその依頼者のいまの版の中身・
    どちらも無ければ None（＝既定の型）。業務の型は依頼者の型をまるごと置き換える（混ぜない）。読めない業務（無い・ほかの依頼者）は LookupError
    （既定の型と取り違えない）。"""
    if tx.one("SELECT 1 FROM salescall.businesses WHERE business_id = %s", (business_id,)) is None:
        raise LookupError(f"業務がありません：{business_id}")
    row = tx.one(
        f"SELECT {_COLUMNS} FROM (SELECT DISTINCT ON (t.business_id) t.* FROM salescall.output_templates t"
        "                          WHERE t.client_id = (SELECT b.client_id FROM salescall.businesses b WHERE b.business_id = %s)"
        "                            AND (t.business_id = %s OR t.business_id IS NULL)"
        "                          ORDER BY t.business_id NULLS LAST, t.version_no DESC) latest"
        " WHERE latest.body IS NOT NULL ORDER BY latest.business_id NULLS LAST LIMIT 1", (business_id, business_id))
    return None if row is None else _template(row)


def history(tx: Tx, business_id: UUID | None = None) -> list[Template]:
    """その持ち主（business_id＝業務・None＝取引の依頼者の型）の版の並び（新しい順。画面の変更の記録と「前の版へ戻す」が読む）。"""
    return [_template(r) for r in tx.all(
        f"SELECT {_COLUMNS} FROM salescall.output_templates WHERE client_id = salescall.cur_client()"
        " AND business_id IS NOT DISTINCT FROM %s ORDER BY version_no DESC", (business_id,))]


def save(tx: Tx, *, business_id: UUID | None, body: dict[str, Any], reason: str, check: Check) -> Template:
    """型を登録する（新しい版を足す。business_id＝業務・None＝取引の依頼者の型）。確かめる処理 check を必ず通し、問題が1つでもあれば登録せずに
    TemplateRejected（問題の並び）。書けるのは管理者か契約者（いま操作している方）だけ＝データベースが守る。"""
    if not isinstance(body, dict):
        raise TypeError(f"出力の型の中身は dict です（{type(body).__name__}）")
    problems = list(check(tx, business_id, body))
    if problems:
        raise TemplateRejected(problems)
    return _add(tx, business_id, body, reason)


def stop_using(tx: Tx, *, business_id: UUID | None, reason: str) -> Template:
    """その持ち主は型を持たない形に戻す（新しい版を足す。業務なら依頼者の型に従う・依頼者なら既定の型）。"""
    return _add(tx, business_id, None, reason)


def restore(tx: Tx, template_id: int, *, reason: str, check: Check) -> Template:
    """前の版へ戻す（その版の中身を、同じ持ち主の新しい版として足す＝記録は消さない）。中身のある版は、いまの確かめで確かめ直す（確かめの決まりが
    あとで増えていても、通らない型を戻さない）。型を持たない版へ戻すのは `stop_using` と同じ。読めない版（無い・ほかの依頼者）は LookupError。"""
    row = tx.one(f"SELECT {_COLUMNS} FROM salescall.output_templates WHERE template_id = %s", (template_id,))
    if row is None:
        raise LookupError(f"出力の型の版がありません：{template_id}")
    if row["body"] is None:
        return stop_using(tx, business_id=row["business_id"], reason=reason)
    return save(tx, business_id=row["business_id"], body=row["body"], reason=reason, check=check)


def _add(tx: Tx, business_id: UUID | None, body: dict[str, Any] | None, reason: str) -> Template:
    """版を1つ足す（書くのはここだけ。版の番号はデータベースが持ち主ごとに振る）。"""
    return _template(tx.one(
        "INSERT INTO salescall.output_templates (tenant_id, client_id, business_id, body, reason, made_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s::jsonb, %s, salescall.cur_actor())"
        f" RETURNING {_COLUMNS}",
        (business_id, None if body is None else json.dumps(body, ensure_ascii=False), reason)))
