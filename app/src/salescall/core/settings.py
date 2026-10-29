"""設定の定義と値（DESIGN.md 8章）。持ち主 CL-2。

- 値の正本は、データベースの設定の表だけ（表の作り 0005）。処理の中に数を書かない（同じ数を2か所に持たない）。
- 値が無ければ `SettingMissing`（既定値で埋めない。原則10）。門なら「判断できない」で通さない。
- 値は追記だけ。値ごとに「仮か確定か」「根拠」「いつ見直すか」を持つ。仮の値は `provisional` で一覧にできる
  （ダッシュボードに出して、見直しを忘れない）。
- 設定の値を足すのは、この部品だけ（I-12。app/checks が見張る）。足した取引の中で、その範囲の予約を取り消す（5-2-1 の2）。
- 階層（システム共通 → 契約者 → 依頼者 → 業務）を合成するのは `resolve` の1か所だけ（Q10）。合成の種類は、設定の定義が持つ。
  2026-10-13 ユーザー：「川下の方が強くないと、依頼主ごとの意味がない」⇒ 依頼者ごとの設定は「上書き」（業務 ＞ 依頼者 ＞
  契約者 ＞ システム）。システムの値は初めの値。
  設定ごとに、いちばん細かく決められる範囲（finest_scope：システム／契約者／依頼者／業務）を定義が持つ（2026-10-17 ユーザー決定。
  0045）。それより細かい範囲の値は、データベースが入れさせない。「システムだけ」は、いちばん細かく決められるのがシステムのもの。
  ただし「超えられない枠」（Intent.md 5-4 の緩められないもの・取り消せない害を防ぐもの）を持つ設定は、上書きの結果を
  その枠の内側に収める（枠は、収める設定より細かく決められない別の設定。同じ範囲で読む。`resolve_detail` で、枠で収めたかが分かる）。
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from salescall.core import calling_hours
from salescall.core.access import Tx

_SCOPE_NAME = {"system": "システム", "tenant": "契約者", "client": "依頼者", "business": "業務"}


class SettingMissing(Exception):
    """その設定の値が無い（決まっていない）。"""


def system_value(tx: Tx, key: str) -> Any:
    """システムだけが決める設定の、いまの値。契約者・依頼者・業務ごとに変えられる設定は、範囲を渡して `resolve` で読む
    （システムの値だけを読むと、契約者・依頼者の値を黙って読み落とす）。"""
    d = tx.one("SELECT finest_scope FROM salescall.setting_definitions WHERE key = %s", (key,))
    if d is None:
        raise SettingMissing(key)
    if d["finest_scope"] != "system":
        raise ValueError(f"設定 {key} は{_SCOPE_NAME[d['finest_scope']]}ごとに変えられます。範囲を渡して resolve で読んでください")
    return initial_value(tx, key)


def initial_value(tx: Tx, key: str) -> Any:
    """システムの値（契約者・依頼者・業務の値が無いときに使う、初めの値）。画面で「初めの値」を見せるときなどに使う。
    処理の中の判断には使わない（契約者・依頼者の値を読み落とす。判断は `resolve`、システムだけの設定は `system_value`）。"""
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
    return set_value(tx, key, value, scope="system", status=status, rationale=rationale, review_when=review_when)


def set_value(tx: Tx, key: str, value: Any, *, scope: Literal["system", "tenant", "client", "business"],
              status: Literal["provisional", "decided"], rationale: str, review_when: str,
              tenant_id: UUID | None = None, client_id: UUID | None = None, business_id: UUID | None = None) -> int:
    """その範囲の値を足す（契約者が依頼者タブで決めた値など）。前の値は記録に残る。形が違えば、データベースが拒む。

    **同じ取引の中で、その範囲のまだ発信していない予約を取り消す**（門の判定に効く変更。DESIGN.md 5-2-1 の2）。
    どの設定が門に効くかを一覧にすると漏れるので、設定を変えたら必ず取り消す（予約は次の回にまた取れる）。
    """
    row = tx.one(
        "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
        "                                      review_when, set_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor()) RETURNING value_id",
        (key, scope, tenant_id, client_id, business_id, json.dumps(value), status, rationale, review_when),
    )
    tx.one("SELECT salescall.release_claims_in_scope(%s, %s, %s, %s)", (scope, tenant_id, client_id, business_id))
    if key == "line.test_number" and scope == "tenant" and value is not None:
        # テスト番号が入った（E14）を状態の欄へ（DESIGN.md 17章の3段目＝影。記録だけ）。状態の欄の部品が設定の部品を読むので、ここで読む
        from salescall.core import health
        health.number_set(tx)
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
            raise SettingMissing(key)  # 厳しいほう・足し合わせは、システムの値が必ず要る（8章）
        values = list(by_scope.values())
        origin = "merged"
        if kind == "min":
            value = min(values)
        elif kind == "max":
            value = max(values)
        elif kind == "intersect":
            value = calling_hours.intersect(values)
        elif kind == "union":
            value = sorted(set().union(*[set(v) for v in values]))
        else:
            raise ValueError(f"合成の種類が分かりません：{kind}")
    bounded_by = None
    if d["bound_key"] is not None:
        # 枠は、収める設定と同じ範囲で読む（依頼者ごとの歯止めは、その依頼者の値）。値が無ければ SettingMissing（決まっていない
        # ⇒ 使う側は通さない）
        bound = resolve(tx, d["bound_key"], tenant_id=tenant_id, client_id=client_id, business_id=business_id)
        side = d["bound_side"]
        limited = (min(value, bound) if side == "upper" else max(value, bound) if side == "lower"
                   else calling_hours.intersect([value, bound]))
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
