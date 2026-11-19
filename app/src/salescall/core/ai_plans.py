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
PLAN_SETTINGS = {"conversation": "ai.plan_conversation", "import": "ai.plan_import"}   # 契約者が選べる AI → 設定の名前（外から読んでよい名前＝画面。CL-3 の 28）
_TIER_SETTING = {"conversation": "ai.tier_conversation", "import": "ai.tier_import"}   # 契約者のキーの枠（0257）
TIERS = ("free", "paid")


def plan(tx: Tx, ai: str, *, tenant_id: UUID) -> str:
    """その契約者で、その AI を呼ぶプラン（included＝システムのキー／own＝契約者のキー）。判定はいつも included。"""
    if ai not in AI_KINDS:
        raise ValueError(f"AI の種類は {list(AI_KINDS)} のどれかです（{ai!r}）")
    if ai == "judge":
        return "included"
    return settings.resolve(tx, PLAN_SETTINGS[ai], tenant_id=tenant_id, client_id=None, business_id=None)


def tier(tx: Tx, ai: str, *, tenant_id: UUID) -> str | None:
    """その契約者で、その AI を呼ぶキーの枠（1か所。0257）：契約者が自分で API を契約するプラン（own）なら、契約者が選んだ枠（free＝無料枠／
    paid＝有料枠。✅ 2026-10-16 ユーザー「契約者が無料枠と有料枠を選べる。…有料枠をおすすめする（強制はしない）」）。AI料金込み（included）と
    判定の AI は、システムのキーなので、契約者の枠は無い（None。システムのキーの枠は管理者が決める）。"""
    if plan(tx, ai, tenant_id=tenant_id) != "own":
        return None
    return settings.resolve(tx, _TIER_SETTING[ai], tenant_id=tenant_id, client_id=None, business_id=None)
