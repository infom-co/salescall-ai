"""発信対象の取込（DESIGN.md 3-4・Intent.md 7-2）。持ち主 CL-2。

- 行の合否を決めるのは `_plan` の1か所だけ。プレビュー（`preview`）と登録（`register`）は、同じ `_plan` を呼ぶ
  （プレビューと登録で別々に判断すると、見せたものと違うものが入る）。
- 登録は、契約者が確かめたプレビューの指紋（`Plan.digest`）を受け取り、**登録の取引の中でもう一度 `_plan` を作って、
  指紋が同じときだけ**書く（プレビューのあとで発信禁止・設定・ほかの取込が変わっていたら、何も書かずに
  `PlanChanged`。契約者はもう一度プレビューを見る）。⇒ 登録されるのは、契約者が見たプレビューとまったく同じもの。
- 登録は1つの取引で、合格の行を全部（1行でも書けなければ何も登録しない）。止めた行は、行番号と理由だけを記録に残す。
- 番号の判断は番号の正規化（Q1）、「そのお客様・番号へかけてよいか」は `target_policy`（門と同じ1か所）。ここで2つ目を作らない。
- タスク・取込の記録を書くのは、この部品だけ（連絡先は contacts、最初の予定は next_action を通す。I-12）。
- **名前の読み**（お客様の会社名・目的のお客様の名前。Intent.md 6-8）：決め方は core/readings.py の1か所（業務設定と同じ）。
  読みの列があればそれ、無ければ AI の読み。**読みが決まらない名前でも止めない**（読みは空、考えられる読みとまず言ってみる読みを
  持って登録し、会話の中で確かめる。2026-10-16 ユーザー・CL-1 の連絡帳 4-15）。検証モードの業務では、AI が決めた読みを契約者が
  確かめてから登録する（確かめていない行は止めて、プレビューに確かめる名前を出す）。読みの列の値が読みとして使えない（漢字など）
  行は、入口のデータの誤りとして止める。**AI に聞くのはプレビューの1回だけ**（`preview` が聞き、`Plan.ai_readings` を
  登録にそのまま渡す。もう一度聞くと答えが変わりうる＝契約者が見たものと違う中身になる）。
- **目的のお客様の名前・役職**：要るかは業務設定の版の1か所（`Settings.target_needs`）。実施回に版があれば、その版で確かめる
  （要る列を選んでいなければ取込を始めない・その欄が空の行は止める）。版をあとで割り当てるときも、同じ決まりで確かめる
  （business_settings.assign）。門も同じ決まりで確かめる。

行の合否（上から順に。最初に当たった理由で止める）：
  (1) 空の行 ⇒ 空 (2) 列の数が見出しと合わない（CSV） (3) 対応付けた欄の問題（エラーの値など）・「かけない」の列の値が読めない (4) 番号が無い
  (5) 管理番号の列を対応付けたのに管理番号が無い・桁が失われている (6) 番号が「不明」・かけない種類・発信禁止（番号・管理番号。target_policy）
  (7) ファイルの上の行（合格・登録済みの行）と同じ番号 (8) 上の行と同じ管理番号
  (9) その実施回に同じ番号のタスクがある：管理番号も同じなら「もう登録済み」、違えば止める
  (10) その実施回に、同じ管理番号で別の番号のタスクがある ⇒ 止める
  (11) 目的のお客様の名前・役職が要るのに無い ⇒ 止める
  (12) 名前（会社名・目的のお客様の名前）の読みの列の値が読みとして使えない・検証モードで AI が決めた読みを契約者が確かめていない
       ⇒ 止める（読みが決まらないだけなら止めない）
  (13) ここまで当たらなければ合格（登録する）
  「かけない」の列で付けた行は、ほかの行と同じ確かめを通して登録し（もう登録済みの行も）、「かけない」の印を付ける（exclusions。
  ダッシュボードで戻せる。2026-10-18 ユーザー決定）。
  (7)(8) は「上の行が先」：止めた行は番号・管理番号を取らない（止めた行のせいで、ほかの行を止めない）。
番号の先頭の 0 が消えた（DESIGN.md 3-4 の4）：xlsx は欄が数の型なら（確かな証拠）。CSV は列ごとに、番号の欄が
1つ残らず「0 で始まらず、0 を足すと日本の番号の決まりにぴったり合う」ときだけ。そうでなければ足さない（行は (6) で止まる）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import (approval, business_settings, contacts, exclusions, import_columns, next_action, readings,
                            target_policy)
from salescall.core.access import Tx
from salescall.core.import_columns import ImportRejected
from salescall.core.import_files import Cell, Sheet
from salescall.core.phone import PhoneNumber, normalize

FIELDS = ("phone", "client_ref", "name", "name_reading", "person_name", "person_name_reading", "person_role", "exclude")
# 対応付けられる欄：番号・依頼者の管理番号・お客様の名前（会社名など）とその読み・目的のお客様の名前とその読み・役職・
# 「かけない」（2026-10-18 ユーザー決定。読み方は exclusions.read_mark の1か所。付けた行は登録して「かけない」の印を付ける）
_READING_OF = {"name": "name_reading", "person_name": "person_name_reading"}   # 名前の欄 → 読みの欄


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
    name_reading: readings.Reading = readings.UNDECIDED
    person_name: str | None = None
    person_reading: readings.Reading = readings.UNDECIDED
    person_role: str | None = None
    excluded: bool = False        # 一覧の「かけない」の列で付けた（登録する行・もう登録済みの行に「かけない」の印を付ける）

    def key(self) -> list[Any]:
        """指紋に入れるもの（この行で、何が登録されるか・なぜ止めるか）。"""
        n = self.number
        return [self.row_no, self.outcome, self.reason_code, n.e164 if n else None, n.zero_added if n else False,
                self.client_ref, self.name, self.source_phone, str(self.task_id) if self.task_id else None,
                self.name_reading.as_json(), self.person_name, self.person_reading.as_json(), self.person_role, self.excluded]


@dataclass(frozen=True)
class Plan:
    run_id: UUID
    rows: tuple[RowPlan, ...]
    digest: str
    ai_readings: dict[str, dict[str, Any]]        # AI の読み（readings.to_json の形。登録にそのまま渡す。聞き直さない）
    to_confirm: tuple[dict[str, Any], ...]        # 検証モードで契約者が確かめる名前（表記・AI の案・行番号）

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ("registered", "stopped", "already", "empty")}
        for r in self.rows:
            out[r.outcome] += 1
        return out


def preview(tx: Tx, run_id: UUID, sheet: Sheet, mapping: dict[str, str], *, confirmed_names: Any = (),
            suggest_readings: readings.Suggester | None = None) -> Plan:
    """行ごとの合否を出す（何も書かない）。読みの無い名前は、AI に1回だけ聞く（答えは Plan.ai_readings。登録に渡す）。
    confirmed_names＝契約者が確かめた（直した）読み（(表記, 読み) の並び）。"""
    cols = _columns(sheet, mapping)
    confirmed = {t for t, _ in confirmed_names}
    texts = []
    for row in sheet.rows:
        if row.is_empty or not row.width_ok:
            continue
        for field, reading_field in _READING_OF.items():
            text, given = _text(row, cols, field), _text(row, cols, reading_field)
            if text and not given and text not in confirmed:
                texts.append(text)
    return _plan(tx, run_id, sheet, mapping, confirmed_names=confirmed_names,
                 ai=readings.ask(texts, suggest_readings))


def _read(text: str | None, given: str | None, *, confirmed: dict[str, str],
          ai: dict[str, readings.Reading]) -> tuple[readings.Reading, str | None]:
    """その名前の読み（決め方は readings の1か所）と、読みの列の値が使えないときの理由。"""
    if text is None:
        return readings.UNDECIDED, None
    try:
        return readings.resolve(text, given, confirmed=confirmed, ai=ai), None
    except ValueError as e:
        return readings.UNDECIDED, str(e)


def register(tx: Tx, run_id: UUID, sheet: Sheet, mapping: dict[str, str], *, expected_digest: str,
             confirmed_names: Any = (), ai_readings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """契約者が確かめたプレビューと同じなら、合格の行を全部登録し、行ごとの結果を記録する（1つの取引）。
    AI には聞き直さない（ai_readings＝プレビューの Plan.ai_readings）。"""
    plan = _plan(tx, run_id, sheet, mapping, confirmed_names=confirmed_names, ai=readings.from_json(ai_readings))
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
                "                             source_phone, source_row, import_id, source_name_reading, source_name_reading_by,"
                "                             source_name_reading_candidates, source_name_reading_likely,"
                "                             target_name, target_name_reading, target_name_reading_by,"
                "                             target_name_reading_candidates, target_name_reading_likely, target_role)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s)"
                " RETURNING task_id",
                (run_id, contact_id, r.client_ref, r.name, r.source_phone, r.row_no, import_id,
                 r.name_reading.reading, r.name_reading.by, list(r.name_reading.candidates), r.name_reading.likely,
                 r.person_name, r.person_reading.reading, r.person_reading.by, list(r.person_reading.candidates),
                 r.person_reading.likely, r.person_role),
            )["task_id"]
            next_action.schedule_now(tx, task_id, cause="取込")
        if r.excluded and r.outcome in ("registered", "already"):
            exclusions.mark(tx, [task_id], excluded=True, reason=exclusions.IMPORT_REASON, source="import", import_id=import_id)
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
    if "phone" not in mapping:
        raise ImportRejected("電話番号の列を選んでください")
    for field, reading_field in _READING_OF.items():
        if reading_field in mapping and field not in mapping:
            raise ImportRejected("読みの列だけを選んでいます（名前の列も選んでください）")
    return import_columns.columns(sheet, mapping, FIELDS)


_text = import_columns.text
_zero_lost = import_columns.zero_lost


def _plan(tx: Tx, run_id: UUID, sheet: Sheet, mapping: dict[str, str], *, confirmed_names: Any,
          ai: dict[str, readings.Reading]) -> Plan:
    run = tx.one("SELECT tenant_id, client_id, business_id, salescall.run_current_version(run_id) AS version_id"
                 " FROM salescall.runs WHERE run_id = %s", (run_id,))
    if run is None:
        raise ImportRejected("その実施回はありません")
    cols = _columns(sheet, mapping)
    # 目的のお客様に要る列（実施回に版があれば、その版の業務設定の1か所で決まる。版が無ければ、割り当てるときに確かめる）
    needs = None
    if run["version_id"] is not None:
        try:
            needs = business_settings.load(tx, run["version_id"]).target_needs()
        except (LookupError, business_settings.SettingsInvalid, business_settings.business_rules.RulesMissing,
                business_settings.business_rules.RulesInvalid) as e:
            raise ImportRejected(f"この実施回の業務設定の版が、確かめに通りません（{e}）。版を直してから取り込んでください") from None
    if needs is not None and needs.name and "person_name" not in cols:
        raise ImportRejected("この業務は目的のお客様を名前で呼ぶので、目的のお客様の名前の列を選んでください")
    if needs is not None and needs.role and "person_role" not in cols:
        raise ImportRejected("この業務は目的のお客様を役職で呼ぶので、目的のお客様の役職の列を選んでください")
    verification = approval.human_for_business(tx, run["business_id"])   # 検証モードなら、AI の読みの案を契約者が確かめる
    confirmed = {t: rd for t, rd in confirmed_names}
    to_confirm: dict[tuple[str, str], list[int]] = {}
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
        bad = next((f for f in FIELDS if f in cells and f != "exclude" and cells[f].problem is not None), None)
        if bad is not None:
            rows.append(stop("cell_problem", f"「{title(bad)}」の欄：{cells[bad].problem}"))
            continue
        skip = exclusions.read_mark(cells["exclude"]) if "exclude" in cells else False
        if skip is None:
            rows.append(stop("exclude_unreadable", f"「{title('exclude')}」の欄の値が読めません（空・かけない・○・レ・1・TRUE など"
                                                   "と、かける・0・FALSE などのどれかで書いてください）"))
            continue
        raw_phone = cells["phone"].text
        if raw_phone is None:
            rows.append(stop("phone_missing", "番号がありません"))
            continue
        ref = cells["client_ref"].text.strip() if "client_ref" in cells and cells["client_ref"].text else None
        if "client_ref" in cells and ref is None:
            rows.append(stop("ref_missing", "管理番号がありません（結果を依頼者の表と突き合わせる鍵です）"))
            continue
        if import_columns.ref_digits_lost(ref):
            rows.append(stop("ref_digits_lost", import_columns.REF_DIGITS_LOST))
            continue
        name = _text(row, cols, "name")
        number = normalize(raw_phone, leading_zero_lost=lost)
        person_name, person_role = _text(row, cols, "person_name"), _text(row, cols, "person_role")
        name_reading, bad_name = _read(name, _text(row, cols, "name_reading"), confirmed=confirmed, ai=ai)
        person_reading, bad_person = _read(person_name, _text(row, cols, "person_name_reading"), confirmed=confirmed, ai=ai)
        found = dict(number=number, client_ref=ref, name=name, source_phone=raw_phone.strip(), name_reading=name_reading,
                     person_name=person_name, person_reading=person_reading, person_role=person_role, excluded=skip)
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
            short = needs.missing(name=person_name, role=person_role) if needs is not None else None
            if short is not None:
                rows.append(stop(short[0], short[1], **found))
                continue
            named = ((name, name_reading), (person_name, person_reading))
            for text, got in named:              # 検証モードで契約者が確かめる名前（行の中のすべて。止める理由とは別に出す）
                if text is not None and got.reading is not None and readings.needs_person(got, verification=verification):
                    to_confirm.setdefault((text, got.reading), []).append(row.row_no)
            why = ("reading_invalid", bad_name or bad_person) if (bad_name or bad_person) else _reading_problem(named, verification)
            if why is not None:
                rows.append(stop(why[0], why[1], **found))
                continue
            plan = RowPlan(row.row_no, "registered", **found)
        taken_number[e164] = row.row_no
        if ref is not None:
            taken_ref[ref] = row.row_no
        rows.append(plan)

    body = {"run": str(run_id), "file": sheet.file_sha256, "sheet": sheet.sheet_name,
            "mapping": dict(sorted(mapping.items())), "rows": [r.key() for r in rows]}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    confirm = tuple({"text": t, "reading": rd, "rows": nos} for (t, rd), nos in sorted(to_confirm.items()))
    return Plan(run_id, tuple(rows), digest, readings.to_json(ai), confirm)


def _reading_problem(names: tuple[tuple[str | None, readings.Reading], ...],
                     verification: bool) -> tuple[str, str] | None:
    """名前の読みで止める理由（理由の名前と日本語）。読みの決め方そのものは core/readings.py。読みが決まらないだけなら止めない
    （会話の中で確かめる）。"""
    for text, got in names:
        if text is None:
            continue
        if readings.needs_person(got, verification=verification):
            return ("reading_unconfirmed", f"名前「{text}」の読み「{got.reading}」は AI が決めた読みです。検証モードでは、契約者が"
                                           "確かめてから登録します")
    return None
