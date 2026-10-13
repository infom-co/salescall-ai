"""設定の定義と値（DESIGN.md 8章）。持ち主 CL-2。

- 値の正本は、データベースの設定の表だけ（表の作り 0005）。処理の中に数を書かない（同じ数を2か所に持たない）。
- 値が無ければ `SettingMissing`（既定値で埋めない。原則10）。門なら「判断できない」で通さない。
- 値は追記だけ。値ごとに「仮か確定か」「根拠」「いつ見直すか」を持つ。仮の値は `provisional` で一覧にできる
  （ダッシュボードに出して、見直しを忘れない）。
- 設定の値を足すのは、この部品だけ（I-12。app/checks が見張る）。
- ⏳ 階層を合成する `resolve`（8章）は、引き継ぎの (9) で足す。いまはシステム共通の値の読み書きだけ。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from salescall.core.access import Tx


class SettingMissing(Exception):
    """その設定の値が無い（決まっていない）。"""


def system_value(tx: Tx, key: str) -> Any:
    """システム共通の、いまの値。"""
    row = tx.one(
        "SELECT value FROM salescall.setting_values WHERE key = %s AND scope = 'system'"
        " ORDER BY value_id DESC LIMIT 1",
        (key,),
    )
    if row is None:
        raise SettingMissing(key)
    return row["value"]


def provisional(tx: Tx) -> list[dict[str, Any]]:
    """システム共通の値のうち、いまの値が「仮」のもの（鍵・値・根拠・いつ見直すか）。"""
    return tx.all(
        "SELECT v.key, v.value, v.rationale, v.review_when, v.set_at, d.meaning"
        " FROM (SELECT DISTINCT ON (key) * FROM salescall.setting_values WHERE scope = 'system'"
        "       ORDER BY key, value_id DESC) v"
        " JOIN salescall.setting_definitions d USING (key)"
        " WHERE v.status = 'provisional' ORDER BY v.key"
    )


def set_system_value(tx: Tx, key: str, value: Any, *, status: Literal["provisional", "decided"], rationale: str,
                     review_when: str) -> int:
    """システム共通の値を足す（前の値は記録に残る）。形が違えば、データベースが拒む。"""
    row = tx.one(
        "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
        " VALUES (%s, 'system', %s, %s, %s, %s, salescall.cur_actor()) RETURNING value_id",
        (key, json.dumps(value), status, rationale, review_when),
    )
    return row["value_id"]
