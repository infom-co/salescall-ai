"""番号の正規化（Q1「この番号は、どの番号か」。DESIGN.md 2章）。持ち主 CL-2。

番号の書き方のゆれ（全角・ハイフン・かっこ・空白・国番号の有無）をそろえ、種類を返す。答えるのはこの1か所だけ。
取込・発信禁止の登録・門・折り返しの照合・重複の判定は、どれもこれを呼ぶ（自分で番号を整形しない。app/checks が見張る）。

- そろえられない・決まりに当てはまらない番号は「不明」（kind = "unknown"）にし、理由を返す。推測で直さない（原則10）。
  例：内線つき・2つの番号が1つの欄にある・桁が足りない・「81」で始まるが「+」が無い（国番号か打ち間違いか分からない）。
- 種類のうち、システムが決してかけないもの（緊急通報などの特番・有料の情報番号・機械どうしの番号・海外）は
  `system_forbidden` が True。フリーダイヤル・ナビダイヤルなどを依頼主の業務でかけてよいかは、門（Q2）が設定で決める。
- 番号の決まりは、総務省の電話番号計画の一般的な形にもとづく（要確認：本番の前に、公式の番号計画で確かめる）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 書き方のゆれとして取り除く文字（全角は NFKC で半角になってから見る）。
# 「ー」（長音）・「―」「‐」「−」「–」「—」は、ハイフンの代わりに打たれることがある。
_SEPARATORS = re.compile(r"[\s\-‐‑‒–—―−ー\.\(\)]")

# 種類の決まり（上から順に、最初に当たったもの）。(頭の数字, 桁数, 種類)
_RULES: tuple[tuple[str, int, str], ...] = (
    ("0120", 10, "toll_free"),   # フリーダイヤル
    ("0800", 11, "toll_free"),   # フリーダイヤル
    ("0570", 10, "navi_dial"),   # ナビダイヤル（通話料がかかる。Rakuten Link の無料通話の対象外）
    ("0180", 10, "premium"),     # テレドーム など
    ("0990", 10, "premium"),     # 情報料つきの番号
    ("020", 11, "m2m"),          # 機械どうしの番号
    ("050", 11, "ip"),           # IP電話
    ("060", 11, "fmc"),          # FMC
    ("070", 11, "mobile"),
    ("080", 11, "mobile"),
    ("090", 11, "mobile"),
)
SYSTEM_FORBIDDEN_KINDS = frozenset({"special", "premium", "m2m", "international", "unknown"})


@dataclass(frozen=True)
class PhoneNumber:
    kind: str                 # fixed / mobile / ip / toll_free / navi_dial / fmc / premium / m2m / special / international / unknown
    e164: str | None          # 例 "+81300000001"（日本の番号で、そろえられたときだけ）
    national: str | None      # 例 "0300000001"（同上）
    reason: str | None = None  # 不明・かけない種類のときの理由（日本語）

    @property
    def system_forbidden(self) -> bool:
        return self.kind in SYSTEM_FORBIDDEN_KINDS


def _unknown(reason: str) -> PhoneNumber:
    return PhoneNumber("unknown", None, None, reason)


def normalize(raw: str | None) -> PhoneNumber:
    """依頼主のファイルのままの番号を受け取り、そろえた番号と種類を返す。"""
    if raw is None:
        return _unknown("番号がありません")
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return _unknown("番号がありません")
    if re.search(r"(内線|ext|EXT|Ext|[,/、・;]|～|~)", text):
        return _unknown("内線つき、または2つ以上の番号が1つの欄にあります")
    compact = _SEPARATORS.sub("", text)
    if compact.startswith("#"):
        return PhoneNumber("special", None, None, "「#」で始まる番号（短縮の特番）はかけません")
    if compact.startswith("+"):
        digits = compact[1:]
        if not digits.isdigit():
            return _unknown("番号以外の文字があります")
        if not digits.startswith("81"):
            return PhoneNumber("international", None, None, "海外の番号はかけません")
        rest = digits[2:]
        if rest.startswith("0"):  # 「+81 (0)3 …」と書かれたもの
            rest = rest[1:]
        national = "0" + rest
    else:
        if not compact.isdigit():
            return _unknown("番号以外の文字があります")
        if compact.startswith("1") and len(compact) <= 4:
            return PhoneNumber("special", None, None, "緊急通報などの特番（110・119・104 など）はかけません")
        if compact.startswith("010"):
            return PhoneNumber("international", None, None, "国際電話の番号はかけません")
        if compact.startswith("00"):
            return _unknown("電話会社を選ぶ番号（00 で始まる）が付いています。番号だけにしてください")
        if not compact.startswith("0"):
            return _unknown("0 で始まりません（「81」で始まるなら「+81」と書いてください）")
        national = compact

    for prefix, length, kind in _RULES:
        if national.startswith(prefix):
            if len(national) != length:
                return _unknown(f"桁数が合いません（{prefix} で始まる番号は {length} 桁）")
            return _japanese(kind, national)
    if len(national) == 10 and national[1] != "0":
        return _japanese("fixed", national)  # 固定電話（市外局番から10桁）
    return _unknown("日本の番号の決まりに当てはまりません")


def _japanese(kind: str, national: str) -> PhoneNumber:
    reason = {"premium": "有料の情報番号はかけません", "m2m": "機械どうしの番号はかけません"}.get(kind)
    return PhoneNumber(kind, "+81" + national[1:], national, reason)
