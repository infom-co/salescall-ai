"""締めの処理（1回の発信のあと）。持ち主 CL-2。DESIGN.md 4-1・5-1 の11・12・7-1・7-1c・7-2。

通話が終わり、通話後の検証が済んだ（または要らない）試行を、**1つの取引で**締める。ここが唯一の入口：
  (1) 判定の入力を集める（試行の事実・会話の報告・行の項目・業務の決まり・検証で見つかった拒否・設定）
  (2) 判定（core/judge.py。データベースを使わない）
  (3) 書く：試行の切れ方（attempts）・結果の版（results。入力の写しつき）・発信禁止（dnc。拒否と番号違い。広げ方は dnc の1か所）・
      次の動き（next_action。決め方は next_action の1か所）
- 同じ試行は1回だけ締める（2回目は前の結果を返す。データベースも、1つの試行に判定の版は1つと守る）。
- 締めが済むまで、門はその行に次をかけない（`awaiting_decision`）。
- ⏳ 相手が言った別の番号へのかけ直し（DESIGN.md 3-5）は、行の番号の表を作るまで締めない（`AlternateNumberNotReady`）。
  元の番号へ誤ってかけないため（作る前に通話が流れることは無い：会話の処理はまだ app/ に無い）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import attempts, business_rules, call_report, dnc, next_action, results, settings
from salescall.core.access import Tx
from salescall.core.judge import JUDGE_VERSION, Facts, Item, JudgeInput, judge


class NotReady(Exception):
    """まだ締められない（進行中・通話後の検証が済んでいない）。"""


class AlternateNumberNotReady(Exception):
    """相手が言った別の番号へのかけ直しは、行の番号の表（DESIGN.md 3-5）を作るまで扱えない。"""


def unsettled(tx: Tx) -> list[dict[str, Any]]:
    """締めを待っている試行（取引の依頼主の分）：終わっていて、検証が済んだ・要らないもので、判定の版が無いもの。"""
    return tx.all(
        "SELECT a.attempt_id, a.task_id FROM salescall.attempts a"
        " WHERE a.state NOT IN ('claimed', 'released') AND NOT salescall.is_active(a.state)"
        "   AND a.verification IN ('done', 'failed', 'not_needed')"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.attempt_id = a.attempt_id)"
        " ORDER BY a.ended_at")


def settle(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """その試行を締める（判定 → 書く → 次の動き）。結果の版の番号・業務の状態・次の動きを返す。"""
    a = tx.one(
        "SELECT a.attempt_id, a.task_id, a.tenant_id, a.client_id, a.state, a.hangup_side, a.verification,"
        "       a.version_id, a.phone_e164, salescall.reached_answer(a.attempt_id) AS reached_answer, r.business_id"
        " FROM salescall.attempts a"
        " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
        " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
        " WHERE a.attempt_id = %s", (attempt_id,))
    if a is None:
        raise attempts.AttemptNotFound(str(attempt_id))
    done = tx.one("SELECT version_no, business_state FROM salescall.task_results WHERE attempt_id = %s AND kind = 'judged'",
                  (attempt_id,))
    if done is not None:
        return {"version_no": done["version_no"], "state": done["business_state"], "already": True}
    if a["state"] in ("claimed", "released") or a["verification"] is None:
        raise NotReady("発信していない・まだ終わっていない試行です")
    if a["verification"] == "pending":
        raise NotReady("通話後の検証が済んでいません")

    report = call_report.load(tx, attempt_id)
    rules = business_rules.load(tx, a["version_id"])
    items = {r["question_id"]: Item(r["state"], r["condition"], r["reason"]) for r in tx.all(
        "SELECT question_id, state::text AS state, condition, reason FROM salescall.item_values WHERE task_id = %s",
        (a["task_id"],))}
    refused_after = tx.one("SELECT 1 FROM salescall.dnc_entries WHERE source_attempt_id = %s"
                           " AND reason = 'refused_found_after' LIMIT 1", (attempt_id,)) is not None
    unclear_is_refusal = bool(settings.resolve(tx, "judge.unclear_disconnect_is_refusal", tenant_id=a["tenant_id"],
                                               client_id=a["client_id"], business_id=a["business_id"]))
    inp = JudgeInput(Facts(a["state"], a["hangup_side"], a["reached_answer"]), report, items, rules, refused_after,
                     unclear_is_refusal)
    j = judge(inp)
    if j.state == "callback_scheduled" and j.detail["callback"]["reason"] == "other_number":
        raise AlternateNumberNotReady("別の番号へのかけ直しは、行の番号の表（DESIGN.md 3-5）を作ってから扱います")

    if j.disconnect is not None:
        attempts.record_disconnect(tx, attempt_id, j.disconnect)
    snapshot = {
        "facts": {"end_state": a["state"], "hangup_side": a["hangup_side"], "reached_answer": a["reached_answer"],
                  "verification": a["verification"]},
        "report": None if report is None else report.to_json(),
        "items": {q: {"state": i.state, "condition": i.condition, "reason": i.reason} for q, i in sorted(items.items())},
        "rules_version": str(rules.version_id), "refused_after": refused_after, "unclear_is_refusal": unclear_is_refusal,
    }
    version_no = results.append(tx, a["task_id"], kind="judged", business_state=j.state, attempt_id=attempt_id,
                                judge_version=JUDGE_VERSION, complaint=j.complaint, snapshot=snapshot, detail=j.detail,
                                decided_by="判定")
    if j.dnc_reason is not None:
        dnc.register(tx, a["phone_e164"], j.dnc_reason, source_attempt_id=attempt_id)
    decision = next_action.after_call(tx, a["task_id"], j, verification=a["verification"], rules=rules)
    return {"version_no": version_no, "state": j.state, "decision": decision, "already": False}
