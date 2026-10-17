"""締めの処理（1回の発信のあと）。持ち主 CL-2。DESIGN.md 4-1・5-1 の11・12・7-1・7-1c・7-2。

通話が終わり、通話後の検証が済んだ（または要らない）試行を、**1つの取引で**締める。ここが唯一の入口：
  (1) 判定の入力を集める（試行の事実・会話の報告・行の項目・業務の決まり・検証で見つかった拒否・設定）
  (2) 判定（core/judge.py。データベースを使わない）
  (3) 書く：試行の切れ方（attempts）・結果の版（results。入力の写しつき）・発信禁止（dnc。拒否と番号違い。広げ方は dnc の1か所）・
      次の動き（next_action。決め方は next_action の1か所）
- 同じ試行は1回だけ締める（2回目は前の結果を返す。データベースも、1つの試行に判定の版は1つと守る）。
- 締めが済むまで、門はその行に次をかけない（`awaiting_decision`）。
- お客様が言った別の番号（DESIGN.md 3-5）：そろえられた番号は、かけるかどうかに関係なく、その行の番号に足す（tasks の1か所。
  お客様が断ったら、この番号も止めるため＝3-6）。かけるか・どの番号へかけるかは次の動き（next_action.choose_number）が決める。
- **判定できなかった行は、判定待ちで残す**（2026-10-16 ユーザー決定）：判定できない＝こちらの誤りなので、何も決めない
  （切れ方・発信禁止・別の番号を書かず、結果 unknown と判定待ちだけ）。原因が直ったら（判定の処理の版・業務設定の版・
  録音から作り直した会話の報告のどれかが変わったら）`rejudge` が判定し直す。最初の締めと判定し直しは、同じ1つの道を通る。
- お客様が言った番号が番号違いだった（2026-10-15 ユーザー決定）：番号の確かめ（core/number_recheck.py。通話後の検証の中で記録済み）を
  読み、聞き違いなら正しい番号を行に足す。次にどこへかけるか（正しい番号／番号を聞いた通話の番号へ聞き直し）は次の動きが決める。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import (apology, approval, attempts, business_rules, call_report, dnc, next_action, notify, number_recheck,
                            quality_stage, results, settings, target_policy, tasks, usage)
from salescall.core.access import Tx
from salescall.core.judge import JUDGE_VERSION, PROGRAM_CAUSES, Facts, Item, JudgeInput, Judgement, judge, refusal
from salescall.core.phone import normalize


class NotReady(Exception):
    """まだ締められない（進行中・通話後の検証が済んでいない）。"""


def unsettled(tx: Tx) -> list[dict[str, Any]]:
    """締めを待っている試行（取引の依頼者の分）：終わっていて、検証が済んだ・要らないもので、判定の版が無いもの。"""
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
    usage.record_line_estimate(tx, attempt_id)   # 利用量の台帳（Q16）：回線の料金の推定（お客様が出た通話だけ。試行ごとに1回）
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
    inp, _, _ = _judge_input(tx, a, a["version_id"])
    report = inp.report
    delivered = bool(report is not None and report.apology_delivered)
    # 発信の拒否・番号違いは、判定と同じ1か所（judge.refusal）で決める（2026-10-16 の見直し J6：前は通話中の申告だけを見て、
    # 何も言わずに切った＝断った、を見落としていた）。断ったかを決められない（報告・切れ方の抜け）・通話後の検証で確かめられ
    # なかった ⇒ もうかけない（かけない側に倒す）
    j = refusal(inp)
    if j is not None and j.state == "unknown":
        # 断ったかを決められない（会話の報告・切れ方の材料の抜け＝こちらの誤り）：ふつうの通話と同じく、何も決めずに残す。
        # 締めずにおくので、この行と同じお客様のほかの行も、決まるまでかけない（2026-10-17 の見直し：前は「もうかけない」で
        # 締めてしまい、同じお客様のほかの行が、断ったかもしれないお客様へすぐかけえた）。報告を作り直せば、締め直される
        notify.emit(tx, "judge_unknown", key=f"judge_unknown:{attempt_id}", admin=j.detail.get("unknown_cause") in PROGRAM_CAUSES,
                    summary="お詫びの電話で、断られたかを決められませんでした（決まるまで、このお客様にはかけません）。",
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "unknown_cause": j.detail.get("unknown_cause"),
                          "error": j.detail.get("error")})
        cur = next_action.current(tx, a["task_id"])
        if cur is None or cur["kind"] != "hold":
            next_action.hold(tx, a["task_id"], cause=f"お詫びの電話：断ったかを決められない（{j.detail.get('unknown_cause')}）")
        return {"version_no": None, "state": None, "already": False, "held": True}
    if j is not None and j.disconnect is not None and a["disconnect"] is None:
        attempts.record_disconnect(tx, attempt_id, j.disconnect)
    tx.execute("INSERT INTO salescall.apology_outcomes (tenant_id, client_id, attempt_id, order_id, delivered)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s)",
               (attempt_id, order["order_id"], delivered))
    if j is not None and j.dnc_reason is not None:
        dnc.register(tx, a["phone_e164"], j.dnc_reason, source_attempt_id=attempt_id)
    refused = j is not None and j.state == "do_not_call"
    # 通話後の検証で拒否の聞き逃しを確かめられなかった：ふつうの通話と同じく、この行はもうかけない（かけない側に倒す）
    stop_reason = (f"apology_{j.dnc_reason}" if j is not None and j.dnc_reason in ("wrong_party", "fax_line") else
                   "apology_verification_failed" if a["verification"] == "failed" else None)
    _notice_forbidden(tx, a, prepare=False)
    decision = next_action.after_apology(tx, a["task_id"], delivered=delivered, refused=refused, stop_reason=stop_reason,
                                         dialed=next_action.Target(a["contact_id"], None, "apology", None))
    if decision.kind == "end":
        notify.emit(tx, "apology", key=f"apology:{attempt_id}",
                    summary={"apology_done": "お詫びを伝えました。",
                             "apology_unreached": "お詫びの電話が、決めた回数かけてもお客様に届きませんでした。",
                             "do_not_call": "お詫びの電話で、発信を断られました。",
                             "apology_wrong_party": "お詫びの電話の番号が、番号違いでした（その番号は発信禁止にしました）。",
                             "apology_fax_line": "お詫びの電話の番号が、FAX でした（その番号は発信禁止にしました）。",
                             "apology_verification_failed": "お詫びの電話の、通話後の確かめができませんでした（もうかけません）。"}.get(
                        decision.end_reason, f"お詫びの電話を終えました（{decision.end_reason}）。"),
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "order_id": order["order_id"]})
    usage.record_line_estimate(tx, attempt_id)
    return {"version_no": None, "state": None, "decision": decision, "delivered": delivered, "already": False}


def _notice_forbidden(tx: Tx, a: dict[str, Any], *, prepare: bool = True) -> bool:
    """言ってはいけないことを言った通話なら、お詫びの電話の案を作り（prepare。お詫びの電話そのものでは作らない）、契約者
    へ知らせる（その試行で1回。契約者は案を確認して承認するだけ）。言ったかを返す。"""
    forbidden = attempts.forbidden_speech(tx, a["attempt_id"])
    if not forbidden:
        return False
    summary = "言ってはいけないことを言った通話がありました。その行はひとまず終えました。"
    refs: dict[str, Any] = {"attempt_id": a["attempt_id"], "task_id": a["task_id"], "version_id": a["version_id"]}
    if prepare:
        draft = apology.prepare(tx, a["attempt_id"])
        summary += f"お詫びの電話の案：「{draft['text']}」承認すると、AI がこのお客様にかけて伝えます。"
        refs["apology_order_id"] = draft["order_id"]
    notify.emit(tx, "forbidden_speech", key=f"forbidden_speech:{a['attempt_id']}", summary=summary, refs=refs)
    return True


def _after(tx: Tx, a: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    quality_stage.evaluate(tx, a["version_id"])   # 品質の段（Q22）：通話ごとに評価（止める・次の段へ）
    approval.after_judged(tx, a["task_id"], out["version_no"], a["version_id"])   # 送ってよい印（モードを読むのは承認の部分だけ）
    return out


def _items(tx: Tx, task_id: UUID) -> dict[str, Item]:
    """行の項目（判定の入力。読むのはここだけ）。"""
    return {r["question_id"]: Item(r["state"], r["condition"], r["reason"]) for r in tx.all(
        "SELECT question_id, state::text AS state, condition, reason FROM salescall.item_values WHERE task_id = %s",
        (task_id,))}


def _items_snapshot(items: dict[str, Item]) -> dict[str, dict[str, Any]]:
    return {q: {"state": i.state, "condition": i.condition, "reason": i.reason} for q, i in sorted(items.items())}


def _inputs_now(tx: Tx, a: dict[str, Any]) -> dict[str, Any]:
    """いま判定し直すなら使う入力：判定の処理の版・業務設定の版（実施回のいまの版）・会話の報告の出どころ・行の項目
    （2026-10-16 見直し：会話の処理を直して項目の値を作り直しても、前は項目を比べず、判定し直さなかった）。"""
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (a["run_id"],))["v"]
    return {"judge_version": JUDGE_VERSION, "rules_version": str(version),
            "report_source": call_report.load_with_source(tx, a["attempt_id"])[1],
            "items": _items_snapshot(_items(tx, a["task_id"])),
            "refused_after": _refused_after(tx, a["attempt_id"])}   # 検証があとで拒否を見つけた（2026-10-17 の見直し）


def rejudge(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """判定待ちの行の、その試行を判定し直す（2026-10-16 ユーザー決定）。原因が直ったとき（判定の処理の版・業務設定の版・
    作り直した会話の報告・行の項目のどれかが、前の判定と違う）だけ判定する。何も変わっていなければ何もしない（None）。
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
              "report_source": last["snapshot"].get("report_source"), "items": last["snapshot"].get("items"),
              "refused_after": bool(last["snapshot"].get("refused_after"))}
    if now == before:
        return None                                # 何も直っていない：同じ入力で判定し直しても同じ結果
    out = _judge_and_write(tx, a, kind="rejudged", rules_version=now["rules_version"])
    return _after(tx, a, out)


