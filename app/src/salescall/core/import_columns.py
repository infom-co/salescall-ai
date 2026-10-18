"""依頼者のファイルの列を欄に対応付け、番号の列を読む（DESIGN.md 3-4）。持ち主 CL-2。

取込の入口は2つ（発信対象の取込＝core/importer.py・発信禁止の一覧の取込＝core/dnc_import.py）。どちらも、列の対応付け・欄の文字・
番号の先頭の 0 が消えたかの決め方は、この1か所を使う（入口ごとに作ると、同じファイルの同じ番号を、取込によって別の番号に読む）。
ファイルを行と欄に読むのは core/import_files.py、番号の意味（そろえ方・種類）は core/phone.py。
"""

from __future__ import annotations

from typing import Any

from salescall.core.import_files import Sheet
from salescall.core.phone import normalize


class ImportRejected(Exception):
    """取込そのものを始められない（対応付けの誤り・実施回が無い など）。理由は日本語。何も登録しない。"""


def columns(sheet: Sheet, mapping: dict[str, str], fields: tuple[str, ...]) -> dict[str, int]:
    """欄 → 列の位置（mapping＝欄 → 見出しの文字）。対応付けられない欄・見出しが無い／2つ以上・1つの列を2つの欄に、は断る。"""
    unknown = set(mapping) - set(fields)
    if unknown:
        raise ImportRejected(f"対応付けられない欄の名前です：{sorted(unknown)}")
    cols: dict[str, int] = {}
    for field, title in mapping.items():
        hits = [i for i, h in enumerate(sheet.header) if h is not None and h == title]
        if len(hits) != 1:
            raise ImportRejected(f"見出し「{title}」の列が{'ありません' if not hits else ' 2つ以上あります'}")
        cols[field] = hits[0]
    if len(set(cols.values())) != len(cols):
        raise ImportRejected("1つの列を、2つの欄に対応付けています")
    return cols


def text(row: Any, cols: dict[str, int], field: str) -> str | None:
    """その欄の文字（前後の空白を除く。無ければ None）。"""
    if field not in cols:
        return None
    t = row.cell(cols[field]).text
    return t.strip() if t is not None and t.strip() else None


def zero_lost(sheet: Sheet, col: int) -> list[bool]:
    """行ごとに、番号の先頭の 0 が消えたとして扱うか（DESIGN.md 3-4 の4）。xlsx は欄が数の型なら（確かな証拠）。CSV は列ごとに、
    番号の欄が1つ残らず「0 で始まらず、0 を足すと日本の番号の決まりにぴったり合う」ときだけ。そうでなければ足さない。"""
    if sheet.file_kind == "xlsx":
        return [r.cell(col).numeric for r in sheet.rows]
    texts = [r.cell(col).text for r in sheet.rows if r.cell(col).text is not None and r.cell(col).problem is None]
    # 0 が消えるのは、表計算ソフトが数として保存したとき＝欄は数字だけ。区切り（「3-1234-5678」）や言葉（「TEL:」）の
    # ある欄は、文字として打ったもので、0 が消えた証拠にならない（2026-10-16 の見直し I13）
    whole_column = bool(texts) and all(
        t.strip().isascii() and t.strip().isdigit()
        and normalize(t).kind == "unknown" and normalize(t, leading_zero_lost=True).kind != "unknown" for t in texts)
    return [whole_column] * len(sheet.rows)
