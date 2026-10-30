"""締めの処理（1回の発信のあと）。持ち主 CL-2。DESIGN.md 4-1・5-1 の11・12・7-1・7-1c・7-2。

通話が終わり、通話後の検証が済んだ（または要らない）試行を、**1つの取引で**締める。ここが唯一の入口：
  (1) 判定の入力を集める（試行の事実・会話の報告・行の項目・業務の決まり・検証で見つかった拒否・設定）
  (2) 判定（core/judge.py。データベースを使わない）
  (3) 書く：試行の切れ方（attempts）・結果の版（results。入力の写しつき）・発信禁止（dnc。拒否と番号違い。広げ方は dnc の1か所）・
      次の動き（next_action。決め方は next_action の1か所）
- 同じ試行は1回だけ締める（2回目は前の結果を返す。データベースも、1つの試行に判定の版は1つと守る）。
- 締めが済むまで、門はその行に次をかけない（`awaiting_decision`）。お客様のところで鳴らなかった電話は、番号のせいか回線のせいかが
  決まってから締める（**行の欄が、その電話の「判定待ち」＝DESIGN.md 17-1-2 のあいだは締めない**＝その行は、その電話をかけた回線では休み、
  回線は次の行へ進む）。締める前に、その電話の出来事（E1〜E5）をまだ状態の表へ渡していなければ渡す（core/health.after_call。1回だけ）。
  ほかの回線がその行にかけたら、表が判定待ちを解く（R-4）ので、まだ分からない電話も待たずに締める（行の予定は書き直さない＝あとの発信の
  締めが決める。`_later_dial`）。次の動きへ渡す「鳴らなかった電話のせい」は、試行のせいの欄（17-1-5。core/health.blame_of）。
  ⛔ 4段目の前：だれのせいかを、締めるたびに salescall.unrung_blame で導き直した。
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

from salescall.core import (apology, approval, attempts, business_rules, business_settings, call_report, calling_hours, dnc,
                            fix_requests, gate, health, next_action, notify, number_recheck, quality_stage, results, settings,
                            spoken_time, target_policy, target_recheck, tasks, usage)
from salescall.core.access import Tx
from salescall.core.judge import (JUDGE_VERSION, UNKNOWN_CAUSE_FIXERS, UNKNOWN_CAUSES, Facts, Item, JudgeInput, Judgement,
                                  classify_cut, cut_off as judge_cut_off, judge, refusal, unreached_today)
from salescall.core.phone import normalize


class NotReady(Exception):
    """まだ締められない（進行中・通話後の検証が済んでいない）。"""


def unsettled(tx: Tx) -> list[dict[str, Any]]:
    """締めを待っている試行（取引の依頼者の分）：終わっていて、検証が済んだ・要らないもので、判定の版が無いもの。"""
    return tx.all(
        "SELECT a.attempt_id, a.task_id FROM salescall.attempts a"
        f" WHERE {attempts.dialed('a')} AND NOT salescall.is_active(a.state)"
        "   AND a.verification IN ('done', 'not_needed')"
        "   AND NOT salescall.is_settled(a.attempt_id)"
        " ORDER BY a.ended_at")


_ATTEMPT = (
    "SELECT a.attempt_id, a.task_id, a.tenant_id, a.client_id, a.state, a.hangup_side, a.verification, a.disconnect, a.cut_off,"
    "       a.version_id, a.contact_id, a.phone_e164, a.purpose, a.wrong_contact_id, a.claimed_at, a.ended_at,"
    # お客様が言った「明日」「30分後」の起点＝通話の終わり。会話の処理が報告をしまった時刻のほうが早ければ、そちら（途中の誤りで
    # 終わった通話は、見守り役が後始末した時刻が終わりになり、何時間もずれる＝2026-10-19 の見直し）
    "       least(a.ended_at, (SELECT c.saved_at FROM salescall.call_reports c WHERE c.attempt_id = a.attempt_id)) AS spoken_at,"
    "       salescall.reached_answer(a.attempt_id) AS reached_answer, salescall.is_dialed(a.state) AS dialed,"
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
    if not a["dialed"] or a["verification"] is None:
        raise NotReady("発信していない・まだ終わっていない試行です")
    if a["verification"] == "pending":
        raise NotReady("通話後の検証が済んでいません")
    health.after_call(tx, attempt_id)   # まだ状態の表へ渡していなければ渡す（1回だけ。電話が終わった所＝回線ごとの処理が先に渡している）
    if health.waiting_decision(tx, attempt_id):
        # お客様のところで鳴らなかった電話が、番号のせいか回線のせいか、まだ分からない（行の欄が、この電話の判定待ち＝R-1）：決まるまで
        # 締めない（その行は締めを待って休む＝門の awaiting_decision・候補を出す処理。予定・約束の印・お詫びの印はそのまま＝R-1）。決まったら
        # （R-2・R-3）、番号のせいなら番号の欄の決まった順（next_action._failed_retry_at）、回線のせいなら、かけた予定のまま
        # （next_action._line_fault_again）で、1回だけ決める（⛔ 0129〜0141：締めの時点で分からない電話も番号のせいと同じ決まった順で予定を
        # 書き、壊れた回線の2時間で行が次の日へ送られ、約束の印も外れた＝見直し21a の場面 a6・a10）。そのあと同じ行にほかの回線がかけたら、
        # 表が判定待ちを解き（R-4）、待たずに締める（行の予定はあとの発信の締めが決める＝`_judge_and_write`・`_settle_apology`）
        return {"version_no": None, "state": None, "already": False, "waiting": "await_decision"}
    if target_recheck.missing(tx, attempt_id):   # 確かめ直しを待つ（締めない）。待つだけにせず、促す所が無ければ CL-1 へ修正依頼
        _recheck_missing_notice(tx, a)            # （この取引に残す＝断らずに返す）
        return {"version_no": None, "state": None, "already": False, "waiting": "target_recheck"}
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
    # 伝えたか：目的のお客様だと確かめられなかった通話（別の方に伝えた・確かめられなかった）は、伝えていない側に倒す
    delivered = bool(report is not None and report.apology_delivered) and not inp.target_unconfirmed
    # 発信の拒否・番号違いは、判定と同じ1か所（judge.refusal）で決める（2026-10-16 の見直し J6：前は通話中の申告だけを見て、
    # 何も言わずに切った＝断った、を見落としていた）。断ったかを決められない（報告・切れ方の抜け）・通話後の検証で確かめられ
    # なかった ⇒ もうかけない（かけない側に倒す）
    j = refusal(inp)
    if j is not None and j.state == "unknown":
        # 断ったかを決められない（会話の報告・切れ方の材料の抜け＝こちらの誤り）：ふつうの通話と同じく、何も決めずに残す。
        # 締めずにおくので、この行と同じお客様のほかの行も、決まるまでかけない（2026-10-17 の見直し：前は「もうかけない」で
        # 締めてしまい、同じお客様のほかの行が、断ったかもしれないお客様へすぐかけえた）。報告を作り直せば、締め直される
        _notice_unknown(tx, a, j, "お詫びの電話で、断られたかを決められませんでした（決まるまで、このお客様にはかけません）。")
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
    stop_reason = f"apology_{j.dnc_reason}" if j is not None and j.dnc_reason in ("wrong_party", "fax_line") else None
    _notice_forbidden(tx, a, prepare=False)
    # 途中で切れたかを試行に残す（ふつうの通話と同じ1か所。すぐかけ直してよいかは、残したものをデータベースの resume_allowed が読む）
    _cut_off(tx, a, classify_cut(inp.facts, report), report)
    # 同じ日にかけ直さないかの材料は、ふつうの電話の判定と同じ1か所（judge.unreached_today）から（お詫びの電話は業務の判定を書かない。
    # ⛔ 見直し16b：前は会話の報告から直に読み、確かめ直しで目的のお客様だと確かめられなかった通話の扱いが、ふつうの電話と違いえた）
    not_reached, again = unreached_today(inp)
    if _later_dial(tx, a):   # あとに同じ行で発信がある：行の予定は、あとの発信の締めが決める（`_judge_and_write` と同じ1つの決まり）
        usage.record_line_estimate(tx, attempt_id)
        return {"version_no": None, "state": None, "decision": None, "delivered": delivered, "already": False}
    decision = next_action.after_apology(tx, a["task_id"], delivered=delivered, refused=refused, stop_reason=stop_reason,
                                         dialed=next_action.Target(a["contact_id"], None, "apology", None),
                                         reached_target=inp.reached_target, not_reached=not_reached, again=again,
                                         dialed_at=a["claimed_at"], blame=health.blame_of(tx, attempt_id))
    if decision.kind == "end":
        notify.emit(tx, "apology", key=f"apology:{attempt_id}",
                    summary={"apology_done": "お詫びを伝えました。",
                             "apology_unreached": "お詫びの電話が、決めた回数かけてもお客様に届きませんでした。",
                             "do_not_call": "お詫びの電話で、発信を断られました。",
                             "apology_wrong_party": "お詫びの電話の番号が、番号違いでした（その番号は発信禁止にしました）。",
                             "apology_fax_line": "お詫びの電話の番号が、FAX でした（その番号は発信禁止にしました）。"}.get(
                        decision.end_reason, f"お詫びの電話を終えました（{decision.end_reason}）。"),
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"], "order_id": order["order_id"]})
    usage.record_line_estimate(tx, attempt_id)
    return {"version_no": None, "state": None, "decision": decision, "delivered": delivered, "already": False}


def _notice_forbidden(tx: Tx, a: dict[str, Any], *, prepare: bool = True) -> bool:
    """言ってはいけないことを言った通話なら、お詫びの電話の案を作り（prepare。お詫びの電話そのものでは作らない）、契約者
    へ知らせる（その試行で1回。契約者は案を確認して承認するだけ）。言ったかを返す。
    言ったのは会話の処理の不具合なので、止めずに会話の担当（CL-1）へ修正依頼を出し、管理者にも知らせる（確かめ・承認のため。
    Intent.md 10-3「仕組みの不具合は、直す担当へ修正依頼を出し、管理者は確かめて承認するだけ」。2026-10-22 の見直し）。"""
    forbidden = attempts.forbidden_speech(tx, a["attempt_id"])
    if not forbidden:
        return False
    summary = "言ってはいけないことを言った通話がありました。その行はひとまず終えました。"
    refs: dict[str, Any] = {"attempt_id": a["attempt_id"], "task_id": a["task_id"], "version_id": a["version_id"]}
    if prepare:
        draft = apology.prepare(tx, a["attempt_id"])
        summary += f"お詫びの電話の案：「{draft['text']}」承認すると、AI がこのお客様にかけて伝えます。"
        refs["apology_order_id"] = draft["order_id"]
    # 同じ不具合は1つ＝業務設定の版ごと（台本が違えば原因も違いうる。⛔ 2026-10-22 の見直し13：前は版をまたいで1つにまとめ、
    # 直したと報告するまで、ほかの業務の版で起きても依頼が出なかった）
    fix_requests.request(tx, "CL-1", "forbidden_speech", key=f"forbidden_speech:{a['version_id']}",
                         summary="言ってはいけないことを言った通話がありました（会話の処理の不具合）", refs=refs)
    notify.emit(tx, "forbidden_speech", key=f"forbidden_speech:{a['attempt_id']}", summary=summary, refs=refs, admin=True)
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


def _callee(tx: Tx, a: dict[str, Any]) -> calling_hours.CalleeSettings:
    """その試行の業務の、お客様の時間の設定（判定の入力。読むのは gate.callee_settings の1か所）。"""
    return gate.callee_settings(tx, tenant_id=a["tenant_id"], client_id=a["client_id"], business_id=a["business_id"])


def _inputs_now(tx: Tx, a: dict[str, Any]) -> dict[str, Any]:
    """いま判定し直すなら使う入力：判定の処理の版・業務設定の版（実施回のいまの版）・会話の報告の出どころ・行の項目
    （2026-10-16 見直し：会話の処理を直して項目の値を作り直しても、前は項目を比べず、判定し直さなかった）。"""
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (a["run_id"],))["v"]
    return {"judge_version": JUDGE_VERSION, "rules_version": str(version),
            "report_source": call_report.load_with_source(tx, a["attempt_id"])[1],
            "items": _items_snapshot(_items(tx, a["task_id"])),
            "refused_after": dnc.refused_in(tx, a["attempt_id"], "refused_found_after"),   # 検証があとで拒否を見つけた（2026-10-17 の見直し）
            "declared_in_call": dnc.refused_in(tx, a["attempt_id"], "refused_in_call"),   # 通話の途中の申告（2026-10-19 の見直し）
            "target_recheck": target_recheck.load(tx, a["attempt_id"]),   # 目的のお客様の確かめ直し（0108）
            # お客様の時間の設定（歯止め時間・1回分の長さ・営業時間。直したら判定し直す＝⛔ 見直し17b：前は比べていなかった）
            "callee": _callee(tx, a).to_json()}


def rejudge(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """判定待ちの行の、その試行を判定し直す（2026-10-16 ユーザー決定）。原因が直ったとき（判定の処理の版・業務設定の版・
    作り直した会話の報告・行の項目のどれかが、前の判定と違う）だけ判定する。何も変わっていなければ何もしない（None）。
    判定し直した結果は、結果の版 rejudged として足し、ふつうの締めと同じ道で次の動きを書く。"""
    a = _attempt(tx, attempt_id)
    last = tx.one("SELECT judge_version, snapshot FROM salescall.call_judgements WHERE attempt_id = %s", (attempt_id,))
    if last is None or awaiting_rejudge(tx, a["task_id"]) != attempt_id:
        return None
    if target_recheck.missing(tx, attempt_id):   # 確かめ直しを待つ行は、記録されてから判定し直す
        _recheck_missing_notice(tx, a)
        return None
    now = _inputs_now(tx, a)
    before = {"judge_version": last["judge_version"], "rules_version": last["snapshot"].get("rules_version"),
              "report_source": last["snapshot"].get("report_source"), "items": last["snapshot"].get("items"),
              "refused_after": bool(last["snapshot"].get("refused_after")),
              "declared_in_call": bool(last["snapshot"].get("declared_in_call")),
              "target_recheck": last["snapshot"].get("target_recheck"), "callee": last["snapshot"].get("callee")}
    if now == before:
        return None                                # 何も直っていない：同じ入力で判定し直しても同じ結果
    out = _judge_and_write(tx, a, kind="rejudged", rules_version=now["rules_version"])
    return _after(tx, a, out)


def awaiting_rejudge(tx: Tx, task_id: UUID) -> UUID | None:
    """その行で判定し直しを待っている試行（1か所）：判定できない結果が、まだ決まっていない扱いの試行のうち、いちばん新しいもの
    （決まりはデータベースの salescall.unknown_pending の1か所。断られたかがまだ決まっていない＝refusal_undecided も同じものを読む）。
    いまの予定が判定待ち（hold）の行だけでなく、途中で切れて判定できなかったので、お詫びのかけ直しを予定した行も入る（2026-10-22
    ユーザー：「通話のあとの確かめで断りの言葉が見つからなければ、お詫びのかけ直しをします」）。判定し直すのは、依頼者へ渡す結果を
    直すため（判定し直した試行のあとに発信があれば、行の予定は書き直さない＝`_judge_and_write`）。断られたかがまだ決まっていないか
    （同じお客様のほかの行を止めるか）は別の問い（データベースの salescall.refusal_undecided：途中で切れた通話は、通話のあとの確かめで
    断りの言葉が見つからなければ、断られていない＝0118）。同じ行のあとの発信がお客様につながって
    判定が決まったら、前の通話は決まったとみなし、判定し直さない（あとの通話の結果が行の結果。⛔ 見直し15：前は、あとの発信が
    あれば判定し直しの対象から外すだけで、前の通話は「まだ決まっていない」のまま残り、同じお客様のほかの行が永久にかからなかった）。
    あとの発信の締めがまだの行は、締めが済んでから（締めと判定し直しが、行の予定を同時に書かない）。"""
    row = tx.one(
        "SELECT a.attempt_id FROM salescall.attempts a"
        f" WHERE a.task_id = %s AND {attempts.dialed('a')} AND salescall.unknown_pending(a.attempt_id)"
        "   AND NOT salescall.task_unsettled(a.task_id)"
        " ORDER BY a.claimed_at DESC LIMIT 1", (task_id,))
    return None if row is None else row["attempt_id"]


def rejudge_held(tx: Tx) -> list[dict[str, Any]]:
    """取引の依頼者の、判定し直しを待っている行（`awaiting_rejudge`）をすべて見て、原因が直ったものを判定し直す（見守り役が決まった
    間隔で呼ぶ）。"""
    done = []
    for row in tx.all(
            "SELECT DISTINCT j.task_id FROM salescall.call_judgements j WHERE j.business_state = 'unknown'"):
        attempt_id = awaiting_rejudge(tx, row["task_id"])
        got = None if attempt_id is None else rejudge(tx, attempt_id)
        if got is not None:
            done.append(got)
    return done


def _recheck_missing_notice(tx: Tx, a: dict[str, Any]) -> None:
    """確かめ直しが要るのに記録が無く、通話後の検証がもう済んでいる（録音から作り直した報告・検証のあとに届いた報告に、判定なしの
    繋ぎが入った）：ほかに促す所が無いので、止めずに CL-1 へ修正依頼を出す（行とお客様が待ち続けないように。同じ不具合は、直したと
    報告するまで1つ。2026-10-22 の見直し）。検証がまだなら、検証の入口（attempts.record_verification）が記録を求める。"""
    if tx.one("SELECT verification::text AS v FROM salescall.attempts WHERE attempt_id = %s", (a["attempt_id"],))["v"] != "done":
        return
    fix_requests.request(tx, "CL-1", "target_recheck_missing", key="target_recheck_missing",
                         summary="通話後の検証が済んだあとの会話の報告（録音から作り直した・あとから届いた）に、判定なしで繋いだ"
                                 "通話がありますが、目的のお客様の確かめ直し（target_recheck.record）が記録されていません（締め・判定"
                                 "し直しを待っています）",
                         refs={"attempt_id": a["attempt_id"], "task_id": a["task_id"]})


def _judge_input(tx: Tx, a: dict[str, Any], rules_version: Any) -> tuple[JudgeInput, str | None, dict[str, Any]]:
    """判定の入力を集める1か所（ふつうの締め・判定し直し・お詫びの電話の締め）。"""
    attempt_id = a["attempt_id"]
    if target_recheck.missing(tx, attempt_id):   # 録音から作り直した報告に、判定なしの繋ぎが入ったときも（検証は済んでいる）
        raise NotReady("目的のお客様の確かめ直し（録音から）がありません。通話後の検証が記録するまで締めません")
    report, report_source = call_report.load_with_source(tx, attempt_id)
    refused_after = dnc.refused_in(tx, attempt_id, "refused_found_after")
    unclear_is_refusal = bool(settings.resolve(tx, "judge.unclear_disconnect_is_refusal", tenant_id=a["tenant_id"],
                                               client_id=a["client_id"], business_id=a["business_id"]))
    dialed = number_recheck.dialed(tx, attempt_id)
    get = lambda key: settings.resolve(tx, key, tenant_id=a["tenant_id"], client_id=a["client_id"],   # noqa: E731
                                       business_id=a["business_id"])
    periods = spoken_time.periods_from({k: spoken_time.range_seconds(get(key))     # 言葉ごとの幅（依頼者ごとの設定。0088）
                                        for k, key in spoken_time.PERIOD_SETTINGS.items()})
    inp = JudgeInput(Facts(a["state"], a["hangup_side"], a["reached_answer"], dialed["source"] == "callee", a["spoken_at"]),
                     report, _items(tx, a["task_id"]), business_rules.load(tx, rules_version), refused_after,
                     unclear_is_refusal, periods,
                     dnc.refused_in(tx, attempt_id, "refused_in_call"), target_recheck.unconfirmed(tx, attempt_id),
                     callee=_callee(tx, a),   # お客様の時間の設定（読むのは gate.callee_settings の1か所）
                     anyone_target=business_settings.anyone_target(tx, rules_version))   # 「誰でも」の業務か（見直し18b の (3)）
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
        "declared_in_call": inp.declared_in_call, "target_recheck": target_recheck.load(tx, attempt_id),
        "callee": inp.callee.to_json(),
    }
    version_no = results.append(tx, a["task_id"], kind=kind, business_state=j.state, attempt_id=attempt_id,
                                judge_version=JUDGE_VERSION, complaint=j.complaint, snapshot=snapshot, detail=j.detail,
                                decided_by="判定")
    if not decided:   # 判定できない：こちらの誤り。行は判定待ちで残す（次の動き）
        _notice_unknown(tx, a, j, "判定できない通話がありました（判定待ちで残しています）。")   # 注意は短く（原因は refs と画面で）
    forbidden = _notice_forbidden(tx, a)   # 言ってはいけないことを言った：その行はひとまず終え、契約者へ知らせる
    if j.complaint:
        # 苦情があった：その行は終える（次の動き）。苦情は、契約者が対応する特例（Intent.md 18-3「クレームになりそうなときなど、
        # 特例のときだけ人が対応する」）なので知らせる（2026-10-17 の計画書との突き合わせ：前は印を残すだけで、誰にも届かなかった）
        notify.emit(tx, "complaint", key=f"complaint:{attempt_id}", summary="苦情があった通話がありました。",
                    refs={"attempt_id": attempt_id, "task_id": a["task_id"]})
    if j.dnc_reason is not None:
        dnc.register(tx, a["phone_e164"], j.dnc_reason, source_attempt_id=attempt_id)
    # 途中で切れた（お客様の意思と関係なく）：決めるのは judge.cut_off の1か所
    # 判定できなかった通話も決める（途中で切れていれば、お詫びのかけ直しをする＝next_action。2026-10-22 ユーザー）
    cut_off = _cut_off(tx, a, j.detail.get("cut"), report)
    if _later_dial(tx, a):
        # この通話のあとに、この行でもう発信している（判定し直した通話・ほかの回線がかけたあとに締めた、まだ分からなかった電話＝
        # 見直し22 の 5）：行の予定は、あとの発信の締めが決める（結果の版と発信禁止だけを足し、予定は書き直さない＝判断はここの1か所。
        # お詫びの電話も同じ＝`_settle_apology`）。⛔ 見直し16：前は、何日も前に途中で切れた通話を判定し直すと、終えた行が
        # 生き返り「すぐお詫びのかけ直し」になった（続けて切れた数を、あとの発信から数えたため）。承認済みのお詫びは、判定が決まった
        # いま書く（apology の1か所）
        apology.schedule_if_ready(tx, a["task_id"])
        return {"version_no": version_no, "state": j.state, "decision": None, "already": False}
    decision = next_action.after_call(
        tx, a["task_id"], j, rules=rules,
        dialed=next_action.Target(a["contact_id"], None, a["purpose"], a["wrong_contact_id"]), dialed_at=a["claimed_at"],
        alternate=alternate, recheck=recheck, forbidden=forbidden, cut_off=cut_off,
        reached_target=inp.reached_target,   # 目的のお客様と話せた（約束の日のお詫びは済んだ）
        blame=health.blame_of(tx, attempt_id))   # 回線のせい・こちらのせい（試行のせいの欄）：かけた予定のまま（0129・0136）
    apology.schedule_if_ready(tx, a["task_id"])   # 締めの前に承認されていたお詫び：判定が決まったいま、予定を書く（apology の1か所）
    return {"version_no": version_no, "state": j.state, "decision": decision, "already": False}


def _later_dial(tx: Tx, a: dict[str, Any]) -> bool:
    """その試行のあとに、同じ行で発信まで進んだ試行があるか。"""
    return tx.one(f"SELECT EXISTS (SELECT 1 FROM salescall.attempts WHERE task_id = %s AND {attempts.dialed()}"
                  "  AND claimed_at > %s) AS later", (a["task_id"], a["claimed_at"]))["later"]


def _cut_off(tx: Tx, a: dict[str, Any], raw_cut: str | None, report: Any) -> bool:
    """途中で切れたか（決めるのは judge.cut_off の1か所）を決め、試行に1回だけ残す（ふつうの通話・お詫びの電話の締めの両方。続けて
    何回切れたかは、この記録を数える）。残してあれば、それを使う（判定し直しで変えない＝切れ方と同じ）。"""
    if a["cut_off"] is not None:
        return a["cut_off"]
    cut = judge_cut_off(reached_answer=bool(a["reached_answer"]), end_state=a["state"], raw_cut=raw_cut,
                        hangup_side=a["hangup_side"], closing_started=report is not None and report.closing_started)
    attempts.record_cut_off(tx, a["attempt_id"], cut)
    return cut


def _notice_unknown(tx: Tx, a: dict[str, Any], j: Judgement, summary: str) -> None:
    """判定できない（断られたかを決められない）通話を知らせる1か所（その試行で1回）：契約者へ知らせる。プログラムの誤りなら、
    止めずに直す担当へ修正依頼を出し、管理者にも知らせる（確かめ・承認のため。担当は judge.UNKNOWN_CAUSE_FIXERS の1か所）。"""
    cause = j.detail.get("unknown_cause")
    refs = {"attempt_id": a["attempt_id"], "task_id": a["task_id"], "judge_version": JUDGE_VERSION,
            "unknown_cause": cause, "error": j.detail.get("error")}
    fixer = UNKNOWN_CAUSE_FIXERS.get(cause)
    if fixer is not None:
        fix_requests.request(tx, fixer, cause, key=f"judge_unknown:{cause}", summary=UNKNOWN_CAUSES[cause],
                             refs=refs)
    notify.emit(tx, "judge_unknown", key=f"judge_unknown:{a['attempt_id']}", admin=fixer is not None, summary=summary,
                refs={**refs, "fixer": fixer})


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
        number_recheck.asked_again_before(tx, a["task_id"]))
