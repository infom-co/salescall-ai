"""見守り役が呼ぶ、ほかの担当の部品の形（DESIGN.md 5-4）。持ち主 CL-2（形だけ。中身は各担当）。

どの部品も、設定に書いた `モジュール:名前`（部品を返す関数。回線の部品と通話を進める部品は回線の ID を受け取る）から作る（`load` の1か所）。回線の部品と通話を進める
部品は、回線ごとの処理（別の処理＝プロセス）の中で作る（1本の通話の処理が落ちても、ほかの回線を巻き込まない＝CL-1 の 2026-10-13 の (c)）。
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol
from uuid import UUID

from salescall.core.access import Access, Actor
from salescall.core.gate import Claim


class CallRunner(Protocol):
    """通話を最後まで進める部品（会話＝CL-1。電話接続の部品と組む）。発信の出口（dial_exit.dial）がかけたあとに呼ぶ。
    試行の状態を終わりまで移し、報告を置く（attempts.move・call_report.save。報告は版3＝通話の事実の形）。通話の途中で「もう
    かけないで」の申告を聞いたら、その場で台帳へ入れる（dnc.register_declared。原則14）。エラーは自分でエラーの入口
    （watch.after_error）へ渡す。外へ出た誤りは、見守り役が「見守り役の誤り」として記録し、その試行を結果不明にする。"""

    def run(self, access: Access, actor: Actor, claim: Claim, *, tenant_id: UUID, client_id: UUID) -> None: ...


class Verifier(Protocol):
    """通話後の検証の部品（CL-1。Q21）。待ちの通話を確かめ、attempts.record_verification・finish_without_recording を呼ぶ。"""

    def run_pending(self, access: Access, actor: Actor, *, tenant_id: UUID) -> None: ...


class Prober(Protocol):
    """停止を解く確かめの部品（会話AI＝CL-1・回線＝電話接続）。確かめた結果（うまくいったか・根拠の短い文）を返す。"""

    def probe(self, stop: dict[str, Any]) -> tuple[bool, str]: ...


def load(spec: str, *args: Any) -> Any:
    """設定の `モジュール:名前` から部品を作る（名前は部品を返す関数。回線の部品・通話を進める部品には回線の ID を渡す）。"""
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"部品は「モジュール:名前」で書きます（{spec!r}）")
    return getattr(importlib.import_module(module), name)(*args)
