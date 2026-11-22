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

from typing import Any
from uuid import UUID

from salescall.core import notify, settings
from salescall.core.access import Tx

AI_KINDS = ("conversation", "import", "judge")
PLANS = ("included", "own")
PLAN_SETTINGS = {"conversation": "ai.plan_conversation", "import": "ai.plan_import"}   # 契約者が選べる AI → 設定の名前（外から読んでよい名前＝画面。CL-3 の 28）
TIER_SETTINGS = {"conversation": "ai.tier_conversation", "import": "ai.tier_import"}   # 契約者のキーの枠（0257。外から読んでよい名前＝画面。CL-3 の 55）
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
    return settings.resolve(tx, TIER_SETTINGS[ai], tenant_id=tenant_id, client_id=None, business_id=None)


# ═════════ 切り替えの申し込み（0288。CL-3 の連絡帳 79。計画書 1-4「契約者は画面から申し込み、管理者が確かめて切り替える」） ═════════
def request(tx: Tx, ai: str, new_plan: str, *, note: str | None = None) -> int:
    """取引の契約者が、その AI のプランの切り替えを申し込む（管理者か契約者だけ・AI ごとに開いた申し込みは1つ＝データベースが守る）。管理者へ
    知らせる。切り替えるのは管理者（設定の値を足す1か所。足すと、データベースが申し込みを閉じる）。申し込みの番号を返す。"""
    if ai not in PLAN_SETTINGS or new_plan not in PLANS:
        raise ValueError(f"選べる AI は {list(PLAN_SETTINGS)}・プランは {list(PLANS)} です（{ai!r}・{new_plan!r}）")
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    if plan(tx, ai, tenant_id=tenant) == new_plan:
        raise ValueError("いまのプランと同じです")
    rid = tx.one("INSERT INTO salescall.ai_plan_requests (tenant_id, ai, plan, note) VALUES (salescall.cur_tenant(), %s, %s, %s)"
                 " RETURNING request_id", (ai, new_plan, (note or "").strip() or None))["request_id"]
    notify.emit(tx, "plan_request", key=f"plan_request:{rid}", admin=True, contractor=False,
                summary="契約プランの切り替えの申し込みがありました。確かめて、切り替えるか見送ってください。",
                refs={"request_id": rid, "ai": ai, "plan": new_plan})
    return rid


def open_request(tx: Tx, ai: str) -> dict[str, Any] | None:
    """取引の契約者の、その AI の開いている申し込み（画面の「申し込み中」。無ければ None）。"""
    return tx.one("SELECT r.request_id, r.plan, r.note, r.requested_at, r.requested_by FROM salescall.ai_plan_requests r"
                  " WHERE r.request_id = salescall.plan_request_open(salescall.cur_tenant(), %s)", (ai,))


def requests(tx: Tx) -> list[dict[str, Any]]:
    """開いている申し込み（全部の契約者。管理者の全体の取引で＝データベースが守る）。"""
    return tx.all("SELECT * FROM salescall.plan_requests_open()")


def decline(tx: Tx, request_id: int, *, note: str) -> None:
    """管理者が申し込みを見送る（理由を書く）。"""
    tx.execute("SELECT salescall.decline_plan_request(%s, %s)", (request_id, note.strip()))
