"""日本語の文字をそろえる唯一の場所（別の耳の比べ方・AIの発話の長さの見込みで使う）。

- norm：句読点・空白・かぎかっこを除く
- reading：読み（ひらがな。pykakasi）。話す長さの見込み（morae）に使う。⚠️ 文脈を見ないので、読み違いがある
  （「代わりました」＝だいわりました、「2時」＝2とき、「日本橋店」＝にほんばしみせ）。ふつうの話す速さ（turn_check）はこの読みで
  測ったので、替えるなら速さを測り直す
- compare_reading：2つの文を比べるための読み（ひらがな。単語に分ける処理＝unidic の、文脈で決めた読み）。
  「代わりました」と「変わりました」のような、同じ音で漢字だけ違う書き方を、食い違いとして数えないため。
  ⛔ 2026-10-13：pykakasi の読みで比べていたため、「代わりました」と「変わりました」が食い違いに見えた
  ⇒ 比べるところ（listen.differences など）は、すべてこれを使う
- morae：読みの長さ（おおよその音の数）。話す長さの見込みに使う（漢字の多い文と、かなの多い文を同じ物差しで測るため）
- words／word_edges：日本語を単語に分ける（fugashi＋unidic-lite。AIを使わない）。名前の洗い出しと、「」でくくる位置の判断で使う
"""
from __future__ import annotations

import re

_PUNCT = re.compile(r"[\s、。，．,.!?！？「」…・\-‐−－]")
_SMALL = set("ゃゅょぁぃぅぇぉゎ")  # 前の音と合わせて1つの音になる小さい字
_KAKASI = None
_TAGGER = None


def norm(text: str) -> str:
    return _PUNCT.sub("", text)


def reading(text: str) -> str:
    global _KAKASI
    if _KAKASI is None:
        import pykakasi

        _KAKASI = pykakasi.kakasi()
    return "".join(x["hira"] for x in _KAKASI.convert(norm(text)))


def compare_reading(text: str) -> str:
    import unicodedata

    out = []
    for w in words(unicodedata.normalize("NFKC", text)):
        kana = getattr(w.feature, "kana", None)
        out.append(kana if kana and kana != "*" else w.surface)
    s = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in "".join(out))  # カタカナをひらがなに
    return norm(s)


_DIGITS = re.compile(r"\d+")
_KANJI_DIGIT = "〇一二三四五六七八九"


def spoken_digits(text: str) -> str:
    """数字（「10」「15日」「090」）を、読みの決まる漢数字に置き換える（唯一の場所＝読みで比べる listen.differences）。別の耳は「じゅう」を「10」と書くことがあり、
    compare_reading は数字を数字のまま残すので、台本の読み（「じゅう」）と食い違いに見えた（2026-11-22 分身 AN の続きの (3)：相手役の声の「えっと、じゅう、の……」が4つの声とも
    「えっと、 10 の」と聞かれ、確かめに通らなかった）。1〜9999（頭が0でない）は位取りの漢数字（「15」＝十五）、ほか（「0」「090」・5桁から）は1字ずつ（〇九〇）。"""
    import unicodedata

    def kanji(m: re.Match) -> str:
        d = m.group()
        if d[0] == "0" or len(d) > 4:
            return "".join(_KANJI_DIGIT[int(c)] for c in d)
        out = ""
        for c, unit in zip(d.zfill(4), ("千", "百", "十", "")):
            n = int(c)
            if n:
                out += ("" if n == 1 and unit else _KANJI_DIGIT[n]) + unit
        return out
    return _DIGITS.sub(kanji, unicodedata.normalize("NFKC", text))


def morae(text: str) -> int:
    """おおよその音の数。数字は1字を2音として数える（「じゅう」「に」などの平均に近い）。"""
    n = 0
    for ch in reading(text):
        if ch in _SMALL:
            continue
        n += 2 if ch.isdigit() else 1
    return n


def words(text: str):
    """文を単語に分ける唯一の場所（単語ごとに、書き方 surface と、品詞などの情報 feature を持つ）。"""
    global _TAGGER
    if _TAGGER is None:
        import fugashi

        _TAGGER = fugashi.Tagger()
    return _TAGGER(text)


def word_edges(text: str) -> set[int]:
    """単語の切れ目の位置（文字の番号。文の頭と終わりを含む）。"""
    edges, pos = {0, len(text)}, 0
    for w in words(text):
        k = text.find(w.surface, pos)
        if k < 0:
            continue
        edges.add(k)
        pos = k + len(w.surface)
        edges.add(pos)
    return edges
