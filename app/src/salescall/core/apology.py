"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）。持ち主 CL-2。

**人は確認と承認だけ**（2026-10-16 ユーザー：「人があくまで確認と承認するだけ」）。
(1) 言ってはいけないことを言った通話を締めるとき、システムがお詫びの電話の案を作る（`prepare`）：誤って案内したこと＝AI が
    実際に言ったこと（通話後の検証の記録 forbidden_findings）／正しい内容＝業務設定の、その言ってはいけないことの正しい案内。
    文面は決まった形（`TEMPLATE_*`。正本はここだけ）にはめる。案は管理者への知らせに入れる。
(2) 管理者が案を見て承認する（`approve`。人だけ。データベースが守る）。承認で、AI がその相手（その通話でかけた番号）へかける
    予定が書かれる。承認しなければ、かけない（「場合によっては」の判断は、承認するかどうか）。
- 業務設定に正しい案内が無い言ってはいけないこと（失礼な言葉など）が含まれるときは、案を作れない（文面の「正しくは…」が
  埋まらない）。その旨を知らせに書く。
- お詫びの電話は、業務の結果を変えない。届かなければ、別の時間帯に決まった回数（retry.apology_max）までかけ直す。
  相手が「もうかけないで」と言っていれば、門が止める。
- 会話の担当（CL-1）は、目的が apology の予約証を受けたら `for_task` で文面を読み、話し終えたら、伝えられたかを報告に入れる
  （call_report の apology_delivered）。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from salescall.core import business_rules, next_action
from salescall.core.access import Tx

# 2026-10-16 ユーザー決定の文面（「○○」に、誤って案内したことと正しい内容が入る）。誤りが2つ以上なら「また、…」で続ける
TEMPLATE_FIRST = "先ほどのお電話で、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
TEMPLATE_MORE = "また、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
TEMPLATE_END = "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。"


def text(pairs: list[dict[str, str]]) -> str:
    """伝える文面（1か所）。pairs＝[{"wrong": 誤って案内したこと, "correct": 正しい内容}, …]（1つ以上）。"""
    if not pairs:
        raise ValueError("誤って案内したことが1つもありません")
    parts = [(TEMPLATE_FIRST if n == 0 else TEMPLATE_MORE).format(wrong=p["wrong"].strip(), correct=p["correct"].strip())
             for n, p in enumerate(pairs)]
    return "".join(parts) + TEMPLATE_END


def prepare(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """言ってはいけないことを言った通話の、お詫びの電話の案を作る（締めの処理が呼ぶ。同じ通話で2回目からは前の案を返す）。
    答え：order_id と text（案を作れた）／missing（正しい案内が業務設定に無い、言ってはいけないことの名前。案は作らない）。"""
    done = tx.one("SELECT order_id, pairs FROM salescall.apology_orders WHERE source_attempt_id = %s", (attempt_id,))
    if done is not None:
        return {"order_id": done["order_id"], "text": text(done["pairs"]), "missing": []}
    a = tx.one("SELECT task_id, version_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    known = business_rules.load(tx, a["version_id"]).forbidden
    pairs, missing = [], []
    for f in tx.all("SELECT item, said FROM salescall.forbidden_findings WHERE attempt_id = %s ORDER BY seq", (attempt_id,)):
        correct = known[f["item"]].correct
        if correct is None:
            missing.append(f["item"])
        elif {"wrong": f["said"], "correct": correct} not in pairs:
            pairs.append({"wrong": f["said"], "correct": correct})
    if missing or not pairs:
        return {"order_id": None, "text": None, "missing": missing}
    order_id = tx.one(
        "INSERT INTO salescall.apology_orders (tenant_id, client_id, task_id, source_attempt_id, pairs)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s) RETURNING order_id",
        (a["task_id"], attempt_id, json.dumps(pairs, ensure_ascii=False)))["order_id"]
    return {"order_id": order_id, "text": text(pairs), "missing": []}


def approve(tx: Tx, order_id: int) -> None:
    """お詫びの電話の案を承認する（人だけ）。その通話の相手へ、いまかける予定を書く。"""
    o = tx.one("SELECT o.task_id, a.contact_id FROM salescall.apology_orders o JOIN salescall.attempts a"
               " ON a.attempt_id = o.source_attempt_id WHERE o.order_id = %s", (order_id,))
    if o is None:
        raise LookupError(f"そのお詫びの案は無い（または、取引の範囲の外）：{order_id}")
    tx.execute("INSERT INTO salescall.apology_approvals (tenant_id, client_id, order_id, approved_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor())", (order_id,))
    next_action.schedule_apology(tx, o["task_id"], o["contact_id"], cause=f"お詫びの電話の承認（案 {order_id}）")


def for_task(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """その行の、承認されたいちばん新しいお詫びの案と、伝える文面（無ければ None）。"""
    row = tx.one("SELECT o.order_id, o.source_attempt_id, o.pairs FROM salescall.apology_orders o"
                 " JOIN salescall.apology_approvals p ON p.order_id = o.order_id"
                 " WHERE o.task_id = %s ORDER BY o.order_id DESC LIMIT 1", (task_id,))
    return None if row is None else dict(row, text=text(row["pairs"]))
