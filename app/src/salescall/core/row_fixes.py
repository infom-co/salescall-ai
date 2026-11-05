"""お客様リストの行の直し（ダッシュボード。データベースは 0178。DESIGN.md 3-4-1）。持ち主 CL-2。

✅ 2026-11-05 ユーザー：「それらはダッシュボードに取込、ダッシュボードでも進捗確認や修正ができるようにすること。」

- 直せる欄は `FIELDS`（会社名・お呼びする方の名前・フルネーム・役職・部署）。直しは記録を足していくだけで、取込の値は変えない。
  行のいまの値は見方 `salescall.tasks_now` の1か所（会話が読む business_settings.target_of・門・材料の数えも、この見方を読む）。
- 名前の欄の読みは、取込と同じ決め方（core/readings.py の1か所）：直した方が入れた読み（確かめた読み＝person）＞ AI の読み ＞
  決まらない（候補つき。会話の中で確かめる）。検証モードの業務では、AI の読みは契約者が確かめるので、読みも一緒に入れていただく
  （Intent.md 6-8 の決まり。入れなければ `FixInvalid`）。
- 名前を直したら、直す前に会話の中でお客様に確かめた読みは、その名前には効かない（tasks_now の *_fixed_at より後に確かめた読みだけ＝
  business_settings.target_of の1か所）。
- 直せるのは管理者か契約者だけ（データベースが守る）。電話番号・連絡先の直しは core/customer_contacts.py。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import approval, readings
from salescall.core.access import Tx

FIELDS = ("source_name", "target_name", "target_full_name", "target_role", "target_department")   # 0178 の CHECK と同じ
NAME_FIELDS = ("source_name", "target_name", "target_full_name")                                  # 読みを持つ欄
FIELD_NAMES = {"source_name": "会社名", "target_name": "お呼びする方の名前", "target_full_name": "お呼びする方のフルネーム",
               "target_role": "お呼びする方の役職", "target_department": "お呼びする方の部署"}


class FixInvalid(ValueError):
    """その直しは受けられない（理由は日本語）。何も書かない。"""


def fix(tx: Tx, task_id: UUID, field: str, value: str | None, *, reading: str | None = None,
        suggest: readings.Suggester | None = None) -> dict[str, Any]:
    """行の1つの欄を直す（書くのはここだけ）。value が空なら、その欄を消す。直したあとの、その欄のいまの値を返す。"""
    if field not in FIELDS:
        raise FixInvalid(f"直せる欄は {[FIELD_NAMES[f] for f in FIELDS]} です（{field!r}）")
    text = value.strip() if isinstance(value, str) and value.strip() else None
    r = readings.UNDECIDED
    if field in NAME_FIELDS and text is not None:
        given = reading.strip() if isinstance(reading, str) and reading.strip() else None
        if given is not None:
            if readings.normalize(given) is None:
                raise FixInvalid(f"{FIELD_NAMES[field]}の読み「{given}」は、ひらがなかカタカナで入れてください")
            r = readings.resolve(text, None, confirmed={text: given}, ai={})
        else:
            business = tx.one("SELECT r.business_id FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id"
                              " WHERE t.task_id = %s", (task_id,))
            if business is None:
                raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
            if approval.human_for_business(tx, business["business_id"]):
                raise FixInvalid(f"検証モードの業務では、{FIELD_NAMES[field]}の読みも入れてください（AI の読みは契約者が確かめる決まり）")
            r = readings.resolve(text, None, confirmed={}, ai=readings.ask([text], suggest))
    elif reading is not None and str(reading).strip():
        raise FixInvalid(f"読みを持つのは名前の欄だけです（{FIELD_NAMES[field]}）")
    tx.execute(
        "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, reading, reading_by, candidates, likely,"
        "                                  recorded_by)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, %s, %s, %s, %s, salescall.cur_actor()"
        " FROM salescall.tasks t WHERE t.task_id = %s",
        (field, text, r.reading, r.by if r.reading else None, list(r.candidates), r.likely, task_id))
    row = tx.one(f"SELECT {field} AS value FROM salescall.tasks_now WHERE task_id = %s", (task_id,))
    if row is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    return {"field": field, "value": row["value"], "reading": r.reading if text is not None else None}
