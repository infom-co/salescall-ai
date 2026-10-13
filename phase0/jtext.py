"""日本語の文字をそろえる唯一の場所（別の耳の比べ方・AIの発話の長さの見込みで使う）。

- norm：句読点・空白・かぎかっこを除く
- reading：読み（ひらがな）にそろえる（「ない」と「無い」のような書き方の違いを、食い違いとして数えないため）
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
