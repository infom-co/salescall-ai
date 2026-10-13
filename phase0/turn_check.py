"""AIの1回の発話（1ターン）を見分ける唯一の場所（DESIGN.md 6-7 の「AIが黙ったとき・途中で止まったとき」の試作）。

分け方（どれか1つ）：
- "silent"     … 声が出ていない（声のある長さが MIN_VOICED_S 未満）。または、決めた秒数のうちにターンが終わらなかった
- "wrong_language" … 業務の言葉（日本語）でない言葉を話した（2026-10-13 G-17：AIが考えていることを英語で口に出した
                   「To read this script, I'll project a polite ...」。長さはたまたま見合っていて、長さの見分けでは拾えなかった）
                   見分けは foreign_text の1か所：ローマ字の単語が4つ以上続くか、日本語の文字・ローマ字以外の文字（ハングルなど）
                   ⚠️ 何を話したかは、別の耳の文字（heard_text）があれば、そちらで決める。AI自身の文字は、声と違うことがある
                   （2026-10-13 G-5：声は日本語なのに、AI自身の文字は「ご 계약의 확정에 대해서는…」と韓国語で書かれていた）
- "cut_off"    … 言葉の長さに比べて、声が短すぎる（途中で止まった）
- "too_long"   … 言葉の長さに比べて、声が長すぎる（同じことを2回言った など）
- "ok"         … 言葉の長さと声の長さが見合っている
- "unknown"    … 比べる言葉が無い（AI自身の文字起こしが届かず、言うべき言葉も分からない）ので、長さでは見分けられない

比べる言葉は、(1) 言うべき言葉が分かっていれば、それ（台本・プログラムが渡した言い直す値など）、
(2) 分からなければ、AI自身の文字起こし（言うつもりだった言葉）。
⚠️ 2026-10-13 の実測（G-17）：途中で止まった7回のうち6回は、AI自身の文字起こしも届いていなかった。
   ⇒ 会話の中で言うべき言葉が分からない発話では、途中で止まっても多くは "unknown" になる。通話後の検証で拾う。
"""
from __future__ import annotations

import re
import unicodedata

from audio import voiced_seconds
from jtext import morae

# ローマ字の単語が続く所（業務の言葉が日本語のとき、これが出たら日本語でない言葉を話したとみなす）
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’]*(?:[\s,.]+[A-Za-z][A-Za-z'’]*){3,}")

# 日本語の文の中にあってよい文字の種類（文字の名前の頭）。名前のローマ字（「Pro」「ABC」）は、続く単語の数で見る
_JA_LETTERS = ("CJK", "HIRAGANA", "KATAKANA", "HALFWIDTH KATAKANA", "IDEOGRAPHIC", "LATIN", "FULLWIDTH LATIN")


def foreign_text(text: str) -> str | None:
    """業務の言葉（日本語）でない部分の、最初の所（無ければ None）。"""
    m = _LATIN_RUN.search(text)
    if m:
        return m.group(0)[:80]
    for i, ch in enumerate(text):
        if unicodedata.category(ch).startswith("L") and not unicodedata.name(ch, "").startswith(_JA_LETTERS):
            return text[i:i + 20]
    return None


MIN_VOICED_S = 0.2
# 声ごとの、ふつうの話す速さ（1秒あたりの音の数の真ん中の値）。G-17 の1文ずつの形、途中で止まった回を除く
# （Sadachbia 216ターン・Leda 226ターン、2026-10-13）。声の型ごとに測り直して設定の値で持つ（フェーズ1）。
NORMAL_MORAE_PER_S = {"Sadachbia": 9.0, "Leda": 8.3}
# 声の長さ ÷ 見込みの長さ が、これより小さければ途中で止まった・大きければ長すぎ。
# G-17：ふつうの発話は Sadachbia で見込みの 0.78倍まで、Leda で 0.81倍まで縮んだ。止まった発話は 0.75倍以下（Sadachbia）・0.72倍以下（Leda）。
#   ⇒ 余裕が小さい（0.03〜0.06）。線の引き方は、フェーズ1の通話の記録で、見逃しと誤りの数を数えてから決める。
CUT_RATIO = 0.77
LONG_RATIO = 1.8
# 相手が話し終えてから、この秒数のあいだ声が出なければ、1回だけ返事を促す（DESIGN.md 6-7）。
# ふつうの返事の間は 0.7〜1.5秒、最大でも約2秒（G-11・G-19）。29.6秒黙った回があった（G-19）。
SILENCE_NUDGE_S = 4.0
# 試験で「不合格」に数える種類（どの試験もここだけを見る＝一本化）。
# ⛔ 2026-10-13 の誤り：G-13・G-16 は "silent" と "cut_off" しか数えず、AIが英語を話した回（G-16c）を合格にしていた。
# "unknown"（見分けられなかった）も、合格とは言えないので数える。
BAD_KINDS = ("silent", "wrong_language", "cut_off", "too_long", "unknown")


def bad_kinds(turns: list[dict]) -> list[str]:
    """通話の中で、見張りが引っかかったターンの種類（無ければ空）。"""
    return [t["check"]["kind"] for t in turns if t["check"]["kind"] in BAD_KINDS]


def check_ai_turn(*, audio: bytes, rate: int, voice: str, timed_out: bool = False,
                  expected_text: str | None = None, intended_text: str | None = None, heard_text: str | None = None) -> dict:
    voiced = voiced_seconds(audio, rate) if audio else 0.0
    return classify(voiced_s=voiced, voice=voice, timed_out=timed_out, expected_text=expected_text,
                    intended_text=intended_text, heard_text=heard_text)


def classify(*, voiced_s: float, voice: str, timed_out: bool = False,
             expected_text: str | None = None, intended_text: str | None = None, heard_text: str | None = None) -> dict:
    """声のある長さから分ける（記録の見直しでも、同じこの処理を使う）。
    heard_text：別の耳がAIの声を文字にしたもの（口から出た言葉）。あれば、何語を話したかはこれで決める。"""
    voiced = voiced_s
    text = expected_text or intended_text or ""
    if text and heard_text and not expected_text and foreign_text(text) and not foreign_text(heard_text):
        text = heard_text  # AI自身の文字が日本語の読みにできない（声と違う文字で書かれた）ので、長さは別の耳の文字で比べる
    out = {"voiced_s": round(voiced, 2), "compared_with": "expected" if expected_text else ("heard" if text and text == heard_text
                                                                                             else ("intended" if intended_text else None))}
    if timed_out or voiced < MIN_VOICED_S:
        return {**out, "kind": "silent", "timed_out": timed_out}
    said = heard_text or intended_text  # 別の耳が何も文字にしなかったときは、AI自身の文字で見る
    if said and foreign_text(said):
        return {**out, "kind": "wrong_language", "foreign": foreign_text(said), "language_from": "heard" if heard_text else "intended"}
    if not text:
        return {**out, "kind": "unknown"}
    need = morae(text) / NORMAL_MORAE_PER_S[voice]
    ratio = voiced / need if need else None
    out.update({"morae": morae(text), "expected_s": round(need, 2), "ratio": round(ratio, 2) if ratio else None})
    if ratio is not None and ratio < CUT_RATIO:
        return {**out, "kind": "cut_off"}
    if ratio is not None and ratio > LONG_RATIO:
        return {**out, "kind": "too_long"}
    return {**out, "kind": "ok"}
