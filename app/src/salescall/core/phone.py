"""番号の正規化（Q1「この番号は、どの番号か」。DESIGN.md 2章）。持ち主 CL-2。

番号の書き方のゆれ（全角・ハイフン・かっこ・空白・国番号の有無）をそろえ、種類を返す。答えるのはこの1か所だけ。
取込・発信禁止の登録・門・折り返しの照合・重複の判定は、どれもこれを呼ぶ（自分で番号を整形しない。app/checks が見張る）。

- そろえられない・決まりに当てはまらない番号は「不明」（kind = "unknown"）にし、理由を返す。推測で直さない（原則10）。
  例：内線つき・2つの番号が1つの欄にある・桁が足りない・「81」で始まるが「+」が無い・FAX と書いてある。
- 取り除くのは「番号ではないと決まっている文字」だけ（区切りの線・空白・目に見えない文字・かっこ・電話を表す頭の言葉・
  数字を含まない注記）。取り除いたあとの数字の並びが、日本の番号の決まりにぴったり合うときだけ「そろえられた」とする。
  ⇒ 2つの番号が並んでいても、桁が合わないので「不明」になる（取り除き方を広げても、誤った1つの番号にはならない）。
- 区切りの文字は、文字の種類（Unicode の分類）で取り除く（1つずつ並べると漏れるため。2026-10-13 ユーザーの確かめの指示で直した）。
- 種類のうち、システムが決してかけないもの（緊急通報などの特番・有料の情報番号・機械どうしの番号・海外・不明）は
  `system_forbidden` が True。フリーダイヤル・ナビダイヤルなどを業務でかけてよいかは、core/target_policy.py が設定で決める
  （取込と門の両方が呼ぶ）。
- 番号の決まりは、総務省の電話番号計画の一般的な形にもとづく（要確認：本番の前に、公式の番号計画で確かめる）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 種類の決まり（上から順に、最初に当たったもの）。(頭の数字, 桁数, 種類)
_RULES: tuple[tuple[str, int, str], ...] = (
    ("0120", 10, "toll_free"),   # フリーダイヤル
    ("0800", 11, "toll_free"),   # フリーダイヤル（「080-0…」と書かれていても、数字の並びはこれ）
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

# 区切りとして取り除く、種類で決まらない文字（ほかは Unicode の分類で決める：下の _is_separator）
_EXTRA_SEPARATORS = frozenset("−ー─━・.()")   # マイナス記号・長音・罫線・中黒・点・かっこ（NFKC のあと）
# 電話を表す頭の言葉（取り除く）。FAX はここに入れない（FAX の番号に電話をかけない）
_LABEL = re.compile(r"^(?:TEL|Tel|tel|PHONE|Phone|phone|電話番号|電話|携帯電話|携帯番号|携帯|代表電話|代表番号|代表|☎|📞)"
                    r"\s*[:：.．]?\s*")
_FAX = re.compile(r"(FAX|Fax|fax|ファックス|ファクス|ファクシミリ)")
_EXTENSION = re.compile(r"(内線|ext|Ext|EXT|extension|\d\s*#)")
_MULTI = re.compile(r"([,/、;&~～〜]|\bor\b|\band\b|又は|または|及び|および|もしくは)")
# 数字を含まない、かっこの注記（例：（代表）（携帯）（担当：山田））は取り除く
_NOTE = re.compile(r"[\(\[【「〔][^\d\)\]】」〕]*[\)\]】」〕]")


@dataclass(frozen=True)
class PhoneNumber:
    kind: str                 # fixed / mobile / ip / toll_free / navi_dial / fmc / premium / m2m / special / international / unknown
    e164: str | None          # 例 "+81300000001"（日本の番号で、そろえられたときだけ）
    national: str | None      # 例 "0300000001"（同上）
    reason: str | None = None  # 不明・かけない種類のときの理由（日本語）
    zero_added: bool = False   # 先頭の 0 が消えたとして 0 を足した（leading_zero_lost のときだけ。取込のプレビューで見せる）

    @property
    def system_forbidden(self) -> bool:
        return self.kind in SYSTEM_FORBIDDEN_KINDS


def _unknown(reason: str) -> PhoneNumber:
    return PhoneNumber("unknown", None, None, reason)


def _is_separator(ch: str) -> bool:
    cat = unicodedata.category(ch)
    # Z*＝空白の類（全角・ノーブレークを含む）、Cc＝改行・タブ、Cf＝目に見えない文字（幅ゼロの空白・BOM・ソフトハイフン）、
    # Pd＝ハイフン・ダッシュの類（‐ ‑ ‒ – — ― など）
    return cat.startswith("Z") or cat in ("Cc", "Cf", "Pd") or ch in _EXTRA_SEPARATORS


def normalize(raw: str | None, *, leading_zero_lost: bool = False) -> PhoneNumber:
    """依頼主のファイルのままの番号を受け取り、そろえた番号と種類を返す。

    leading_zero_lost：表計算ソフトで数として保存され、先頭の 0 が消えたと**取込が列ごとに決めた**ときだけ True
    （1つの値だけを見て決めない。例：xlsx の数の型の列。DESIGN.md 3-4）。True のとき、0 で始まらない 9〜10 桁に 0 を足す。
    """
    if raw is None:
        return _unknown("番号がありません")
    text = unicodedata.normalize("NFKC", raw).strip()
    m = re.fullmatch(r'=\s*"(.*)"', text)          # Excel の ="0312345678" の書き方
    if m:
        text = m.group(1).strip()
    text = text.strip('"').lstrip("'’").strip()     # 囲みの " と、Excel の文字の印 '
    if re.fullmatch(r"\d+\.0+", text):
        return _unknown("数として読み込まれています（例：312345678.0）。列を文字として取り込み直してください")
    if re.search(r"\d[eE][+\-]?\d", text):
        return _unknown("表計算ソフトの指数の書き方（例：3.12E+09）で、桁が失われています")
    if _FAX.search(text):
        return _unknown("FAX と書かれています（FAX の番号には電話をかけません）")
    if _EXTENSION.search(text):
        return _unknown("内線（または「#」のあとの番号）が付いています")
    if _MULTI.search(text):
        return _unknown("2つ以上の番号、または範囲が1つの欄にあります")
    text = _LABEL.sub("", text)
    text = _NOTE.sub("", text)
    compact = "".join(ch for ch in text if not _is_separator(ch))
    if not compact:
        return _unknown("番号がありません")
    if compact.startswith("#"):
        return PhoneNumber("special", None, None, "「#」で始まる番号（短縮の特番）はかけません")
    zero_added = False
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
        if compact.startswith(("184", "186")) and len(compact) > 4:
            return _unknown("184・186（番号を通知する・しないの印）が付いています。番号だけにしてください")
        if compact.startswith("1") and len(compact) <= 4:
            return PhoneNumber("special", None, None, "緊急通報などの特番（110・119・104 など）はかけません")
        if compact.startswith("010"):
            return PhoneNumber("international", None, None, "国際電話の番号はかけません")
        if compact.startswith("00"):
            return _unknown("電話会社を選ぶ番号（00 で始まる）が付いています。番号だけにしてください")
        if not compact.startswith("0"):
            if leading_zero_lost and len(compact) in (9, 10):
                compact = "0" + compact
                zero_added = True
            else:
                return _unknown("0 で始まりません（先頭の 0 が消えている、または「+」の無い「81」）")
        national = compact

    for prefix, length, kind in _RULES:
        if national.startswith(prefix):
            if len(national) != length:
                return _unknown(f"桁数が合いません（{prefix} で始まる番号は {length} 桁）")
            return _japanese(kind, national, zero_added)
    if len(national) == 10 and national[1] != "0":
        return _japanese("fixed", national, zero_added)  # 固定電話（市外局番から10桁）
    return _unknown("日本の番号の決まりに当てはまりません（桁が合わない、など）")


def _japanese(kind: str, national: str, zero_added: bool) -> PhoneNumber:
    reason = {"premium": "有料の情報番号はかけません", "m2m": "機械どうしの番号はかけません"}.get(kind)
    return PhoneNumber(kind, "+81" + national[1:], national, reason, zero_added)
