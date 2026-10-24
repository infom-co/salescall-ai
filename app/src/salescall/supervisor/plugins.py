"""中央の見守り役（サーバー）が呼ぶ、ほかの担当の部品の形（DESIGN.md 5-4・5-5）。持ち主 CL-2（形だけ。中身は各担当）。

どの部品も、設定に書いた `モジュール:名前`（部品を返す関数）から作る（`load` の1か所）。通話を進める部品は、回線ごとに、回線の ID と
その回線の電話の部品（PC の部品へ頼む形＝`links.Links.driver`。通話の声もここを通る）を受け取って作る。電話の部品そのもの（スマホの
アプリ・音声の機器）は PC の部品の側（`salescall.link.phone`）。
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from salescall.core.access import Access, Actor
from salescall.core.gate import Claim
from salescall.link.basics import load   # 部品の作り方は link.basics の1か所（PC の部品と同じ）

__all__ = ["CallRunner", "Prober", "Verifier", "load"]


class CallRunner(Protocol):
    """通話を最後まで進める部品（会話＝CL-1。サーバーで動く。作るときに受け取った回線の電話の部品で、通話の声を受け渡す）。
    発信の出口（dial_exit.dial）がかけたあとに呼ぶ。
    試行の状態を終わりまで移し（attempts.move）、答え・報告・「もうかけないで」の申告は、書き込む入口（core/conversation_io.py）
    だけを通して書く：答えは決まるたびに `write_item`、報告は `save_report`（版3＝通話の事実の形）、申告はその場で
    `declare_do_not_call`（原則14）。エラーは自分でエラーの入口
    （watch.after_error）へ渡す。外へ出た誤りは、見守り役が「見守り役の誤り」として記録し、その試行を結果不明にする。"""

    def run(self, access: Access, actor: Actor, claim: Claim, *, tenant_id: UUID, client_id: UUID) -> None: ...


class Verifier(Protocol):
    """通話後の検証の部品（CL-1。Q21）。待ちの通話を確かめ、attempts.record_verification・finish_without_recording を呼ぶ。"""

    def run_pending(self, access: Access, actor: Actor, *, tenant_id: UUID) -> None: ...


class Prober(Protocol):
    """停止を解く確かめの部品（会話AI＝CL-1・回線＝電話接続）。確かめた結果（うまくいったか・根拠の短い文）を返す。"""

    def probe(self, stop: dict[str, Any]) -> tuple[bool, str]: ...