def rejudge_held(tx: Tx) -> list[dict[str, Any]]:
    """取引の依頼者の、判定待ちの行をすべて見て、原因が直ったものを判定し直す（見守り役が決まった間隔で呼ぶ）。"""
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


def _refused_after(tx: Tx, attempt_id: UUID) -> bool:
    """通話後の検証が、その通話の拒否の聞き逃しを見つけて登録したか（1か所）。"""
    return tx.one("SELECT 1 FROM salescall.dnc_entries WHERE source_attempt_id = %s"
                  " AND reason = 'refused_found_after' LIMIT 1", (attempt_id,)) is not None


def _judge_input(tx: Tx, a: dict[str, Any], rules_version: Any) -> tuple[JudgeInput, str | None, dict[str, Any]]:
    """判定の入力を集める1か所（ふつうの締め・判定し直し・お詫びの電話の締め）。"""
    attempt_id = a["attempt_id"]
    report, report_source = call_report.load_with_source(tx, attempt_id)
    refused_after = _refused_after(tx, attempt_id)
    unclear_is_refusal = bool(settings.resolve(tx, "judge.unclear_disconnect_is_refusal", tenant_id=a["tenant_id"],
                                               client_id=a["client_id"], business_id=a["business_id"]))
    dialed = number_recheck.dialed(tx, attempt_id)
    inp = JudgeInput(Facts(a["state"], a["hangup_side"], a["reached_answer"], dialed["source"] == "callee"), report,
                     _items(tx, a["task_id"]), business_rules.load(tx, rules_version), refused_after, unclear_is_refusal)
    return inp, report_source, dialed


