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
- **判定できなかった行は、判定待ちで残す**（2026-10-16 ユーザー決定）：判定できない＝こちらの誤りなので、何も決めない
  （切れ方・発信禁止・別の番号を書かず、結果 unknown と判定待ちだけ）。原因が直ったら（判定の処理の版・業務設定の版・
  録音から作り直した会話の報告のどれかが変わったら）`rejudge` が判定し直す。最初の締めと判定し直しは、同じ1つの道を通る。
- 相手が言った番号が番号違いだった（2026-10-15 ユーザー決定）：番号の確かめ（core/number_recheck.py。通話後の検証の中で記録済み）を
  読み、聞き違いなら正しい番号を行に足す。次にどこへかけるか（正しい番号／番号を聞いた通話の番号へ聞き直し）は次の動きが決める。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import (apology, approval, attempts, business_rules, call_report, dnc, next_action, notify, number_recheck,
                            quality_stage, results, settings, target_policy, tasks, usage)
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
        "   AND NOT salescall.is_settled(a.attempt_id)"
        " ORDER BY a.ended_at")


_ATTEMPT = (
    "SELECT a.attempt_id, a.task_id, a.tenant_id, a.client_id, a.state, a.hangup_side, a.verification, a.disconnect,"
    "       a.version_id, a.contact_id, a.phone_e164, a.purpose, a.wrong_contact_id, a.claimed_at,"
    "       salescall.reached_answer(a.attempt_id) AS reached_answer,"
    "       t.client_ref, t.run_id, r.business_id"
    " FROM salescall.attempts a"
    " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
    " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
    " WHERE a.attempt_id = %s")


