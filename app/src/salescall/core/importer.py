"""発信対象の取込（DESIGN.md 3-4・Intent.md 7-2）。持ち主 CL-2。

- 行の合否を決めるのは `_plan` の1か所だけ。プレビュー（`preview`）と登録（`register`）は、同じ `_plan` を呼ぶ
  （プレビューと登録で別々に判断すると、見せたものと違うものが入る）。
- 登録は、契約者が確かめたプレビューの指紋（`Plan.digest`）を受け取り、**登録の取引の中でもう一度 `_plan` を作って、
  指紋が同じときだけ**書く（プレビューのあとで発信禁止・設定・ほかの取込が変わっていたら、何も書かずに
  `PlanChanged`。契約者はもう一度プレビューを見る）。⇒ 登録されるのは、契約者が見たプレビューとまったく同じもの。
- 登録は1つの取引で、合格の行を全部（1行でも書けなければ何も登録しない）。止めた行は、行番号と理由だけを記録に残す。
- 番号の判断は番号の正規化（Q1）、「その相手・番号へかけてよいか」は `target_policy`（門と同じ1か所）。ここで2つ目を作らない。
- タスク・取込の記録を書くのは、この部品だけ（連絡先は contacts、最初の予定は next_action を通す。I-12）。

行の合否（上から順に。最初に当たった理由で止める）：
  (1) 空の行 ⇒ 空 (2) 列の数が見出しと合わない（CSV） (3) 対応付けた欄の問題（エラーの値など） (4) 番号が無い
  (5) 管理番号の列を対応付けたのに管理番号が無い・桁が失われている (6) 番号が「不明」・かけない種類・発信禁止（番号・管理番号。target_policy）
  (7) ファイルの上の行（合格・登録済みの行）と同じ番号 (8) 上の行と同じ管理番号
  (9) その実施回に同じ番号のタスクがある：管理番号も同じなら「もう登録済み」、違えば止める
  (10) その実施回に、同じ管理番号で別の番号のタスクがある ⇒ 止める
  (11) ここまで当たらなければ合格（登録する）
  (7)(8) は「上の行が先」：止めた行は番号・管理番号を取らない（止めた行のせいで、ほかの行を止めない）。
番号の先頭の 0 が消えた（DESIGN.md 3-4 の4）：xlsx は欄が数の型なら（確かな証拠）。CSV は列ごとに、番号の欄が
1つ残らず「0 で始まらず、0 を足すと日本の番号の決まりにぴったり合う」ときだけ。そうでなければ足さない（行は (6) で止まる）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import contacts, next_action, target_policy
from salescall.core.access import Tx
from salescall.core.import_files import Cell, Sheet
from salescall.core.phone import PhoneNumber, normalize

FIELDS = ("phone", "client_ref", "name")   # 対応付けられる欄（番号・依頼主の管理番号・名前）
_EXPONENT = re.compile(r"^\s*[+-]?\d+(\.\d+)?[eE][+-]?\d+\s*$")


class ImportRejected(Exception):
    """取込そのものを始められない（対応付けの誤り・実施回が無い）。理由は日本語。"""


class PlanChanged(Exception):
    """プレビューのあとで結果が変わった（発信禁止・設定・ほかの取込）。何も登録していない。もう一度プレビューを見る。"""


@dataclass(frozen=True)
class RowPlan:
    row_no: int
    outcome: str                  # registered（登録する／した）・stopped・already・empty
    reason_code: str | None = None
    reason: str | None = None
    number: PhoneNumber | None = None
    client_ref: str | None = None
    name: str | None = None
    source_phone: str | None = None
    task_id: UUID | None = None   # already のとき：もうあるタスク

    def key(self) -> list[Any]:
        """指紋に入れるもの（この行で、何が登録されるか・なぜ止めるか）。"""
        n = self.number
        return [self.row_no, self.outcome, self.reason_code, n.e164 if n else None, n.zero_added if n else False,
                self.client_ref, self.name, self.source_phone, str(self.task_id) if self.task_id else None]


@dataclass(frozen=True)
class Plan:
    run_id: UUID
    rows: tuple[RowPlan, ...]
    digest: str

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ("registered", "stopped", "already", "empty")}
        for r in self.rows:
            out[r.outcome] += 1
        return out


def preview(tx: Tx, run_id: UUID, sheet: Sheet, mapping: dict[str, str]) -> Plan:
    """行ごとの合否を出す（何も書かない）。"""
    return _plan(tx, run_id, sheet, mapping)


def register(tx: Tx, run_id: UUID, sheet: Sheet, mapping: dict[str, str], *, expected_digest: str) -> dict[str, Any]:
    """契約者が確かめたプレビューと同じなら、合格の行を全部登録し、行ごとの結果を記録する（1つの取引）。"""
    plan = _plan(tx, run_id, sheet, mapping)
    if plan.digest != expected_digest:
        raise PlanChanged("プレビューのあとで結果が変わりました（発信禁止・設定・ほかの取込）。もう一度プレビューを確かめてください")
    import_id = tx.one(
        "INSERT INTO salescall.imports (tenant_id, client_id, run_id, file_name, file_sha256, file_kind, encoding,"
        "                               sheet_name, mapping, plan_digest, created_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING import_id",
        (run_id, sheet.file_name, sheet.file_sha256, sheet.file_kind, sheet.encoding, sheet.sheet_name,
         json.dumps(mapping, ensure_ascii=False, sort_keys=True), plan.digest),
    )["import_id"]
    rows = []
    for r in plan.rows:
        task_id = r.task_id
        if r.outcome == "registered":
            contact_id = contacts.get_or_create(tx, r.number)
            task_id = tx.one(
                "INSERT INTO salescall.tasks (tenant_id, client_id, run_id, contact_id, client_ref, source_name,"
                "                             source_phone, source_row, import_id)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s) RETURNING task_id",
                (run_id, contact_id, r.client_ref, r.name, r.source_phone, r.row_no, import_id),
            )["task_id"]
            next_action.schedule_now(tx, task_id, cause="取込")
        tx.execute(
            "INSERT INTO salescall.import_rows (tenant_id, client_id, import_id, row_no, outcome, reason_code, reason,"
            "                                   task_id, zero_added)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s)",
            (import_id, r.row_no, r.outcome, r.reason_code, r.reason, task_id,
             bool(r.number and r.number.zero_added)),
        )
        rows.append({"row_no": r.row_no, "outcome": r.outcome, "reason_code": r.reason_code, "reason": r.reason,
                     "task_id": task_id})
    return {"import_id": import_id, "counts": plan.counts(), "rows": rows}


# ── 行の合否（プレビューと登録の両方が使う、ただ1つの判断） ──
def _columns(sheet: Sheet, mapping: dict[str, str]) -> dict[str, int]:
    unknown = set(mapping) - set(FIELDS)
    if unknown:
        raise ImportRejected(f"対応付けられない欄の名前です：{sorted(unknown)}")
    if "phone" not in mapping:
        raise ImportRejected("電話番号の列を選んでください")
    cols: dict[str, int] = {}
    for field, title in mapping.items():
        hits = [i for i, h in enumerate(sheet.header) if h is not None and h == title]
        if len(hits) != 1:
            raise ImportRejected(f"見出し「{title}」の列が{'ありません' if not hits else ' 2つ以上あります'}")
        cols[field] = hits[0]
    if len(set(cols.values())) != len(cols):
        raise ImportRejected("1つの列を、2つの欄に対応付けています")
    return cols


def _zero_lost(sheet: Sheet, col: int) -> list[bool]:
    """行ごとに、番号の先頭の 0 が消えたとして扱うか（DESIGN.md 3-4 の4）。"""
    if sheet.file_kind == "xlsx":
        return [r.cell(col).numeric for r in sheet.rows]
    texts = [r.cell(col).text for r in sheet.rows if r.cell(col).text is not None and r.cell(col).problem is None]
    whole_column = bool(texts) and all(
        normalize(t).kind == "unknown" and normalize(t, leading_zero_lost=True).kind != "unknown" for t in texts)
    return [whole_column] * len(sheet.rows)


def _plan(tx: Tx, run_id: UUID, sheet: Sheet, mapping: dict[str, str]) -> Plan:
    run = tx.one("SELECT tenant_id, client_id, business_id FROM salescall.runs WHERE run_id = %s", (run_id,))
    if run is None:
        raise ImportRejected("その実施回はありません")
    cols = _columns(sheet, mapping)
    zero_lost = _zero_lost(sheet, cols["phone"])
    in_run_by_number: dict[str, dict[str, Any]] = {}
    in_run_by_ref: dict[str, dict[str, Any]] = {}
    for t in tx.all("SELECT t.task_id, t.client_ref, c.phone_e164 FROM salescall.tasks t"
                    " JOIN salescall.contacts c ON c.client_id = t.client_id AND c.contact_id = t.contact_id"
                    " WHERE t.run_id = %s", (run_id,)):
        in_run_by_number[t["phone_e164"]] = t
        if t["client_ref"] is not None:
            in_run_by_ref[t["client_ref"]] = t
    taken_number: dict[str, int] = {}   # 番号 → それを取った上の行
    taken_ref: dict[str, int] = {}
    rows: list[RowPlan] = []

    def title(field: str) -> str:
        return sheet.header[cols[field]] or field

    for row, lost in zip(sheet.rows, zero_lost, strict=True):
        def stop(code: str, reason: str, **kw: Any) -> RowPlan:
            return RowPlan(row.row_no, "stopped", code, reason, **kw)

        if row.is_empty:
            rows.append(RowPlan(row.row_no, "empty"))
            continue
        if not row.width_ok:
            rows.append(stop("columns_mismatch", "列の数が見出しと合いません（値の中の「,」が「\"」で囲まれていない、など。"
                                                 "列がずれて、別の列の値を読む恐れがあります）"))
            continue
        cells: dict[str, Cell] = {f: row.cell(i) for f, i in cols.items()}
        bad = next((f for f in FIELDS if f in cells and cells[f].problem is not None), None)
        if bad is not None:
            rows.append(stop("cell_problem", f"「{title(bad)}」の欄：{cells[bad].problem}"))
            continue
        raw_phone = cells["phone"].text
        if raw_phone is None:
            rows.append(stop("phone_missing", "番号がありません"))
            continue
        ref = cells["client_ref"].text.strip() if "client_ref" in cells and cells["client_ref"].text else None
        if "client_ref" in cells and ref is None:
            rows.append(stop("ref_missing", "管理番号がありません（結果を依頼主の表と突き合わせる鍵です）"))
            continue
        if ref is not None and _EXPONENT.match(ref):
            rows.append(stop("ref_digits_lost", "管理番号が指数の書き方（例：1.23E+15）で、桁が失われています"))
            continue
        name = cells["name"].text.strip() if "name" in cells and cells["name"].text else None
        number = normalize(raw_phone, leading_zero_lost=lost)
        found = dict(number=number, client_ref=ref, name=name, source_phone=raw_phone.strip())
        blocked = target_policy.block_reason(tx, number, client_ref=ref, tenant_id=run["tenant_id"],
                                             client_id=run["client_id"], business_id=run["business_id"])
        if blocked is not None:
            rows.append(stop(blocked[0], blocked[1], **found))
            continue
        e164 = number.e164
        if e164 in taken_number:
            rows.append(stop("duplicate_number_in_file", f"行 {taken_number[e164]} と同じ番号です（1つの番号は1行だけ登録します）",
                             **found))
            continue
        if ref is not None and ref in taken_ref:
            rows.append(stop("duplicate_ref_in_file", f"行 {taken_ref[ref]} と同じ管理番号です", **found))
            continue
        same_number = in_run_by_number.get(e164)
        if same_number is not None:
            if same_number["client_ref"] != ref:
                rows.append(stop("number_in_run_other_ref", "この番号は、この実施回に、別の管理番号で登録されています", **found))
                continue
            plan = RowPlan(row.row_no, "already", "already_in_run", "この実施回に、もう登録されています",
                           task_id=same_number["task_id"], **found)
        elif ref is not None and ref in in_run_by_ref:
            rows.append(stop("ref_in_run", "この管理番号は、この実施回に、別の番号で登録されています", **found))
            continue
        else:
            plan = RowPlan(row.row_no, "registered", **found)
        taken_number[e164] = row.row_no
        if ref is not None:
            taken_ref[ref] = row.row_no
        rows.append(plan)

    body = {"run": str(run_id), "file": sheet.file_sha256, "sheet": sheet.sheet_name,
            "mapping": dict(sorted(mapping.items())), "rows": [r.key() for r in rows]}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return Plan(run_id, tuple(rows), digest)
