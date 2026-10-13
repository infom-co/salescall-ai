"""AIの1回の発話（1ターン）を見分ける唯一の場所（DESIGN.md 6-7 の「AIが黙ったとき・途中で止まったとき」の試作）。

分け方（どれか1つ）：
- "silent"     … 声が出ていない（声のある長さが MIN_VOICED_S 未満）。または、決めた秒数のうちにターンが終わらなかった
- "wrong_language" … 業務の言葉（日本語）でない言葉を話した（2026-10-13 G-17：AIが考えていることを英語で口に出した
                   「To read this script, I'll project a polite ...」。長さはたまたま見合っていて、長さの見分けでは拾えなかった）
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

from audio import voiced_seconds
from jtext import morae

# ローマ字の単語が続く所（業務の言葉が日本語のとき、これが出たら日本語でない言葉を話したとみなす）
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’]*(?:[\s,.]+[A-Za-z][A-Za-z'’]*){3,}")

MIN_VOICED_S = 0.2
# 声ごとの、ふつうの話す速さ（1秒あたりの音の数の真ん中の値）。G-17 の1文ずつの形、途中で止まった回を除く
# （Sadachbia 216ターン・Leda 226ターン、2026-10-13）。声の型ごとに測り直して設定の値で持つ（フェーズ1）。
NORMAL_MORAE_PER_S = {"Sadachbia": 9.0, "Leda": 8.3}
# 声の長さ ÷ 見込みの長さ が、これより小さければ途中で止まった・大きければ長すぎ。
# G-17：ふつうの発話は Sadachbia で見込みの 0.78倍まで、Leda で 0.81倍まで縮んだ。止まった発話は 0.75倍以下（Sadachbia）・0.72倍以下（Leda）。
#   ⇒ 余裕が小さい（0.03〜0.06）。線の引き方は、フェーズ1の通話の記録で、見逃しと誤りの数を数えてから決める。
CUT_RATIO = 0.77
LONG_RATIO = 1.8


def check_ai_turn(*, audio: bytes, rate: int, voice: str, timed_out: bool = False,
                  expected_text: str | None = None, intended_text: str | None = None) -> dict:
    voiced = voiced_seconds(audio, rate) if audio else 0.0
    return classify(voiced_s=voiced, voice=voice, timed_out=timed_out, expected_text=expected_text, intended_text=intended_text)


def classify(*, voiced_s: float, voice: str, timed_out: bool = False,
             expected_text: str | None = None, intended_text: str | None = None) -> dict:
    """声のある長さから分ける（記録の見直しでも、同じこの処理を使う）。"""
    voiced = voiced_s
    text = expected_text or intended_text or ""
    out = {"voiced_s": round(voiced, 2), "compared_with": "expected" if expected_text else ("intended" if intended_text else None)}
    if timed_out or voiced < MIN_VOICED_S:
        return {**out, "kind": "silent", "timed_out": timed_out}
    if intended_text and _LATIN_RUN.search(intended_text):
        return {**out, "kind": "wrong_language", "latin": _LATIN_RUN.search(intended_text).group(0)[:80]}
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