def _judge_and_write(tx: Tx, a: dict[str, Any], *, kind: str, rules_version: Any) -> dict[str, Any]:
    """判定して書く（最初の締めと、判定し直しの、同じ1つの道）。判定できなかったときは何も決めない（切れ方・発信禁止・
    別の番号を書かず、結果 unknown と判定待ちだけ）。"""
    attempt_id = a["attempt_id"]
    inp, report_source, dialed = _judge_input(tx, a, rules_version)
    report, rules, items = inp.report, inp.rules, inp.items
    refused_after, unclear_is_refusal = inp.refused_after, inp.unclear_is_refusal
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
        "items": _items_snapshot(items),
        "rules_version": str(rules.version_id), "refused_after": refused_after, "unclear_is_refusal": unclear_is_refusal,
    }
    version_no = results.append(tx, a["task_id"], kind=kind, business_state=j.state, attempt_id=attempt_id,
                                judge_version=JUDGE_VERSION, complaint=j.complaint, snapshot=snapshot, detail=j.detail,
                                decided_by="判定")
    if not decided:   # 判定できない：こちらの誤り。行は判定待ちで残す（次の動き）。契約者へ知らせる（その試行で1回）。
        #               プログラムの誤りなら管理者にも（直せるのは管理者だけ。原因ごとの決まりは judge の1か所）
        notify.emit(tx, "judge_unknown", key=f"judge_unknown:{attempt_id}",
                    admin=j.detail.get("unknown_cause") in PROGRAM_CAUSES,
                    summary="判定できない通話がありました（判定待ちで残しています）。",   # 注意は短く（原因は refs と画面で）
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "judge_version": JUDGE_VERSION,
                          "unknown_cause": j.detail.get("unknown_cause"), "error": j.detail.get("error")})
    forbidden = _notice_forbidden(tx, a)   # 言ってはいけないことを言った：その行はひとまず終え、契約者へ知らせる
    if j.complaint:
        # 苦情があった：その行は終える（次の動き）。苦情は、契約者が対応する特例（Intent.md 18-3「クレームになりそうなときなど、
        # 特例のときだけ人が対応する」）なので知らせる（2026-10-17 の計画書との突き合わせ：前は印を残すだけで、誰にも届かなかった）
        notify.emit(tx, "complaint", key=f"complaint:{attempt_id}", summary="苦情があった通話がありました。",
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"]})
    if j.dnc_reason is not None:
        dnc.register(tx, a["phone_e164"], j.dnc_reason, source_attempt_id=attempt_id)
    decision = next_action.after_call(
        tx, a["task_id"], j, verification=a["verification"], rules=rules,
        dialed=next_action.Target(a["contact_id"], None, a["purpose"], a["wrong_contact_id"]), dialed_at=a["claimed_at"],
        alternate=alternate, recheck=recheck, forbidden=forbidden)
    return {"version_no": version_no, "state": j.state, "decision": decision, "already": False}


def _number_material(tx: Tx, a: dict[str, Any], e164: str | None, *, source_attempt_id: UUID) -> next_action.Alternate:
    """お客様が言った番号（または、聞き違いだったときの正しい番号）を行の番号に足し、かけてよいかの材料を集める（1か所）。
    判定の結果に関係なく足す（同じ通話で断られたら、この番号も止める。足すのは発信禁止の登録より前）。
    source_attempt_id＝その番号をお客様が言った通話。e164 が None＝そろえられない番号（言ったままの番号は判定の中身に残る）。"""
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
    source = dialed["source_attempt_id"]       # その番号をお客様が言った通話
    corrected = None if r["outcome"] != "misheard" else _number_material(tx, a, r["corrected_e164"],
                                                                          source_attempt_id=source)
    return next_action.Recheck(
        r["outcome"], corrected,
        tx.one("SELECT contact_id FROM salescall.attempts WHERE attempt_id = %s", (source,))["contact_id"],
        number_recheck.asked_again_before(tx, a["task_id"], except_attempt=a["attempt_id"]))
