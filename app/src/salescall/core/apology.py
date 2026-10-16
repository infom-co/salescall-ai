"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）。持ち主 CL-2。

**人は確認と承認だけ**（2026-10-16 ユーザー：「人があくまで確認と承認するだけ」）。
(1) 言ってはいけないことを言った通話を締めるとき、システムがお詫びの電話の案を作る（`prepare`）：誤って案内したこと＝AI が
    実際に言ったこと（通話後の検証の記録 forbidden_findings）／正しい内容＝業務設定の、その言ってはいけないことの正しい案内。
    文面は決まった形（`TEMPLATE_*`。正本はここだけ）にはめる。案は管理者への知らせに入れる。
(2) 管理者が案を見て承認する（`approve`。人だけ。データベースが守る）。承認で、AI がその相手（その通話でかけた番号）へかける
    予定が書かれる。承認しなければ、かけない（「場合によっては」の判断は、承認するかどうか）。
- 業務設定に正しい案内が無い言ってはいけないこと（失礼な言葉など）を言ったときは、別の文面（`TEMPLATE_IMPROPER_*`）を使う
  （2026-10-16 ユーザー：「その場合は、別のトークスクリプトを作成しておきましょう」）。**何を言ってしまったかを、文面に入れる**
  （2026-10-16 ユーザー：「何が不適切なのかが相手に伝わらない、お詫びは、お詫びになりません」）。正しい案内のあるものと両方なら、
  両方を伝える。
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

# 2026-10-16 ユーザー決定の文面（「○○」に、誤って案内したことと正しい内容が入る）。誤りが2つ以上なら「また、…」で続ける。
# 誤った通話と、お詫びの電話の日が違えば（相手が出ず、翌日以降にかけ直したとき）「先ほど」を「先日」にする（2026-10-16 ユーザー承認）
TEMPLATE_FIRST = "{when}のお電話で、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
WHEN_SAME_DAY, WHEN_LATER = "先ほど", "先日"
TEMPLATE_MORE = "また、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
TEMPLATE_END = "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。"
# 正しい案内の無いことを言ったとき（失礼な言葉など）の文面（2026-10-16 ユーザーの文面。{said}＝言ってしまったこと「…」の並び）
TEMPLATE_IMPROPER_ONLY = "{when}のお電話で、{said}といった不適切な発言をしてしまいました。大変失礼いたしました。誠に申し訳ございませんでした。"
TEMPLATE_IMPROPER_ALSO = "また、{said}といった不適切な発言もしてしまいました。"
TEMPLATE_END_BOTH = "不正確なご案内と不適切な発言をしてしまいまして、誠に申し訳ございませんでした。"


def text(pairs: list[dict[str, str]], *, improper: list[str] = (), same_day: bool = True) -> str:
    """伝える文面（1か所）。pairs＝[{"wrong": 誤って案内したこと, "correct": 正しい内容}, …]。improper＝正しい案内の無いこと
    （失礼な言葉など）として実際に言ってしまったことの並び。same_day＝誤った通話と同じ日（日本時間）に伝えるか（違えば「先日」）。"""
    if not pairs and not improper:
        raise ValueError("お詫びすることが1つもありません")
    when = WHEN_SAME_DAY if same_day else WHEN_LATER
    said = "".join(f"「{x.strip()}」" for x in improper)
    if not pairs:
        return TEMPLATE_IMPROPER_ONLY.format(when=when, said=said)
    parts = [(TEMPLATE_FIRST if n == 0 else TEMPLATE_MORE).format(when=when, wrong=p["wrong"].strip(),
                                                                  correct=p["correct"].strip())
             for n, p in enumerate(pairs)]
    return "".join(parts) + (TEMPLATE_IMPROPER_ALSO.format(said=said) + TEMPLATE_END_BOTH if improper else TEMPLATE_END)


def prepare(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """言ってはいけないことを言った通話の、お詫びの電話の案を作る（締めの処理が呼ぶ。同じ通話で2回目からは前の案を返す）。
    答え：order_id と text（案）。"""
    done = tx.one("SELECT order_id, pairs, improper FROM salescall.apology_orders WHERE source_attempt_id = %s", (attempt_id,))
    if done is not None:
        return {"order_id": done["order_id"], "text": text(done["pairs"], improper=done["improper"])}
    a = tx.one("SELECT task_id, version_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    known = business_rules.load(tx, a["version_id"]).forbidden
    pairs, improper = [], []
    for f in tx.all("SELECT item, said FROM salescall.forbidden_findings WHERE attempt_id = %s ORDER BY seq", (attempt_id,)):
        correct = known[f["item"]].correct
        if correct is None:                          # 正しい案内の無いこと（失礼な言葉など）：別の文面に、言ったことを入れる
            if f["said"] not in improper:
                improper.append(f["said"])
        elif {"wrong": f["said"], "correct": correct} not in pairs:
            pairs.append({"wrong": f["said"], "correct": correct})
    order_id = tx.one(
        "INSERT INTO salescall.apology_orders (tenant_id, client_id, task_id, source_attempt_id, pairs, improper)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s) RETURNING order_id",
        (a["task_id"], attempt_id, json.dumps(pairs, ensure_ascii=False), json.dumps(improper, ensure_ascii=False)))["order_id"]
    return {"order_id": order_id, "text": text(pairs, improper=improper)}


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
    """その行の、承認されたいちばん新しいお詫びの案と、いま伝える文面（無ければ None。誤った通話と今日の日付で「先ほど」か
    「先日」かが決まる）。"""
    row = tx.one("SELECT o.order_id, o.source_attempt_id, o.pairs, o.improper,"
                 "       salescall.jst(a.claimed_at)::date = salescall.jst(now())::date AS same_day"
                 " FROM salescall.apology_orders o JOIN salescall.apology_approvals p ON p.order_id = o.order_id"
                 " JOIN salescall.attempts a ON a.attempt_id = o.source_attempt_id"
                 " WHERE o.task_id = %s ORDER BY o.order_id DESC LIMIT 1", (task_id,))
    return None if row is None else dict(row, text=text(row["pairs"], improper=row["improper"], same_day=row["same_day"]))
