"""締めの処理（1回の発信のあと）。持ち主 CL-2。DESIGN.md 4-1・5-1 の11・12・7-1・7-1c・7-2。

通話が終わり、通話後の検証が済んだ（または要らない）試行を、**1つの取引で**締める。ここが唯一の入口：
  (1) 判定の入力を集める（試行の事実・会話の報告・行の項目・業務の決まり・検証で見つかった拒否・設定）
  (2) 判定（core/judge.py。データベースを使わない）
  (3) 書く：試行の切れ方（attempts）・結果の版（results。入力の写しつき）・発信禁止（dnc。拒否と番号違い。広げ方は dnc の1か所）・
      次の動き（next_action。決め方は next_action の1か所）
- 同じ試行は1回だけ締める（2回目は前の結果を返す。データベースも、1つの試行に判定の版は1つと守る）。
- 締めが済むまで、門はその行に次をかけない（`awaiting_decision`）。
- 相手が言った別の番号（DESIGN.md 3-5）：そろえられた番号は、かけるかどうかに関係なく、その行の番号に足す（tasks の1か所。
  相手が断ったら、この番号も止めるため＝3-6）。かけるか・どの番号へかけるかは次の動き（next_action.choose_number）が決める。
- 相手が言った番号が番号違いだった（2026-10-15 ユーザー決定）：番号の確かめ（core/number_recheck.py。通話後の検証の中で記録済み）を
  読み、聞き違いなら正しい番号を行に足す。次にどこへかけるか（正しい番号／番号を聞いた通話の番号へ聞き直し）は次の動きが決める。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import (attempts, business_rules, call_report, dnc, next_action, notify, number_recheck,
                            quality_stage, results, settings, target_policy, tasks)
from salescall.core.access import Tx
from salescall.core.judge import JUDGE_VERSION, Facts, Item, JudgeInput, Judgement, judge
from salescall.core.phone import normalize


class NotReady(Exception):
    """まだ締められない（進行中・通話後の検証が済んでいない）。"""


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
        "       a.version_id, a.contact_id, a.phone_e164, a.purpose, a.wrong_contact_id, a.claimed_at,"
        "       salescall.reached_answer(a.attempt_id) AS reached_answer,"
        "       t.client_ref, r.business_id"
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
    dialed = number_recheck.dialed(tx, attempt_id)
    inp = JudgeInput(Facts(a["state"], a["hangup_side"], a["reached_answer"], dialed["source"] == "callee"), report, items,
                     rules, refused_after,
                     unclear_is_refusal)
    j = judge(inp)
    alternate = None if report is None or report.callback is None or report.callback.reason != "other_number" else \
        _number_material(tx, a, report.callback.number_e164, source_attempt_id=attempt_id)
    recheck = _recheck(tx, a, j, dialed)

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
    if j.state == "unknown":   # 判定できない：判定の処理か入力の誤り。次の動きは終える。運営者へ知らせる（監視）
        notify.emit(tx, "judge_unknown", key=f"judge_unknown:{attempt_id}",
                    summary=f"判定できない通話がありました（判定の処理か入力の誤り。その行は終えました）。{j.detail.get('error', '')}",
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "judge_version": JUDGE_VERSION})
    if j.dnc_reason is not None:
        dnc.register(tx, a["phone_e164"], j.dnc_reason, source_attempt_id=attempt_id)
    decision = next_action.after_call(
        tx, a["task_id"], j, verification=a["verification"], rules=rules,
        dialed=next_action.Target(a["contact_id"], None, a["purpose"], a["wrong_contact_id"]), dialed_at=a["claimed_at"],
        alternate=alternate, recheck=recheck)
    quality_stage.evaluate(tx, a["version_id"])   # 品質の段（Q22）：通話ごとに評価（止める・次の段へ）
    return {"version_no": version_no, "state": j.state, "decision": decision, "already": False}


def _number_material(tx: Tx, a: dict[str, Any], e164: str | None, *, source_attempt_id: UUID) -> next_action.Alternate:
    """相手が言った番号（または、聞き違いだったときの正しい番号）を行の番号に足し、かけてよいかの材料を集める（1か所）。
    判定の結果に関係なく足す（同じ通話で断られたら、この番号も止める。足すのは発信禁止の登録より前）。
    source_attempt_id＝その番号を相手が言った通話。e164 が None＝そろえられない番号（言ったままの番号は判定の中身に残る）。"""
    scope = dict(tenant_id=a["tenant_id"], client_id=a["client_id"], business_id=a["business_id"])
    allowed = bool(settings.resolve(tx, "callback.call_alternate_number", **scope))
    if e164 is None:
        return next_action.Alternate(None, allowed, "number_unknown")
    number = normalize(e164)
    contact_id = tasks.add_callee_number(tx, a["task_id"], number, source_attempt_id=source_attempt_id)
    blocked = target_policy.block_reason(tx, number, client_ref=a["client_ref"], **scope)
    return next_action.Alternate(contact_id, allowed, None if blocked is None else blocked[0])


def _recheck(tx: Tx, a: dict[str, Any], j: Judgement, dialed: dict[str, Any]) -> next_action.Recheck | None:
    """言われた番号が番号違いだったとき：番号の確かめを読み、次の動きの材料にする（そうでなければ None）。"""
    if not (j.state == "not_reached" and j.detail.get("not_reached") == "wrong_alternate"):
        return None
    r = number_recheck.load(tx, a["attempt_id"])
    if r is None:   # 通話後の検証が、確かめを記録せずに済むことはない（attempts.record_verification が守る）
        raise NotReady("番号違いだった番号の確かめ（証拠との付け合わせ）がありません")
    source = dialed["source_attempt_id"]       # その番号を相手が言った通話
    corrected = None if r["outcome"] != "misheard" else _number_material(tx, a, r["corrected_e164"],
                                                                          source_attempt_id=source)
    return next_action.Recheck(
        r["outcome"], corrected,
        tx.one("SELECT contact_id FROM salescall.attempts WHERE attempt_id = %s", (source,))["contact_id"],
        number_recheck.asked_again_before(tx, a["task_id"], except_attempt=a["attempt_id"]))
