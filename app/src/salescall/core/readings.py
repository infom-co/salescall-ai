"""名前の読みの決め方（Intent.md 6-8・DESIGN.md 3-7・3-4）。持ち主 CL-2。

業務設定の名前（名乗り・トークスクリプトの名前）も、発信対象の一覧の名前（お客様の会社名・目的のお客様の名前）も、
**読みは、この1か所の決め方で決める**（2つ目を作らない）：

- 入口（ファイル・依頼者の画面など）が持ってきた読み ＞ 契約者が確かめた（直した）AI の読み ＞ AI が決めた読み、の順で使い、
  どれで決めたかを残す（`by`：given／person／ai）。発信対象の名前は、このほかに、会話の中でお客様に確かめた読み
  （conversation）があれば、それがいちばん強い（`with_confirmed`。CL-1 の連絡帳 4-15）。
- **読みが決まらない名前でも止めない**（2026-10-16 ユーザー：「なかじまとなかしまなどは人が見ても判断できません。それは会話の中で
  失礼のないように語るか、場合によっては聞き出す」。CL-1 の連絡帳 4-15）。読みは空のまま、考えられる読み（`candidates`）と
  まず言ってみる読み（`likely`）を持って登録し、会話の中で確かめる（CL-1 の `flowctl.reading_question`）。
- **契約者が確かめるのは、検証モードの業務の、AI が決めた読みだけ**（2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」
  「開発時、検証時、クレームになりそうなど、特例時のみ人が対応」）。入口の読み・フルオートの AI の読みは、そのまま使う。読みが
  決まらない名前は、契約者にも決められない（上のユーザーの言葉）ので、確かめの対象にしない（会話で確かめる）。
  確かめは表記ごとなので、契約者の読みは入口の読みの無い名前にだけ効く（同じ漢字でも行ごとに読みが違いうる「東海林」などを、
  入口の読みを上書きしてそろえてしまわないため）。
- **読みは、ひらがなで持つ**（`normalize`。カタカナはひらがなに直す。CL-1 の連絡帳 4-15：会話の側は、決まった読みを、声に出す
  文の中でひらがなに置き換える＝G-44）。ひらがな・長音・中黒のほかの文字（漢字・ローマ字）は、読みとして使わない。
- **AI に聞くのは1回だけ**（`ask`）：プレビューで聞いた答えを、登録でもそのまま使う。AI は同じ問いに毎回同じ答えを返すとは
  限らないので、登録でもう一度聞くと、契約者が見たプレビューと違う中身になり、登録できなくなるため（2026-10-16 に見つけた穴）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

BY = ("given", "ai", "person", "conversation", "recording")   # 入口が持ってきた／AI が決めた／契約者が確かめた／会話でお客様に確かめた／
#   契約者が録音を聞いて選んだ（読みの候補ごとの録音から。Intent.md 6-2・CL-1 の連絡帳 4-16）
_HIRA = re.compile(r"^[ぁ-ゖー・]+$")

Suggester = Callable[[list[str]], dict[str, dict[str, Any]]]
"""名前の読みを出す処理（表記の並び → 表記ごとの {"reading": 決まった読み（決まらなければ空）, "candidates": 考えられる読み,
"likely": まず言ってみる読み}）。AI（文字のモデル）で出すのは会話の担当 CL-1（Gemini のキーを使うのは CL-1 だけ。決め方は
CL-1 の `names.decide`：2つのモデルの読みが同じときだけ決まる）。混雑・上限で出せない表記は入れない（止まらずに、出せた分だけ返す。
入れなかった表記は「読みが決まらない」になる）。"""


@dataclass(frozen=True)
class Reading:
    reading: str | None                             # 決まった読み（ひらがな）。None＝読みが決まらない
    by: str | None                                  # BY のどれか（読みが無ければ None）
    candidates: tuple[str, ...] = ()                # 読みが決まらないときの、考えられる読み（ひらがな）
    likely: str | None = None                       # 読みが決まらないときの、まず言ってみる読み（ひらがな）

    def as_json(self) -> dict[str, Any]:
        return {"reading": self.reading, "reading_by": self.by, "candidates": list(self.candidates), "likely": self.likely}


UNDECIDED = Reading(None, None)


def normalize(reading: Any) -> str | None:
    """読みを、持つ形（ひらがな・空白なし）にする。読みとして使えなければ None（ひらがな・カタカナ・長音・中黒だけ）。"""
    if not isinstance(reading, str):
        return None
    s = unicodedata.normalize("NFKC", reading)
    s = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s if not c.isspace())
    return s if _HIRA.match(s) else None


def required(reading: Any, label: str) -> str:
    """読みとして使える形にした読み（1か所）。使えなければ ValueError（label＝どの読みか。理由の頭に付ける）。"""
    r = normalize(reading)
    if r is None:
        raise ValueError(f"{label}：読みは、ひらがなかカタカナで書きます")
    return r


def _from_ai(got: Any) -> Reading:
    """AI の答え1つを、読みにする（読みとして使えない文字は捨てる。決まった読みが使えなければ、決まらない側に倒す）。"""
    if not isinstance(got, Mapping):
        return UNDECIDED
    reading = normalize(got.get("reading")) if got.get("reading") else None
    if reading is not None:
        return Reading(reading, "ai")
    raw = got.get("candidates")
    cands = tuple(dict.fromkeys(c for c in (normalize(x) for x in (raw if isinstance(raw, (list, tuple)) else ())) if c))
    likely = normalize(got.get("likely")) if got.get("likely") else None
    return Reading(None, None, cands, likely or (cands[0] if cands else None))


def ask(texts: Iterable[str], suggest: Suggester | None) -> dict[str, Reading]:
    """読みの無い表記について、AI に1回だけ聞く（同じ表記は1回）。表記ごとの読み（決まった読みか、決まらない読みの候補）。"""
    wanted = sorted({t for t in texts if t})
    if not wanted or suggest is None:
        return {}
    got = suggest(wanted)
    return {t: _from_ai(got[t]) for t in wanted if isinstance(got, Mapping) and t in got}


def to_json(ai: Mapping[str, Reading]) -> dict[str, dict[str, Any]]:
    """AI の答えを、プレビューから登録へ渡す形にする（`from_json` で戻す）。"""
    return {t: r.as_json() for t, r in ai.items()}


def from_json(raw: Any) -> dict[str, Reading]:
    if not isinstance(raw, Mapping):
        return {}
    return {t: (Reading(normalize(x["reading"]), "given") if isinstance(x, Mapping) and x.get("reading_by") == "given"
                and normalize(x.get("reading")) else _from_ai(x)) for t, x in raw.items()}


NameFinder = Callable[[str], list[dict[str, Any]]]
"""台本（業務設定の文をまとめたもの）から、名前を洗い出して読みを決める処理（CL-1 の `names.decide` の戻り値の形：
[{"surface", "reading"（空＝決まらない）, "source"（"台本の読みがな"・"モデル2つ"・""）, "candidates", "likely"}]）。
作るのは CL-1（Gemini のキーを使うのは CL-1 だけ）。"""
_GIVEN_IN_SCRIPT = ("台本の読みがな", "given")      # 台本に書いてある読みがな（「東海林（しょうじ）」）＝入口が持ってきた読み


def find(text: str, finder: NameFinder | None) -> dict[str, Reading]:
    """台本から名前と読みを1回だけ出させる（表記 → 読み）。台本の読みがなは入口の読み（given）、2つのモデルが合った読みは
    AI が決めた読み（ai）、それ以外は読みが決まらない（候補つき）。"""
    if finder is None or not text.strip():
        return {}
    out: dict[str, Reading] = {}
    for x in finder(text) or []:
        surface = x.get("surface") if isinstance(x, Mapping) else None
        if not isinstance(surface, str) or not surface.strip() or surface.strip() in out:
            continue
        r = _from_ai(x)
        if r.reading is not None and x.get("source") in _GIVEN_IN_SCRIPT:
            r = Reading(r.reading, "given")
        out[surface.strip()] = r
    return out


def resolve(text: str, given: Any, *, confirmed: Mapping[str, str], ai: Mapping[str, Reading]) -> Reading:
    """その名前の読み（決め方はここだけ）。入口の読み ＞ 契約者が確かめた読み ＞ AI が決めた読み ＞ 決まらない（AI の候補つき）。
    入口の読みが読みとして使えないときは ValueError（入口のデータの誤り。呼ぶ側が、その行・その名前の問題として出す）。"""
    if isinstance(given, str) and given.strip():
        return Reading(required(given, f"名前「{text}」の読み「{given.strip()}」"), "given")
    if confirmed.get(text) and normalize(confirmed[text]):
        return Reading(normalize(confirmed[text]), "person")
    return ai.get(text, UNDECIDED)


def with_recording(r: Reading, chosen: str | None) -> Reading:
    """契約者が録音を聞いて選んだ読みがあれば、入口の読みのほかより強い（決め方の順：入口の読み ＞ 録音で選んだ読み ＞ 契約者が確かめた
    読み ＞ AI が決めた読み ＞ 決まらない。CL-2 の連絡帳 4-66）。入口の読みと違う読みを選んだ録音は、作るときに断る（recordings）。"""
    return r if chosen is None or r.by == "given" else Reading(chosen, "recording")


def with_confirmed(r: Reading, said: str | None) -> Reading:
    """会話の中でお客様に確かめた読みがあれば、それがいちばん強い（発信対象の名前。次にかけるときは、決まった読みとして使う）。"""
    s = normalize(said) if said else None
    return Reading(s, "conversation") if s is not None else r


def needs_person(r: Reading, *, verification: bool) -> bool:
    """契約者の確かめを待つ読みか（検証モードの、AI が決めた読みだけ。決まらない読みは会話で確かめる）。"""
    return verification and r.by == "ai"
