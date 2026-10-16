"""名前の読みの決め方（Intent.md 6-8・DESIGN.md 3-7・3-4）。持ち主 CL-2。

業務設定の名前（名乗り・トークスクリプトの名前）も、発信対象の一覧の名前（お客様の会社名・目的のお客様の名前）も、
**読みは、この1か所の決め方で決める**（2つ目を作らない）：

- 入口（ファイル・依頼者の画面など）が持ってきた読み ＞ 契約者が確かめた（直した）AI の案 ＞ AI の読みの案、の順で使い、
  どれで決めたかを残す（`by`：given／person／ai）。**契約者が確かめるのは AI の案だけ**なので、契約者の読みは、入口の読みの
  無い名前にだけ効く（確かめは表記ごと＝同じ漢字でも行ごとに読みが違いうる「東海林」などを、入口の読みを上書きして
  そろえてしまわないため。2026-10-16 に見つけた穴）。
- **契約者が確かめるのは、検証モードの業務の AI の案だけ**（2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」。
  検証モードかは承認の部分＝core/approval.py が答える）。入口が持ってきた読みと、フルオートの AI の案は、そのまま使う。
- 読みは、ひらがな・カタカナ（長音・中黒・空白を含む）だけ（AI が漢字やローマ字を返したら、読みとして使わない）。
- **AI に聞くのは1回だけ**（`ask`）：プレビューで聞いた答えを、登録でもそのまま使う。AI は同じ問いに毎回同じ答えを返すとは
  限らないので、登録でもう一度聞くと、契約者が見たプレビューと違う中身になり、登録できなくなるため（2026-10-16 に見つけた穴）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

BY = ("given", "ai", "person")                      # 入口が持ってきた／AI の読みの案／契約者が確かめた（直した）
_KANA = re.compile(r"^[ぁ-ゖァ-ヺー・ 　]+$")

Suggester = Callable[[list[str]], dict[str, str]]
"""名前の読みの案を出す処理（表記の並び → 表記ごとの読み）。AI（文字のモデル）で出すのは会話の担当 CL-1（Gemini のキーを
使うのは CL-1 だけ）。出せない表記は入れない（混雑・上限のときも、止まらずに、出せた分だけ返す）。"""


@dataclass(frozen=True)
class Reading:
    reading: str | None
    by: str | None                                  # BY のどれか（読みが無ければ None）


def problem(reading: str) -> str | None:
    """読みとして使えるか（使えなければ理由）。"""
    return None if _KANA.match(reading) else "読みは、ひらがなかカタカナで書きます"


def ask(texts: Iterable[str], suggest: Suggester | None) -> dict[str, str]:
    """読みの無い表記について、AI の読みの案を1回だけ聞く（同じ表記は1回）。答えのうち、読みとして使えるものだけを返す。"""
    wanted = sorted({t for t in texts if t})
    if not wanted or suggest is None:
        return {}
    got = suggest(wanted)
    return {t: r.strip() for t, r in got.items()
            if t in wanted and isinstance(r, str) and r.strip() and problem(r.strip()) is None}


def resolve(text: str, given: str | None, *, confirmed: Mapping[str, str], ai: Mapping[str, str]) -> Reading:
    """その名前の読みと、決めたもの（決め方はここだけ）。"""
    if given and given.strip():
        return Reading(given.strip(), "given")
    if confirmed.get(text):
        return Reading(confirmed[text], "person")
    if ai.get(text):
        return Reading(ai[text], "ai")
    return Reading(None, None)


def needs_person(r: Reading, *, verification: bool) -> bool:
    """契約者の確かめを待つ読みか（検証モードの AI の案だけ）。"""
    return verification and r.by == "ai"
