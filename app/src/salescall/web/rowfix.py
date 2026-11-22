"""お客様の行の直し（docs/DASHBOARD.md 5-7。計画書 7-1「ダッシュボードで取り込み・進み具合の確かめ・直しができる」）。

直すのは core の入口だけ（名前などの欄＝`row_fixes.fix`・かける番号＝`row_fixes.fix_dial_number`）。画面は、直すときの選び
（いまの担当の方と同じ方か／前の担当の方に戻すか／前の担当の方との約束も守るか）を、**要るかどうかを core に聞いて、要るものだけを
順に1つずつ**出す（`next_question` の1か所）。要るかの決まりを画面で持たない（core の `same_person_choice_needed`・`back_choices`・
`old_promise_choice_needed`。直す入口も同じ1か所で確かめ、足りない・余る選びは断る）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from salescall.core import row_fixes
from salescall.core.access import Tx

DIAL = "dial_phone"                      # かける番号の直し（core の欄の名前ではない。fix_dial_number を呼ぶ）
FIELDS: tuple[str, ...] = (DIAL,) + row_fixes.FIELDS
NAME_FIELDS: tuple[str, ...] = row_fixes.NAME_FIELDS   # 読みを入れられる欄（core の決まりを読む）
NEW_PERSON = "new"                       # 「前の担当の方に戻す」の選びで、戻さない（新しい担当の方）


def field_name(name: str) -> str:
    """直す欄の名前（core の FIELD_NAMES を読む。かける番号だけ、ここ）。"""
    return "かける番号" if name == DIAL else row_fixes.FIELD_NAMES.get(name, name)


@dataclass(frozen=True)
class Draft:
    """直しの下書き（画面の欄から読む。選びは、答えたものだけ値を持つ）。"""
    field: str
    value: str | None
    reading: str | None = None
    same: bool | None = None
    back: str | None = None              # 前の担当の方に戻す先の期（戻さない＝None）
    back_answered: bool = False
    keep: bool | None = None
    clear: bool = False                  # 「この欄を消す」を選んだ（空のまま送っただけでは消さない＝設定の画面と同じ決まり）

    @property
    def is_dial(self) -> bool:
        return self.field == DIAL


def from_form(form: dict[str, Any]) -> Draft:
    def text(k: str) -> str | None:
        v = str(form.get(k) or "").strip()
        return v or None

    def yes_no(k: str) -> bool | None:
        return {"1": True, "0": False}.get(str(form.get(k) or ""))

    back = text("back")
    return Draft(field=str(form.get("field") or ""), value=text("value"), reading=text("reading"), same=yes_no("same"),
                 back=None if back in (None, NEW_PERSON) else back, back_answered=back is not None, keep=yes_no("keep"),
                 clear=str(form.get("clear") or "") == "1")


@dataclass(frozen=True)
class Question:
    name: str                             # 欄の名前（same／back／keep）
    title: str
    hint: str
    options: list[tuple[str, str]] = field(default_factory=list)


def _person(values: dict[str, Any]) -> str:
    name = values.get("target_full_name") or values.get("target_name") or "（お名前なし）"
    extra = "・".join(x for x in (values.get("target_department"), values.get("target_role")) if x)
    return name + (f"（{extra}）" if extra else "")   # 敬称は付けない（一覧の値に「様」が入っていることがある）


def next_question(tx: Tx, task_id: UUID, d: Draft) -> Question | None:
    """次に聞く選び（無ければ None＝直せる）。要るかは core に聞く（ここで決めない）。値の形が違えば core が `FixInvalid`（理由の文つき）。"""
    if d.field not in FIELDS:
        raise row_fixes.FixInvalid("直す欄を選んでください")
    if d.is_dial and d.value is None:
        raise row_fixes.FixInvalid("新しい番号を入れてください")
    # 消すのは「この欄を消す」を選んだときだけ（うっかり空で送って消さない。2026-11-20 の見直し：この画面は「空なら消す」、
    # 設定の画面は「消すを選ぶ」で、決まりが2つあった）。値と消すの両方は、どちらのつもりか分からないので断る
    if d.value is None and not d.clear:
        raise row_fixes.FixInvalid("新しい値を入れてください（欄を消すときは「この欄を消す」を選びます）")
    if d.value is not None and d.clear:
        raise row_fixes.FixInvalid("「この欄を消す」を選んだときは、新しい値を空にしてください")
    if d.same is None:
        needed = True if d.is_dial else row_fixes.same_person_choice_needed(tx, task_id, field=d.field, value=d.value)
        if needed:
            return Question("same", ("新しい番号でお話しする方は、いままでの担当の方と同じ方ですか" if d.is_dial
                                     else "直したあとのお名前の方は、いままでの担当の方と同じ方ですか"),
                            "誤字の直しや、同じ方の別の番号なら「同じ方」：約束も予定もそのままです。違う方なら、担当の方が替わります（約束は引き継がず、引き継ぎを確かめる電話からです）。",
                            [("1", "同じ方"), ("0", "違う方")])
    if d.same is False and not d.back_answered:
        choices = row_fixes.back_choices(tx, task_id)
        if choices:
            return Question("back", "前の担当の方に戻しますか", "前にお話ししていた方に戻すと、その方とのお約束やお答えも戻ります。",
                            [(NEW_PERSON, "戻さない（新しい担当の方）")] + [(c["back_to"], f"戻す：{_person(c['values'])}") for c in choices])
    if d.keep is None:
        if d.is_dial:
            needed = d.same is False and row_fixes.old_promise_choice_needed(tx, task_id, d.value, back_to=d.back)
        else:
            needed = row_fixes.old_promise_choice_needed(tx, task_id, field=d.field, value=d.value, same_person=d.same, back_to=d.back)
        if needed:
            return Question("keep", "前の担当の方とのかけ直しのお約束も守りますか",
                            "守るなら、お約束の日時に前の担当の方へおかけします。守らないなら、お約束をやめて、新しい担当の方へおかけします。",
                            [("1", "約束も守る"), ("0", "約束は守らない")])
    return None


def perform(tx: Tx, task_id: UUID, d: Draft) -> dict[str, Any]:
    """直す（core の入口だけ）。答えていない選びが残っていれば、何も書かずに断る（古い画面・書き換えた送信）。"""
    if next_question(tx, task_id, d) is not None:
        raise row_fixes.FixInvalid("選びが足りません。もう一度、直す画面から進めてください")
    if d.is_dial:
        return row_fixes.fix_dial_number(tx, task_id, d.value or "", same_person=bool(d.same), keep_old_promise=d.keep, back_to=d.back)
    return row_fixes.fix(tx, task_id, d.field, d.value, reading=d.reading, same_person=d.same, keep_old_promise=d.keep, back_to=d.back)


def summary(tx: Tx, task_id: UUID, d: Draft) -> list[tuple[str, str]]:
    """確かめの窓に出す「何をどう直すか」（答えた選びの言葉つき）。"""
    out = [("直す欄", field_name(d.field)), ("新しい値", d.value or "（消す）")]
    if d.reading:
        out.append(("読み", d.reading))
    if d.same is not None:
        out.append(("担当の方", "同じ方" if d.same else "違う方"))
    if d.back_answered:
        label = "戻さない（新しい担当の方）"
        for c in row_fixes.back_choices(tx, task_id) if d.back else []:
            if c["back_to"] == d.back:
                label = f"戻す：{_person(c['values'])}"
        out.append(("前の担当の方", label))
    if d.keep is not None:
        out.append(("前の担当の方との約束", "守る" if d.keep else "守らない"))
    return out
