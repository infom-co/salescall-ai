"""依頼者が受けられる日時の空き（accept_free。業務ごと。持ち主 CL-2。表の作り 0264）。

✅ 2026-11-20 ユーザー（計画書 9-4「担当者が不在」の行・伺い60）「1.空スケジュールの中から2つ　2.別の空スケジュールの2つ」：アポと依頼者の日時の質問の2択は、
依頼者が受けられる日時の空きから作る（会話の側＝CL-1 の連絡帳 4-212 の accept_free）。空きは毎日変わる運用の値なので、版を持つ業務設定に入れない
（入れると空きを直すたびに版と承認が増える）。形の確かめはここ（`check`）の1か所。書くのは管理者か契約者だけ（データベースが守る）。会話へは
core/conversation_io.call_dates が、その通話をかけた時刻より後に始まる空きだけを渡す。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

from salescall.core.access import Refused, Tx

_HHMM = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00")


class FreeSlotsInvalid(Refused):
    """空きの形が違う（理由は日本語）。"""


def check(slots: Any) -> tuple[tuple[str, str, str], ...]:
    """空きの形を確かめて読む（1か所）：[["YYYY-MM-DD","HH:MM","HH:MM"], …]。始まりは終わりより前・同じ日の空きは重ならない。並びは日・始まりの順にそろえる。"""
    if not isinstance(slots, list):
        raise FreeSlotsInvalid("空きは、[日, 始まり, 終わり] の並びです")
    out = []
    for i, s in enumerate(slots):
        where = f"{i + 1}つ目の空き"
        if not isinstance(s, list) or len(s) != 3 or not all(isinstance(x, str) for x in s):
            raise FreeSlotsInvalid(f"{where}は、[日（YYYY-MM-DD）, 始まり（HH:MM）, 終わり（HH:MM）] です")
        day, start, end = s
        try:
            if len(day) != 10:
                raise ValueError
            date.fromisoformat(day)
        except ValueError:
            raise FreeSlotsInvalid(f"{where}の日（{day}）は、YYYY-MM-DD の形の、ある日です") from None
        if not (_HHMM.fullmatch(start) and _HHMM.fullmatch(end)) or start == "24:00" or not start < end:
            raise FreeSlotsInvalid(f"{where}の時刻（{start}〜{end}）は、HH:MM の形で、始まりが終わりより前です")
        out.append((day, start, end))
    out.sort()
    for a, b in zip(out, out[1:]):
        if a[0] == b[0] and b[1] < a[2]:
            raise FreeSlotsInvalid(f"{a[0]} の空き（{a[1]}〜{a[2]} と {b[1]}〜{b[2]}）が重なっています")
    return tuple(out)


def put(tx: Tx, business_id: UUID, slots: Any) -> tuple[tuple[str, str, str], ...]:
    """その業務の空きを、まるごと置き換える（記録を1つ足す。前の記録は残る）。書いた方はデータベースの1か所（salescall.cur_actor）。"""
    checked = check(slots)
    tx.execute("INSERT INTO salescall.business_free_slots (tenant_id, client_id, business_id, slots, set_by)"
               " SELECT b.tenant_id, b.client_id, b.business_id, %s, salescall.cur_actor() FROM salescall.businesses b WHERE b.business_id = %s",
               (json.dumps([list(s) for s in checked]), business_id))
    return checked


def current(tx: Tx, business_id: UUID, *, after: datetime | None = None) -> list[list[str]]:
    """その業務のいまの空き（いちばん新しい記録。無ければ空）。after＝その日本時間（時差を持たない日時）より後に始まる空きだけ（会話へ渡すとき＝その通話を
    かけた時刻。過ぎた空きを2択に並べない）。"""
    row = tx.one("SELECT slots FROM salescall.business_free_slots WHERE business_id = %s ORDER BY seq DESC LIMIT 1", (business_id,))
    slots = check(row["slots"]) if row is not None else ()
    cut = None if after is None else (after.date().isoformat(), f"{after:%H:%M}")
    return [list(s) for s in slots if cut is None or (s[0], s[1]) > cut]
