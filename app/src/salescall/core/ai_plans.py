"""契約プラン（AI の料金。どのキーで呼ぶか。2026-11-05 ユーザー決定。データベースは 0176）。持ち主 CL-2。

✅ ユーザー：「契約者の契約プランで選べるようにする。AI料金込みプランと、自分でAPIを契約するプラン」・「判定は企業秘密なので
こちらのみ。会話は選べるように。」

- 会話 AI（conversation）と、業務を取り込む専用の AI（import）は、契約者ごとに選ぶ（設定 ai.plan_conversation・ai.plan_import）：
  included＝AI料金込み（システムのキーで呼び、料金はその契約者の分として数える）／own＝契約者が自分で API を契約する
  （その契約者のキーで呼ぶ）。
- 判定の AI（judge）は、いつもシステムのキー（選べない＝設定を作らない）。
- どの AI をどのキーで呼ぶかを決めるのは `plan` の1か所（呼ぶ側は、ここの答えでキーを選ぶ）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import settings
from salescall.core.access import Tx

AI_KINDS = ("conversation", "import", "judge")
PLANS = ("included", "own")
_SETTING = {"conversation": "ai.plan_conversation", "import": "ai.plan_import"}   # 契約者が選べる AI → 設定の名前


def plan(tx: Tx, ai: str, *, tenant_id: UUID) -> str:
    """その契約者で、その AI を呼ぶプラン（included＝システムのキー／own＝契約者のキー）。判定はいつも included。"""
    if ai not in AI_KINDS:
        raise ValueError(f"AI の種類は {list(AI_KINDS)} のどれかです（{ai!r}）")
    if ai == "judge":
        return "included"
    return settings.resolve(tx, _SETTING[ai], tenant_id=tenant_id, client_id=None, business_id=None)
