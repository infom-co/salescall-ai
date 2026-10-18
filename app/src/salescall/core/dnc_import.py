"""発信禁止の一覧の取込（Intent.md 原則14・5-3 (8)・7-2。DESIGN.md 3-6-2）。持ち主 CL-2。

依頼者から受け取った「この番号（お客様）にはかけないで」という一覧（ファイル）を、発信禁止の台帳に入れる。入口は1つ：
- **依頼者の取引で呼べば、その依頼者の台帳**（理由 client_list。欄は番号・管理番号）。
- **契約者の取引で呼べば、その契約者の共通の台帳**（その契約者のすべての依頼者に効く。欄は番号だけ）。
取り込むのは管理者か契約者だけ（データベースが守る）。

- ファイルを読むのは core/import_files.py、列の対応付け・番号の先頭の 0 が消えたかの決め方は core/import_columns.py（発信対象の
  取込と同じ1か所。取込によって同じ番号を別の番号に読まない）。番号のそろえ方は core/phone.py。
- **どこまで止めるかは core/dnc.py の `stop_set` の1か所**（依頼者の一覧は、断ったお客様と同じく、その番号・管理番号を持つ行の
  お客様の番号すべてまで止める。依頼者をまたいでは広げない。契約者の一覧は、その番号だけ）。ここで2つ目を作らない。
- 行の結果を決めるのは `_plan` の1か所だけ。プレビュー（`preview`。何も書かない）と登録（`register`）は同じ `_plan` を呼び、
  **登録の取引の中でもう一度作った結果の指紋が、管理者か契約者が確かめたプレビューと同じときだけ書く**（プレビューのあとで、
  台帳・行が変わっていたら、何も書かずに `PlanChanged`）。登録は1つの取引で全部（1行でも書けなければ何も登録しない）。
- 止めるのは「かけない」側：誤って止めても機会を失うだけで、取り消せない害は無い（Intent.md 10-3）。止めきれない行
  （番号として読めない など）は、行番号と理由を見せる（直して取り込み直す）。

行の結果（上から順に。最初に当たったもの）：
  (1) 空の行 ⇒ empty (2) 列の数が見出しと合わない（CSV）⇒ stopped (3) 対応付けた欄の問題（エラーの値など）⇒ stopped
  (4) 番号も管理番号も無い ⇒ stopped (5) 管理番号の桁が失われている（指数の書き方）⇒ stopped
  (6) 番号が番号として読めない ⇒ stopped（その行は何も止めない。番号を直して取り込み直す）
  (7) 止める番号・管理番号（広げた分も）が、どれも台帳にある・このファイルの上の行で止める ⇒ already
  (8) ここまで当たらなければ registered（新しく止める番号・管理番号の数を持つ）
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import dnc, import_columns
from salescall.core.access import Tx
from salescall.core.import_columns import ImportRejected
from salescall.core.import_files import Sheet
from salescall.core.phone import PhoneNumber, normalize

FIELDS = ("phone", "client_ref")              # 番号・依頼者の管理番号（契約者の一覧は番号だけ）
LIST_REASON = "契約者の一覧の取込"               # 契約者の共通の台帳の理由（どのファイルかは、登録が指す取込の記録にある）
_EXPONENT = re.compile(r"^\s*[+-]?\d+(\.\d+)?[eE][+-]?\d+\s*$")


class PlanChanged(Exception):
    """プレビューのあとで結果が変わった（台帳・行）。何も登録していない。もう一度プレビューを見る。"""


@dataclass(frozen=True)
class RowPlan:
    row_no: int
    outcome: str                              # registered・already・stopped・empty
    reason_code: str | None = None
    reason: str | None = None
    number: PhoneNumber | None = None
    client_ref: str | None = None
    new_numbers: tuple[str, ...] = ()         # この行で新しく止める番号（広げた分も）
    new_refs: tuple[str, ...] = ()            # この行で新しく止める管理番号

    def key(self) -> list[Any]:
        n = self.number
        return [self.row_no, self.outcome, self.reason_code, n.e164 if n else None, n.zero_added if n else False,
                self.client_ref, list(self.new_numbers), list(self.new_refs)]


@dataclass(frozen=True)
class Plan:
    ledger: str                               # client＝依頼者の台帳・contractor＝契約者の共通の台帳
    rows: tuple[RowPlan, ...]
    digest: str

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ("registered", "already", "stopped", "empty")}
        for r in self.rows:
            out[r.outcome] += 1
        out["numbers"] = sum(len(r.new_numbers) for r in self.rows)
        out["client_refs"] = sum(len(r.new_refs) for r in self.rows)
        return out


def preview(tx: Tx, sheet: Sheet, mapping: dict[str, str]) -> Plan:
    """行ごとの結果と、新しく止める番号・管理番号の数（何も書かない）。"""
    return _plan(tx, sheet, mapping)


def register(tx: Tx, sheet: Sheet, mapping: dict[str, str], *, expected_digest: str) -> dict[str, Any]:
    """確かめたプレビューと同じなら、台帳に登録し、取込と行ごとの結果を記録する（1つの取引）。"""
    plan = _plan(tx, sheet, mapping)
    if plan.digest != expected_digest:
        raise PlanChanged("プレビューのあとで結果が変わりました（発信禁止の台帳・一覧の行）。もう一度プレビューを確かめてください")
    import_id = tx.one(
        "INSERT INTO salescall.dnc_imports (tenant_id, client_id, ledger, file_name, file_sha256, file_kind, encoding,"
        "                                   sheet_name, mapping, plan_digest, created_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING import_id",
        (plan.ledger, sheet.file_name, sheet.file_sha256, sheet.file_kind, sheet.encoding, sheet.sheet_name,
         json.dumps(mapping, ensure_ascii=False, sort_keys=True), plan.digest))["import_id"]
    released = 0
    for r in plan.rows:
        if r.outcome == "registered":
            if plan.ledger == "client":
                released += dnc.register(tx, r.number, "client_list", client_ref=r.client_ref,
                                         import_id=import_id)["released_claims"]
            else:
                released += dnc.register_contractor(tx, r.number, LIST_REASON, import_id=import_id)["released_claims"]
        tx.execute(
            "INSERT INTO salescall.dnc_import_rows (tenant_id, client_id, import_id, row_no, outcome, reason_code, reason,"
            "                                       zero_added, new_numbers, new_refs)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s)",
            (import_id, r.row_no, r.outcome, r.reason_code, r.reason, bool(r.number and r.number.zero_added),
             len(r.new_numbers), len(r.new_refs)))
    return {"import_id": import_id, "counts": plan.counts(), "released_claims": released}


def _plan(tx: Tx, sheet: Sheet, mapping: dict[str, str]) -> Plan:
    scope = tx.one("SELECT salescall.cur_tenant() AS t, salescall.cur_client() AS c")
    if scope["t"] is None:
        raise ImportRejected("発信禁止の一覧は、依頼者か契約者の取引で取り込みます")
    ledger = "client" if scope["c"] is not None else "contractor"
    if ledger == "contractor" and "client_ref" in mapping:
        raise ImportRejected("契約者の共通の発信禁止は、番号だけで止めます（管理番号は依頼者ごとのものです）")
    if not {"phone", "client_ref"} & set(mapping):
        raise ImportRejected("番号の列（依頼者の一覧なら、管理番号の列でも）を選んでください")
    cols = import_columns.columns(sheet, mapping, FIELDS)
    lost = import_columns.zero_lost(sheet, cols["phone"]) if "phone" in cols else [False] * len(sheet.rows)
    planned_numbers: set[str] = set()
    planned_refs: set[str] = set()
    rows: list[RowPlan] = []

    def title(field: str) -> str:
        return sheet.header[cols[field]] or field

    for row, zero in zip(sheet.rows, lost, strict=True):
        def stop(code: str, reason: str, **kw: Any) -> RowPlan:
            return RowPlan(row.row_no, "stopped", code, reason, **kw)

        if row.is_empty:
            rows.append(RowPlan(row.row_no, "empty"))
            continue
        if not row.width_ok:
            rows.append(stop("columns_mismatch", "列の数が見出しと合いません（値の中の「,」が「\"」で囲まれていない、など。"
                                                 "列がずれて、別の列の値を読む恐れがあります）。この行は止めていません"))
            continue
        bad = next((f for f in FIELDS if f in cols and row.cell(cols[f]).problem is not None), None)
        if bad is not None:
            rows.append(stop("cell_problem", f"「{title(bad)}」の欄：{row.cell(cols[bad]).problem}。この行は止めていません"))
            continue
        raw = import_columns.text(row, cols, "phone")
        ref = import_columns.text(row, cols, "client_ref")
        if raw is None and ref is None:
            rows.append(stop("nothing", "番号も管理番号もありません"))
            continue
        if ref is not None and _EXPONENT.match(ref):
            rows.append(stop("ref_digits_lost", "管理番号が指数の書き方（例：1.23E+15）で、桁が失われています。この行は止めていません"))
            continue
        number = None if raw is None else normalize(raw, leading_zero_lost=zero)
        if number is not None and number.e164 is None:
            rows.append(stop("number_unreadable", f"番号として読めません（{number.reason}）。この行は止めていません"
                                                  "（番号を直して、取り込み直してください）", client_ref=ref))
            continue
        if ledger == "client":
            numbers, refs = dnc.stop_set(tx, number, "client_list", client_ref=ref)
        else:
            numbers, refs = [number.e164], []
        have_numbers, have_refs = dnc.active(tx, numbers, refs, contractor=ledger == "contractor")
        new_numbers = tuple(n for n in numbers if n not in have_numbers and n not in planned_numbers)
        new_refs = tuple(r for r in refs if r not in have_refs and r not in planned_refs)
        if not new_numbers and not new_refs:
            rows.append(RowPlan(row.row_no, "already", "already_stopped", "もう発信禁止です（台帳にあるか、このファイルの上の行で止めます）",
                                number=number, client_ref=ref))
            continue
        planned_numbers.update(new_numbers)
        planned_refs.update(new_refs)
        rows.append(RowPlan(row.row_no, "registered", number=number, client_ref=ref, new_numbers=new_numbers,
                            new_refs=new_refs))

    body = {"ledger": ledger, "tenant": str(scope["t"]), "client": str(scope["c"]), "file": sheet.file_sha256,
            "sheet": sheet.sheet_name, "mapping": dict(sorted(mapping.items())), "rows": [r.key() for r in rows]}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return Plan(ledger, tuple(rows), digest)
