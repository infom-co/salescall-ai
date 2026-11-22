"""日本語の文字をそろえる唯一の場所（別の耳の比べ方・AIの発話の長さの見込みで使う）。

- norm：句読点・空白・かぎかっこを除く
- reading：読み（ひらがな。pykakasi）。話す長さの見込み（morae）に使う。⚠️ 文脈を見ないので、読み違いがある
  （「代わりました」＝だいわりました、「2時」＝2とき、「日本橋店」＝にほんばしみせ）。ふつうの話す速さ（turn_check）はこの読みで
  測ったので、替えるなら速さを測り直す
- compare_reading：2つの文を比べるための読み（ひらがな。単語に分ける処理＝unidic の、文脈で決めた読み）。
  「代わりました」と「変わりました」のような、同じ音で漢字だけ違う書き方を、食い違いとして数えないため。
  ⛔ 2026-10-13：pykakasi の読みで比べていたため、「代わりました」と「変わりました」が食い違いに見えた
  ⇒ 比べるところ（listen.differences など）は、すべてこれを使う
- spoken_reading：数の書き方（数字・漢数字・桁区切り・電話番号の区切り）をそろえてから compare_reading にする唯一の入口（spoken_digits）。
  測りの比べ方（listen.differences）と、本番の言い間違いの見張り（offscript）が呼ぶ
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
# 電話番号の形：0で始まる数字の並びと、それに「-」・空白・「の」でつながる数字の並び（数字のまま比べる＝区切りを外す）
_PHONE = re.compile(r"(?<![\d.,])0\d*(?:(?:\s*[-‐‑–—−ー]\s*|\s+|\s*の\s*)\d+)*")
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d,])")   # 桁区切りの「,」（「10,000」）
_BIG_UNITS = ("", "万", "億", "兆")


def _positional(d: str) -> str:
    """頭が0でない数字の並びを、位取りの漢数字にする（「15」＝十五・「1234」＝千二百三十四・「10000」＝一万・「53000」＝五万三千・「120000000」＝一億二千万）。
    兆より大きい数は数字のまま。"""
    if len(d) > 4 * len(_BIG_UNITS):
        return d
    out, groups = "", []
    while d:
        d, g = d[:-4], d[-4:]
        groups.insert(0, g)
    for g, big in zip(groups, reversed(_BIG_UNITS[:len(groups)])):
        part = ""
        for c, unit in zip(g.zfill(4), ("千", "百", "十", "")):
            n = int(c)
            if n:
                part += ("" if n == 1 and unit else _KANJI_DIGIT[n]) + unit
        out += part + big if part else ""
    return out


def spoken_digits(text: str) -> str:
    """数の書き方をそろえる唯一の場所（読みで比べる spoken_reading の中）：数字（「10」「15日」「10000円」）を、読みの決まる漢数字に置き換える。別の耳は「じゅう」を「10」と
    書くことがあり、compare_reading は数字を数字のまま残すので、台本の読み（「じゅう」）と食い違いに見えた（2026-11-22 分身 AN の続きの (3)：相手役の声の「えっと、じゅう、の……」が
    4つの声とも「えっと、 10 の」と聞かれ、確かめに通らなかった）。
    - 頭が0でない数は位取りの漢数字（「15」＝十五）。5桁からも万・億で読む（「10000」「10,000」＝一万。桁区切りの「,」は外す）＝伺い12 のイ「言い直す金額は万・千で言う」
      （`flowctl.spoken_amount` の「1万円」と、AIの文字の「10000円」がそろう）。
    - 0で始まる数字の並びと、それに「-」・空白・「の」でつながる数字の並び（電話番号の形「090-1234-5678」「090 1234 の 5678」）は、区切りを外して数字のまま（「09012345678」）。
      ✅ 2026-11-22 CL-1 が決めた（帳面 4-30「明らかに違う中身だけで止める」＝正しく言った電話番号を止めない）。⛔ それまでは「1234」を千二百三十四・「09012345678」を
      1字ずつ（〇九〇一…）にし、同じ番号でも区切りの有る無しで読みが別になった（記録の組で、本番の見張りが4組を外れと読む形だった＝分身 AO の書き置き rv192/AO/notes.md）。
      漢数字で書いた番号（「〇九〇の一二三四」）は数字に戻さない。"""
    import unicodedata

    t = _THOUSANDS.sub(lambda m: m.group().replace(",", ""), unicodedata.normalize("NFKC", text))

    def kanji(m: re.Match) -> str:
        d = m.group()
        return d if d[0] == "0" else _positional(d)
    out, pos = [], 0
    for m in _PHONE.finditer(t):
        out += [_DIGITS.sub(kanji, t[pos:m.start()]), re.sub(r"\D", "", m.group())]
        pos = m.end()
    out.append(_DIGITS.sub(kanji, t[pos:]))
    return "".join(out)


def spoken_reading(text: str) -> str:
    """2つの文を、数の書き方をそろえてから比べるための読み（唯一の入口＝測りの比べ方 listen.differences と、本番の言い間違いの見張り offscript._reading）。
    ✅ 2026-11-22 CL-1（帳面 10-18「同じ問いに答える所は1つ」）。⛔ それまで見張りは逆の向き（漢数字を数字に直してから読み＝flowctl._kanji_to_digits）で、「15日」を「15か」と
    読み、「じゅうご日」「1度」を違う読みと見ていた。"""
    return compare_reading(spoken_digits(text or ""))


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
