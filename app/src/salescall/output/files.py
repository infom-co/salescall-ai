"""結果のファイル（docs/DESIGN.md 7-3-1 の (3)。持ち主 CL-3）。いまは既定の型を CSV と Excel（xlsx）の2つの形で書ける（`FORMATS`。
Excel を書く部品 openpyxl は、このファイルだけで使う＝CL-2 4-439 の 6）。⏳ 業務（依頼者）ごとの出力の型（7-3-5）で書き出す部品と、型の中身の確かめ
（`output/templates.check`）は、最初に指定の形を持つ依頼者が決まったときに一緒に作る（型の置き場は core の output_templates＝0280）。
- **依頼者に埋めていただく空欄**（✅ 2026-11-18 管理者「(ア)でOK」）：CSV は「（ご記入ください）」、Excel は淡い赤の地に濃い赤の文字の空欄
  （色は result_rows の TO_FILL_BACKGROUND・TO_FILL_TEXT の1か所＝画面の見本と同じ色）。

- **書き出す直前に、こちらが作った言葉に、依頼者へ出さない言葉が無いかを確かめる**（`output/words.not_for_client` の1か所。2026-11-18 管理者の一覧）。
  当たったら出さずに止める（`ClientWordsLeak`）。お客様・依頼者のデータ（会社名など）は確かめない（依頼者のものなので）。
- CSV は、Excel でそのまま開けるように UTF-8（BOM つき）・改行は CRLF。
- 依頼者へ渡すファイルとして書き出したときだけ、提出用の控えに置く（`submit` の1か所。`backup.record_submission` を呼ぶのはここだけ）。
  渡せる結果（確定した版）が1つも無ければ渡さない（サンプル検証＝模擬通話の段では、依頼者へは何も渡さない＝2026-11-18 管理者。
  模擬通話は結果の版を作らないので、渡せる結果が無い）。同じ中身をもう控えに置いていれば、重ねて置かない（二度押し・同じファイルの渡し直し）。
- **表計算の式として動く書き出しを作らない**：セルの頭が「= + - @」・タブ・改行なら、頭に「'」を付けて文字のままにする（`_cell` の1か所。
  依頼者のファイルに来た会社名などが式の形でも、依頼者の Excel で式として動かない）。
- ファイルの名前（依頼者の名前・実施回の名前・日付）も、依頼者の目に入る。こちらが付けた実施回の名前に依頼者へ出さない言葉があれば止め、
  ファイルの名前に使えない文字は「_」にする（`file_name` の1か所）。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any

from salescall.core import access, backup, output_templates
from salescall.output import result_rows, words


class ClientWordsLeak(access.Refused):
    """依頼者へ出さない言葉が、こちらの作った言葉に入っていた（何も書き出さない）。"""


class NothingToDeliver(access.Refused):
    """依頼者へ渡せる結果（確定した版）が、まだ1つも無い。"""


class TemplateNotReady(access.Refused):
    """この業務には依頼者の指定の形（出力の型）があるが、その形で書き出す部品がまだ無い（既定の形のまま黙って渡さない）。"""


def check(rows: list[result_rows.ResultRow], *ours: str) -> None:
    """こちらが作った言葉（列の名前・行の言葉・ours＝ファイルの名前に使う実施回の名前など）に、依頼者へ出さない言葉が無いか。"""
    texts = list(result_rows.COLUMNS) + [w for r in rows for w in r.our_words()] + list(ours)   # 質問の見出しは依頼者の業務設定の文（こちらの言葉ではない）
    hits = sorted({h for t in texts for h in words.not_for_client(t)})
    if hits:
        raise ClientWordsLeak(f"依頼者へ出さない言葉が入っていたので、書き出しませんでした（{'・'.join(hits)}）。担当に直しを頼んでください")


_FORMULA_HEADS = ("=", "+", "-", "@", "\t", "\r", "\n")


def _cell(v: str) -> str:
    return "'" + v if v.startswith(_FORMULA_HEADS) else v


FORMATS: dict[str, tuple[str, str, str]] = {   # 形 → (画面の名前, 拡張子, 中身の種類)
    "csv": ("CSV", ".csv", "text/csv; charset=utf-8"),
    "xlsx": ("Excel", ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


def write(fmt: str, rows: list[result_rows.ResultRow], *ours: str) -> bytes:
    """その形でファイルを書く（1か所。どの形も、書く前に同じ確かめ `check` を通る）。"""
    if fmt == "xlsx":
        return to_xlsx(rows, *ours)
    if fmt == "csv":
        return to_csv(rows, *ours)
    raise LookupError(fmt)


def to_xlsx(rows: list[result_rows.ResultRow], *ours: str) -> bytes:
    """Excel（xlsx）。値はどれも文字として書く（式として動かさない）。依頼者に埋めていただく欄は、淡い赤の地に濃い赤の文字の空欄。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    check(rows, *ours)
    wb = Workbook()
    ws = wb.active
    ws.title = "結果"
    columns = rows[0].columns if rows else result_rows.COLUMNS
    ws.append(list(columns))
    for c in ws[1]:
        c.font = Font(bold=True)
    fill = PatternFill("solid", fgColor=result_rows.TO_FILL_BACKGROUND.lstrip("#"))
    ink = Font(color=result_rows.TO_FILL_TEXT.lstrip("#"))
    for i, r in enumerate(rows, start=2):
        for j, (col, v) in enumerate(zip(columns, r.values()), start=1):
            cell = ws.cell(row=i, column=j)
            cell.value = v
            cell.data_type = "s"   # 「=」で始まる会社名なども文字のまま（式として動かさない）
            if col in r.to_fill:
                cell.value = None
                cell.fill, cell.font = fill, ink
    for j, col in enumerate(columns, start=1):   # 列の幅：見出しと中身の長いほう（全角は2つ分）に合わせる（上限あり）
        longest = max([len(str(col))] + [len(str(v)) for r in rows for c, v in zip(columns, r.values()) if c == col])
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(60, max(8, longest * 2 + 2))
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def book(sheets: list[tuple[str, list[str], list[list[Any]]]]) -> bytes:
    """何枚ものシートの Excel（記録のすべての書き出し＝output/record_book。設計書 7-3-5b）。sheets＝[(シートの名前, 見出し, 行)]。
    値はどれも文字として書く（式として動かさない＝`_cell` と同じ守り）。日時は日本時間の「YYYY-MM-DD HH:MM:SS」の文字にして渡す（呼ぶ側）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    wb.remove(wb.active)
    for title, header, rows in sheets:
        ws = wb.create_sheet(title=title[:31])
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(["" if v is None else _cell(str(v)) for v in r])
        for j, col in enumerate(header, start=1):   # 列の幅：見出しと中身の長いほう（上限あり。行が多いときは初めの500行で決める）
            longest = max([len(str(col))] + [len(str(r[j - 1] or "")) for r in rows[:500]])
            ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(60, max(8, longest * 2 + 2))
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_csv(rows: list[result_rows.ResultRow], *ours: str) -> bytes:
    check(rows, *ours)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(rows[0].columns if rows else result_rows.COLUMNS)   # 列は実施回の行で同じ（基本の列＋質問ごとの2列）
    for r in rows:
        w.writerow([_cell(c) for c in r.cells()])
    return buf.getvalue().encode("utf-8-sig")


_NOT_IN_NAMES = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def file_name(client_name: str, run_name: str, day: date, fmt: str = "csv") -> str:
    """結果のファイルの名前（1か所）。実施回の名前は、こちらが付けた言葉なので確かめる（依頼者の名前は依頼者のもの）。"""
    check([], run_name)
    name = _NOT_IN_NAMES.sub("_", f"{client_name}_{run_name}_結果_{day.isoformat()}").strip(" .")
    return name[:115] + FORMATS[fmt][1]


def deliverable(rows: list[result_rows.ResultRow]) -> bool:
    """依頼者へ渡せるか（1か所。確定した結果が1つでもあるか）。画面のボタンも、渡す処理（submit）も、これを読む。"""
    return any(r.result for r in rows)


NOTHING_YET = "確定した結果がまだありません（確かめるための書き出しは使えます）"
TEMPLATE_NOT_READY = ("この業務には依頼者の指定の形が登録されていますが、その形での書き出しはまだできません。"
                      "既定の形のまま渡すことはしません（確かめるための書き出しは使えます）")


def why_not_deliver(tx: Any, business_id: Any, rows: list[result_rows.ResultRow]) -> str | None:
    """依頼者へ渡せない理由（1か所。渡せるなら None）。画面のボタン（押せない理由）と、渡す処理（submit）が同じものを読む。
    - 確定した結果が1つも無い。
    - 業務（か依頼者）に指定の形（出力の型）がある：使う形は core の output_templates.current の1か所で、出すたびに読む。いまの書き出しは
      既定の形だけなので、既定の形のまま黙って渡さない（指定の形で書き出す部品は ⏳＝最初に指定の形を持つ依頼者が決まったら作る。設計書 7-3-5。
      CL-2 の 4-472）。"""
    if not deliverable(rows):
        return NOTHING_YET
    if output_templates.current(tx, business_id) is not None:
        return TEMPLATE_NOT_READY
    return None


def submit(tx: Any, business_id: Any, rows: list[result_rows.ResultRow], name: str, data: bytes) -> int:
    """依頼者へ渡すファイルを、提出用の控えに置く（依頼者の取引で）。答えは控えの番号。同じ中身をもう置いていれば、重ねずに前の控えの番号
    （決めるのは core の backup.record_submission の1か所＝データベースの一意の約束。CL-2 4-431 の 31。ここで確かめ直さない）。
    どの形で作ったかも控えに残す（表の作り 0280。いまは既定の形だけ＝None）。渡せないときは why_not_deliver の理由で断る。"""
    why = why_not_deliver(tx, business_id, rows)
    if why == NOTHING_YET:
        raise NothingToDeliver(why)
    if why is not None:
        raise TemplateNotReady(why)
    return backup.record_submission(tx, name, data, template_id=None)
