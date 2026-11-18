"""お客様リストの取込の画面の部品（docs/DASHBOARD.md 5-8）。合否・止める理由・登録は core の importer だけが決める。
ここは、列と欄の対応付けの「はじめの案」と、画面の欄の読み方だけ（案は契約者が確かめて直す。プレビューで合否も見せる）。"""

from __future__ import annotations

import unicodedata
from typing import Any

from salescall.core import importer
from salescall.core.import_files import Sheet
from salescall.output import words

# 見出しの文字 → 欄（はじめの案だけ。画面の言葉と同じ見出しは、その欄。ほかはよくある書き方）
_USUAL: dict[str, str] = {"電話番号": "phone", "電話": "phone", "tel": "phone", "会社電話": "phone", "代表番号": "phone",
                          "会社名": "name", "企業名": "name", "社名": "name", "法人名": "name",
                          "担当者名": "person_name", "担当者": "person_name", "氏名": "person_full_name", "役職": "person_role",
                          "部署": "person_department", "部署名": "person_department", "携帯": "mobile", "携帯番号": "mobile",
                          "メール": "email", "email": "email", "mail": "email", "fax": "fax", "住所": "address",
                          "管理番号": "client_ref", "顧客番号": "client_ref", "かけない": "exclude", "除外": "exclude"}


def _key(s: str) -> str:
    return "".join(unicodedata.normalize("NFKC", s).lower().split())


def guess(sheet: Sheet) -> dict[str, str]:
    """欄 → 見出しの文字（はじめの案）。1つの見出しは1つの欄だけ。同じ見出しが2つある列は使わない（core も断る）。"""
    by_word = {_key(w): f for f, w in words.IMPORT_FIELD_WORDS.items()}
    usual = {_key(k): v for k, v in _USUAL.items()}
    seen: dict[str, int] = {}
    for h in sheet.header:
        if h:
            seen[h] = seen.get(h, 0) + 1
    out: dict[str, str] = {}
    for h in sheet.header:
        if not h or seen[h] != 1:
            continue
        f = by_word.get(_key(h)) or usual.get(_key(h))
        if f in importer.FIELDS and f not in out:
            out[f] = h
    return out


def mapping_from_form(form: Any, sheet: Sheet) -> dict[str, str]:
    """画面の欄（map_<欄>＝見出しの文字）から対応付けを読む。空は使わない欄。見出しに無い文字は core が断る。"""
    out = {}
    for f in importer.FIELDS:
        v = str(form.get(f"map_{f}") or "")
        if v:
            out[f] = v
    return out


def row_names(sheet: Sheet, mapping: dict[str, str]) -> dict[int, str]:
    """行番号 → 会社名の列の文字（止める行を、ファイルのどの会社か分かるように見せるだけ。合否には使わない）。"""
    title = mapping.get("name")
    if title is None or title not in sheet.header:
        return {}
    i = sheet.header.index(title)
    return {r.row_no: r.cell(i).text or "" for r in sheet.rows}


def confirmed_from_form(form: Any) -> list[tuple[str, str]]:
    """検証モードで契約者が確かめた読み（name_<n>＝表記・reading_<n>＝読み）。読みが空のものは入れない。"""
    out = []
    for k in form.keys():
        if k.startswith("name_"):
            n = k[5:]
            text, reading = str(form.get(k) or "").strip(), str(form.get(f"reading_{n}") or "").strip()
            if text and reading:
                out.append((text, reading))
    return out
