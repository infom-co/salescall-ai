"""設定の定義と値（DESIGN.md 8章）。持ち主 CL-2。

- 値の正本は、データベースの設定の表だけ（表の作り 0005）。処理の中に数を書かない（同じ数を2か所に持たない）。
- 値が無ければ `SettingMissing`（既定値で埋めない。原則10）。門なら「判断できない」で通さない。
- 値は追記だけ。値ごとに「仮か確定か」「根拠」「いつ見直すか」を持つ。仮の値は `provisional` で一覧にできる
  （ダッシュボードに出して、見直しを忘れない）。
- 設定の値を足すのは、この部品だけ（I-12。app/checks が見張る）。
- 階層（システム共通 → 契約者 → 依頼主 → 業務）を合成するのは `resolve` の1か所だけ（Q10）。合成の種類は、設定の定義が持つ。
  2026-10-13 ユーザー：「川下の方が強くないと、依頼主ごとの意味がない」⇒ 依頼主ごとの設定は「上書き」（業務 ＞ 依頼主 ＞
  契約者 ＞ システム）。システムの値は初めの値。
  ただし「超えられない枠」（Intent.md 5-4 の緩められないもの・取り消せない害を防ぐもの）を持つ設定は、上書きの結果を
  その枠の内側に収める（枠はシステムだけの別の設定。`resolve_detail` で、枠で収めたかが分かる）。
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from salescall.core.access import Tx

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")


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


def resolve(tx: Tx, key: str, *, tenant_id: UUID | None, client_id: UUID | None,
            business_id: UUID | None) -> Any:
    """その範囲で、結局いくつか（Q10。DESIGN.md 8章）。値が決まらなければ SettingMissing。"""
    return resolve_detail(tx, key, tenant_id=tenant_id, client_id=client_id, business_id=business_id)["value"]


def resolve_detail(tx: Tx, key: str, *, tenant_id: UUID | None, client_id: UUID | None,
                   business_id: UUID | None) -> dict[str, Any]:
    """合成した値と、どの範囲の値か（from）・枠で収めたか（bounded_by）。画面で「枠で止まった」を見せるのに使う。"""
    d = tx.one("SELECT merge_kind, value_type, bound_key, bound_side FROM salescall.setting_definitions WHERE key = %s",
               (key,))
    if d is None:
        raise SettingMissing(key)
    by_scope = _layers(tx, key, tenant_id, client_id, business_id)
    kind = d["merge_kind"]
    if kind == "override":
        scope = next((sc for sc in ("business", "client", "tenant", "system") if sc in by_scope), None)
        if scope is None:
            raise SettingMissing(key)
        value, origin = by_scope[scope], scope
    else:
        if "system" not in by_scope:
            raise SettingMissing(key)  # 厳しいほう・足し合わせ・システムだけは、システムの値が必ず要る（8章）
        values = list(by_scope.values())
        origin = "merged"
        if kind == "system_only":
            value, origin = by_scope["system"], "system"
        elif kind == "min":
            value = min(values)
        elif kind == "max":
            value = max(values)
        elif kind == "intersect":
            value = _intersect_hours(values)
        elif kind == "union":
            value = sorted(set().union(*[set(v) for v in values]))
        else:
            raise ValueError(f"合成の種類が分かりません：{kind}")
    bounded_by = None
    if d["bound_key"] is not None:
        bound = system_value(tx, d["bound_key"])  # 枠に値が無ければ SettingMissing（決まっていない ⇒ 使う側は通さない）
        side = d["bound_side"]
        limited = (min(value, bound) if side == "upper" else max(value, bound) if side == "lower"
                   else _intersect_hours([value, bound]))
        if limited != value:
            bounded_by = d["bound_key"]
        value = limited
    return {"value": value, "from": origin, "bounded_by": bounded_by}


def _layers(tx: Tx, key: str, tenant_id: UUID | None, client_id: UUID | None,
            business_id: UUID | None) -> dict[str, Any]:
    rows = tx.all(
        "SELECT DISTINCT ON (scope) scope, value FROM salescall.setting_values"
        " WHERE key = %s AND (scope = 'system'"
        "   OR (scope = 'tenant' AND tenant_id = %s)"
        "   OR (scope = 'client' AND tenant_id = %s AND client_id = %s)"
        "   OR (scope = 'business' AND tenant_id = %s AND client_id = %s AND business_id = %s))"
        " ORDER BY scope, value_id DESC",
        (key, tenant_id, tenant_id, client_id, tenant_id, client_id, business_id),
    )
    return {r["scope"]: r["value"] for r in rows}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _intersect_hours(values: list[dict[str, list[list[str]]]]) -> dict[str, list[list[str]]]:
    """曜日ごとの時間帯の重なり。どこかの階層でかけない曜日は、かけない。"""
    out: dict[str, list[list[str]]] = {}
    for day in _DAYS:
        ranges = [(_minutes(a), _minutes(b)) for a, b in values[0][day]]
        for v in values[1:]:
            other = [(_minutes(a), _minutes(b)) for a, b in v[day]]
            ranges = [(max(a1, a2), min(b1, b2)) for a1, b1 in ranges for a2, b2 in other if max(a1, a2) < min(b1, b2)]
        out[day] = [[_hhmm(a), _hhmm(b)] for a, b in sorted(ranges)]
    return out
