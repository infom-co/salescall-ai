"""業務を組む AI が示した出典の文が、依頼者の資料の中に本当にあるかを確かめる（1か所。持ち主 CL-2。DESIGN.md 6-5）。

✅ 2026-11-05 ユーザー：「案A」（組む AI が、どの項目にも元の資料の文を添え、資料に書いていないことは決めずに「決まらなかった所」として
出す）。CL-1 の連絡帳 4-92 の (b)：「出典は、プログラムが資料の中にそのまま在るかを確かめてください：AI が添えた出典を信じるだけでは、
推し量りを防げません（それらしい文を添えて誤った値を入れうる）。資料の文と照らして見つからない項目は、値を入れずに「決まらなかった所」に
します（確かめは CL-2 の1か所）。」

- 照らす文字は、資料を文字にする1か所（core/import_files.read_document）の文字そのもの（組む AI に渡す文字と同じもの）。
- 照らし方（`normalize` の1か所）：文字の形をそろえ（NFKC＝全角の英数字・記号と半角のカタカナなど）、空白・改行・タブを除いてから、
  出典の文が資料の文字の中に一字一句そのまま在るかを見る。似た文を探す・言い換えを許すことはしない（推し量らない。見つからなければ、
  値を入れずに契約者に見せる側へ倒す）。
- 項目の値と出典の文の組の形（どの項目に何を入れるか）は、組む AI への指示の文と一緒に CL-1 が持つ（4-92 の (2)）。ここは、組の
  1つずつについて「出典が資料にあるか」だけを答える。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from salescall.core.import_files import Document

_SPACE = re.compile(r"\s+")
# NFKC でもそろわない、見た目の同じ記号（見直し(B) の6：資料の「9:00〜18:00」（波ダッシュ）を AI が「～」「~」で写すと、照らして
# 「無い」になった）。波の記号はチルダに、ハイフン・ダッシュ・マイナスの類はハイフンにそろえる。長音「ー」はそろえない（言葉の一部）
_SAME_MARKS = str.maketrans({"\u301c": "~", "\uff5e": "~", "\u2053": "~", "\u223c": "~",
                             "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
                             "\u2212": "-", "\ufe63": "-", "\uff0d": "-"})


def normalize(text: str) -> str:
    """照らすための文字の形（1か所）：NFKC で形をそろえ、見た目の同じ記号をそろえ（`_SAME_MARKS`）、空白・改行・タブを除く。"""
    return _SPACE.sub("", unicodedata.normalize("NFKC", text).translate(_SAME_MARKS))


@dataclass(frozen=True)
class Checked:
    """組む AI の項目1つを照らした結果。found＝出典が見つかった資料の名前（見つからなければ None）、why＝見つからない理由（日本語）。"""
    key: str
    value: Any
    quote: str | None
    found: str | None
    why: str | None

    @property
    def confirmed(self) -> bool:
        return self.found is not None


def locate(documents: Iterable[Document], quote: str | None) -> str | None:
    """出典の文が、どの資料にあるか（資料の名前。無ければ None）。"""
    q = normalize(quote) if isinstance(quote, str) else ""
    if not q:
        return None
    for d in documents:
        if q in normalize(d.text):
            return d.file_name
    return None


def check(documents: Iterable[Document], items: Iterable[tuple[str, Any, str | None]]) -> tuple[Checked, ...]:
    """組む AI の項目（項目の名前・値・出典の文）を1つずつ資料と照らす（1か所）。出典の文が無い・資料に見つからない項目は、
    confirmed が偽（呼ぶ側は値を入れずに「決まらなかった所」にする）。"""
    docs = tuple(documents)
    out = []
    for key, value, quote in items:
        if not (isinstance(quote, str) and normalize(quote)):
            out.append(Checked(key, value, quote, None, "出典の文がありません（資料のどの文から取ったかが分かりません）"))
            continue
        found = locate(docs, quote)
        out.append(Checked(key, value, quote, found,
                           None if found is not None else "出典の文が、資料の中に見つかりません（資料に書いていない値かもしれません）"))
    return tuple(out)