def _attempt(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    a = tx.one(_ATTEMPT, (attempt_id,))
    if a is None:
        raise attempts.AttemptNotFound(str(attempt_id))
    return a


def settle(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """その試行を締める（判定 → 書く → 次の動き）。結果の版の番号・業務の状態・次の動きを返す。"""
    a = _attempt(tx, attempt_id)
    done = tx.one("SELECT version_no, business_state FROM salescall.task_results WHERE attempt_id = %s AND kind = 'judged'",
                  (attempt_id,))
    if done is not None:
        return {"version_no": done["version_no"], "state": done["business_state"], "already": True}
    if a["state"] in ("claimed", "released") or a["verification"] is None:
        raise NotReady("発信していない・まだ終わっていない試行です")
    if a["verification"] == "pending":
        raise NotReady("通話後の検証が済んでいません")
    if a["purpose"] == "apology":
        return _settle_apology(tx, a)
    out = _judge_and_write(tx, a, kind="judged", rules_version=a["version_id"])
    usage.record_line_estimate(tx, attempt_id)   # 利用量の台帳（Q16）：回線の料金の推定（相手が出た通話だけ。試行ごとに1回）
    return _after(tx, a, out)


def _settle_apology(tx: Tx, a: dict[str, Any]) -> dict[str, Any]:
    """お詫びの電話を締める（業務の判定はしない＝業務の結果を変えない。DESIGN.md 10-1-2）。伝えられたか（会話の報告。
    報告に無ければ、伝えていない側に倒す）・発信の拒否・言ってはいけないことを見て、次の動きを決める。"""
    attempt_id = a["attempt_id"]
    if tx.one("SELECT 1 FROM salescall.apology_outcomes WHERE attempt_id = %s", (attempt_id,)) is not None:
        return {"version_no": None, "state": None, "already": True}
    order = apology.for_task(tx, a["task_id"])
    if order is None:
        raise NotReady("承認されたお詫びの案が無いのに、お詫びの電話をかけています")
    report = call_report.load(tx, attempt_id)
    delivered = bool(report is not None and report.apology_delivered)
    refused = bool(report is not None and report.do_not_call)
    tx.execute("INSERT INTO salescall.apology_outcomes (tenant_id, client_id, attempt_id, order_id, delivered)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s)",
               (attempt_id, order["order_id"], delivered))
    if refused:
        dnc.register(tx, a["phone_e164"], "refused_in_call", source_attempt_id=attempt_id)
    _notice_forbidden(tx, a, prepare=False)
    decision = next_action.after_apology(tx, a["task_id"], delivered=delivered, refused=refused,
                                         dialed=next_action.Target(a["contact_id"], None, "apology", None))
    if decision.kind == "end":
        notify.emit(tx, "apology", key=f"apology:{attempt_id}",
                    summary={"apology_done": "お詫びを伝えました。",
                             "apology_unreached": "お詫びの電話が、決めた回数かけても相手に届きませんでした。",
                             "do_not_call": "お詫びの電話で、発信を断られました。"}.get(
                        decision.end_reason, f"お詫びの電話を終えました（{decision.end_reason}）。"),
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "order_id": order["order_id"]})
    usage.record_line_estimate(tx, attempt_id)
    return {"version_no": None, "state": None, "decision": decision, "delivered": delivered, "already": False}


def _notice_forbidden(tx: Tx, a: dict[str, Any], *, prepare: bool = True) -> bool:
    """言ってはいけないことを言った通話なら、お詫びの電話の案を作り（prepare。お詫びの電話そのものでは作らない）、管理者
    （運営者）へ知らせる（その試行で1回。人は案を確認して承認するだけ）。言ったかを返す。"""
    forbidden = attempts.forbidden_speech(tx, a["attempt_id"])
    if not forbidden:
        return False
    summary = "言ってはいけないことを言った通話がありました。その行はひとまず終えました。"
    refs: dict[str, Any] = {"attempt_id": a["attempt_id"], "task_id": a["task_id"], "version_id": a["version_id"]}
    if prepare:
        draft = apology.prepare(tx, a["attempt_id"])
        if draft["order_id"] is not None:
            summary += f"お詫びの電話の案：「{draft['text']}」承認すると、AI がこの相手にかけて伝えます。"
            refs["apology_order_id"] = draft["order_id"]
        else:
            summary += ("お詫びの電話の案は作れません（業務設定に、正しい案内が無い言ってはいけないこと："
                        f"{', '.join(draft['missing'])}）。")
    notify.emit(tx, "forbidden_speech", key=f"forbidden_speech:{a['attempt_id']}", summary=summary, refs=refs)
    return True


def _after(tx: Tx, a: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    quality_stage.evaluate(tx, a["version_id"])   # 品質の段（Q22）：通話ごとに評価（止める・次の段へ）
    approval.after_judged(tx, a["task_id"], out["version_no"], a["version_id"])   # 送ってよい印（モードを読むのは承認の部分だけ）
    return out


def _inputs_now(tx: Tx, a: dict[str, Any]) -> dict[str, Any]:
    """いま判定し直すなら使う入力の版：判定の処理の版・業務設定の版（実施回のいまの版）・会話の報告の出どころ。"""
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (a["run_id"],))["v"]
    return {"judge_version": JUDGE_VERSION, "rules_version": str(version),
            "report_source": call_report.load_with_source(tx, a["attempt_id"])[1]}


def rejudge(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """判定待ちの行の、その試行を判定し直す（2026-10-16 ユーザー決定）。原因が直ったとき（判定の処理の版・業務設定の版・
    作り直した会話の報告のどれかが、前の判定と違う）だけ判定する。何も変わっていなければ何もしない（None）。
    判定し直した結果は、結果の版 rejudged として足し、ふつうの締めと同じ道で次の動きを書く。"""
    a = _attempt(tx, attempt_id)
    held = tx.one("SELECT 1 FROM salescall.task_next_actions n WHERE n.task_id = %s AND n.kind = 'hold'"
                  " AND n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id)",
                  (a["task_id"],)) is not None
    last = tx.one("SELECT judge_version, snapshot FROM salescall.task_results WHERE attempt_id = %s"
                  " AND kind IN ('judged', 'rejudged') ORDER BY version_no DESC LIMIT 1", (attempt_id,))
    latest_attempt = tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state NOT IN ('claimed', 'released')"
                            " ORDER BY claimed_at DESC LIMIT 1", (a["task_id"],))["attempt_id"]
    if not held or last is None or latest_attempt != attempt_id:
        return None
    now = _inputs_now(tx, a)
    before = {"judge_version": last["judge_version"], "rules_version": last["snapshot"].get("rules_version"),
              "report_source": last["snapshot"].get("report_source")}
    if now == before:
        return None                                # 何も直っていない：同じ入力で判定し直しても同じ結果
    out = _judge_and_write(tx, a, kind="rejudged", rules_version=now["rules_version"])
    return _after(tx, a, out)


def rejudge_held(tx: Tx) -> list[dict[str, Any]]:
    """取引の依頼主の、判定待ちの行をすべて見て、原因が直ったものを判定し直す（見守り役が決まった間隔で呼ぶ）。"""
    done = []
    for row in tx.all(
            "SELECT (SELECT a.attempt_id FROM salescall.attempts a WHERE a.task_id = n.task_id"
            "        AND a.state NOT IN ('claimed', 'released') ORDER BY a.claimed_at DESC LIMIT 1) AS attempt_id"
            " FROM salescall.task_next_actions n WHERE n.kind = 'hold'"
            " AND n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id)"):
        got = rejudge(tx, row["attempt_id"])
        if got is not None:
            done.append(got)
    return done


def _judge_and_write(tx: Tx, a: dict[str, Any], *, kind: str, rules_version: Any) -> dict[str, Any]:
    """判定して書く（最初の締めと、判定し直しの、同じ1つの道）。判定できなかったときは何も決めない（切れ方・発信禁止・
    別の番号を書かず、結果 unknown と判定待ちだけ）。"""
    attempt_id = a["attempt_id"]
    report, report_source = call_report.load_with_source(tx, attempt_id)
    rules = business_rules.load(tx, rules_version)
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
    decided = j.state != "unknown"
    alternate = None if not decided or report is None or report.callback is None \
        or report.callback.reason != "other_number" else \
        _number_material(tx, a, report.callback.number_e164, source_attempt_id=attempt_id)
    recheck = _recheck(tx, a, j, dialed) if decided else None

    if j.disconnect is not None and a["disconnect"] is None:
        attempts.record_disconnect(tx, attempt_id, j.disconnect)
    snapshot = {
        "facts": {"end_state": a["state"], "hangup_side": a["hangup_side"], "reached_answer": a["reached_answer"],
                  "verification": a["verification"]},
        "report": None if report is None else report.to_json(), "report_source": report_source,
        "items": {q: {"state": i.state, "condition": i.condition, "reason": i.reason} for q, i in sorted(items.items())},
        "rules_version": str(rules.version_id), "refused_after": refused_after, "unclear_is_refusal": unclear_is_refusal,
    }
    version_no = results.append(tx, a["task_id"], kind=kind, business_state=j.state, attempt_id=attempt_id,
                                judge_version=JUDGE_VERSION, complaint=j.complaint, snapshot=snapshot, detail=j.detail,
                                decided_by="判定")
    if not decided:   # 判定できない：こちらの誤り。行は判定待ちで残す（次の動き）。運営者へ知らせる（その試行で1回）
        notify.emit(tx, "judge_unknown", key=f"judge_unknown:{attempt_id}",
                    summary=f"判定できない通話がありました（その行は判定待ちで残し、原因が直ったら判定し直します）。"
                            f"{j.detail.get('error', '')}",
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "judge_version": JUDGE_VERSION,
                          "unknown_cause": j.detail.get("unknown_cause")})
    forbidden = _notice_forbidden(tx, a)   # 言ってはいけないことを言った：その行はひとまず終え、管理者（運営者）へ知らせる
    if j.dnc_reason is not None:
        dnc.register(tx, a["phone_e164"], j.dnc_reason, source_attempt_id=attempt_id)
    decision = next_action.after_call(
        tx, a["task_id"], j, verification=a["verification"], rules=rules,
        dialed=next_action.Target(a["contact_id"], None, a["purpose"], a["wrong_contact_id"]), dialed_at=a["claimed_at"],
        alternate=alternate, recheck=recheck, forbidden=forbidden)
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
