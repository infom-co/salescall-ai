"""知らせの台帳へ出来事を入れる（Q23。2026-10-14 ユーザー承認）。持ち主 CL-2（入れる側とデータの形）。送るのは CL-3（output/）。

- 出来事を台帳に入れるのは、この部品の `emit` だけ（I-12。app/checks が見張る）。停止・品質の段・締め・暦の取り込みなどは、
  出来事を入れるだけで、自分で送らない（同じ知らせが二重に届く・漏れるのを防ぐ）。
- 同じ出来事は1回だけ（出来事の鍵 `key`。同じ鍵で2回呼んでも1つ）。鍵は出来事そのものを指す名前にする（例 "stop:123"）。
- 契約者・依頼主は取引のものをそのまま使う（全体の取引なら全体の出来事）。
- 要約に、相手の電話番号・名前・発話の中身を入れない（画面と知らせに出す。中身は refs の ID で指す。CL-3 の 4-1e の5）。
  要約は、この部品を呼ぶ処理が、決まった言葉と ID・数から作る（会話の文字をそのまま入れない）。
"""

from __future__ import annotations

import json
from typing import Any

from salescall.core.access import Tx

# 出来事の種類（名前 → 日本語）。一覧の正本はここだけ（画面・送る処理は、これを読む）。
KINDS: dict[str, str] = {
    "stopped": "発信を止めた",
    "resumed": "止めていた発信を解いた（自動で・人の判断で）",
    "quality_stage": "品質の段が進んだ",
    "judge_unknown": "判定できない通話があった（判定の処理か入力の誤り）",
    "error_notice": "知らせるべきエラーがあった（9章の表）",
    "calendar_sync_failed": "祝日の暦を取りに行けなかった",
    "budget": "費用の上限に達した・報酬に対する費用の割合が上限を超えた",
    "forbidden_speech": "言ってはいけないことを言った通話があった（その行はひとまず終えた）",
}


class UnknownKind(Exception):
    """一覧に無い種類。"""


def emit(tx: Tx, kind: str, *, key: str, summary: str, refs: dict[str, Any] | None = None) -> bool:
    """出来事を台帳に入れる（取引の契約者・依頼主の出来事）。新しく入れたら True、同じ鍵がもうあれば False。"""
    if kind not in KINDS:
        raise UnknownKind(kind)
    row = tx.one(
        "INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s)"
        " ON CONFLICT (event_key) DO NOTHING RETURNING event_id",
        (kind, key, summary, json.dumps(refs or {}, ensure_ascii=False, default=str)))
    return row is not None
