"""次の動き（Q4。DESIGN.md 7-2・4-1）。持ち主 CL-2。

- 次の予定の正本は「次の動きの記録」の最新だけ（「かけ直す（日時）」か「終える（理由）」か「判定待ち」）。
- **判定待ち**（hold。2026-10-16 ユーザー決定）：判定できなかった（業務の状態 unknown）・判定の結果から次の動きを決められなかった
  行は、終えずに残す（かけない）。原因が直ったら、締めの処理が判定し直して、ふつうの次の動きを書く（core/settle.py の `rejudge`）。
- 次の動きの記録を足すのは、この部品だけ（I-7・I-12。設計書の `scheduled_at`＝次の予定を書くのは、ここだけ。app/checks が見張る）。
- **何を決めるかは `decide_after_call`・`decide_after_denial` の1か所**（データベースを使わない部品。同じ入力からは同じ答え）。
  通話のあと（締めの処理 core/settle.py が判定と一緒に呼ぶ `after_call`）と、門で止められたあと（`after_denial`）の両方が使う。
  - 回数と間隔は門の数え方（`gate.pacing`）を読む（数え直さない）。再発信の方針は業務の決まり（core/business_rules.py）。
  - 決める予定は、門の最も早い時刻より前にしない（門で止まるだけの予定を作らない）。
  - 門で止められた理由が「前の発信の締めがまだ」「予定の時刻より前」なら、何も書かない（締めより先に予定を書き直さない）。
- **どの番号へかけるかも、ここだけが決める**（`choose_number`。DESIGN.md 3-5）。予定（かけ直す）は必ず番号を持ち、門はその番号へ
  かける。決め方：お客様が「別の番号にかけ直して」と言った ⇒ その番号（業務の設定で「かけない」・かけない理由がある ⇒ 終える。
  番号は記録して依頼者へ渡す）／それ以外 ⇒ いまかけた番号のまま（門で止められたあとは、いまの予定の番号のまま）。
  最初の予定（取込）は一覧の番号。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from dataclasses import dataclass
from datetime import timedelta

from salescall.core import attempts, calling_hours, gate, settings, tasks
from salescall.core.access import Tx
from salescall.core.business_rules import Rules
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.judge import Judgement
from salescall.core.tasks import TaskNotFound, lock


def current(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """いまの予定（いちばん新しい記録）。無ければ None（まだ何も決めていない）。"""
    return tx.one(
        "SELECT task_id, seq, kind, call_at, end_reason, cause, promised, contact_id, purpose, wrong_contact_id, callee_at,"
        "       decided_at"
        " FROM salescall.task_next_actions"
        " WHERE task_id = %s ORDER BY seq DESC LIMIT 1",
        (task_id,),
    )


PURPOSES = ("normal", "ask_number_again", "apology", "resume")   # 発信の目的：ふつう／番号の聞き直し（3-5）／お詫び（10-1-2）／
#                                                                   続き（途中で切れた通話のかけ直し。お詫びから始めて続ける）


@dataclass(frozen=True)
class Target:
    """次の予定でかける先（決めるのは `choose_number` の1か所）。かけられないときは contact_id が None で end_reason を持つ。"""
    contact_id: UUID | None
    end_reason: str | None = None
    purpose: str = "normal"
    wrong_contact_id: UUID | None = None    # 番号の聞き直しのとき：番号違いだった番号


def _append(tx: Tx, task_id: UUID, *, call_at: datetime | None, end_reason: str | None, cause: str,
            call_now: bool = False, promised: bool | None = None, target: Target | None = None,
            callee_at: datetime | None = None, hold: bool = False) -> int:
    calls = call_at is not None or call_now
    if hold and (calls or end_reason is not None):
        raise ValueError("判定待ちは、日時も終える理由も持ちません")
    if not hold and (calls == (end_reason is not None) or (call_now and call_at is not None)):
        raise ValueError("「かけ直す（日時）」か「終える（理由）」の、どちらか1つだけです")
    if calls != (promised is not None) or calls != (target is not None and target.contact_id is not None):
        raise ValueError("かけ直すときは、約束したかけ直しか（promised）と、かける先（番号・目的）を必ず決めます"
                         "（終えるときは持たない）")
    if target is not None and (target.purpose not in PURPOSES
                               or (target.purpose == "ask_number_again") != (target.wrong_contact_id is not None)):
        raise ValueError("発信の目的は normal か ask_number_again（聞き直しは、番号違いだった番号を必ず持つ）")
    if callee_at is not None and not (calls and promised):
        raise ValueError("お客様が指定した時刻を持てるのは、約束したかけ直しだけです")
    lock(tx, task_id)  # 同じタスクへの追記を1つずつ順にする（tasks.py の説明）
    row = tx.one(
        "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, end_reason, cause,"
        "                                         promised, contact_id, purpose, wrong_contact_id, callee_at)"
        " SELECT t.tenant_id, t.client_id, t.task_id,"
        "        coalesce((SELECT max(n.seq) FROM salescall.task_next_actions n WHERE n.task_id = t.task_id), 0) + 1,"
        "        %s, CASE WHEN %s THEN now() ELSE %s::timestamptz END, %s, %s, %s, %s, %s, %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING seq",
        ("hold" if hold else "call" if calls else "end", call_now, call_at, end_reason, cause, promised,
         target and target.contact_id, target and target.purpose, target and target.wrong_contact_id, callee_at, task_id),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row["seq"]


def schedule_call(tx: Tx, task_id: UUID, call_at: datetime, *, contact_id: UUID, cause: str, promised: bool,
                  purpose: str = "normal", wrong_contact_id: UUID | None = None, callee_at: datetime | None = None) -> int:
    """かけ直す（日時・かける先）を記録する。promised＝お客様と約束したかけ直し（お客様に頼まれた・こちらが約束した）か。
    約束したかけ直しは、門の「同じお客様への最小の間隔・1日の回数」の対象外（2026-10-14 ユーザー決定）。
    contact_id：かける番号（その行の番号の表にある番号だけ。データベースも守る）。purpose：発信の目的（PURPOSES）。
    callee_at：お客様が自分で指定した時刻（その時刻を含むその日のNG時間の枠の中は、NG時間でもかけてよい）。"""
    return _append(tx, task_id, call_at=call_at, end_reason=None, cause=cause, promised=promised,
                   target=Target(contact_id, None, purpose, wrong_contact_id), callee_at=callee_at)


def schedule_now(tx: Tx, task_id: UUID, *, cause: str) -> int:
    """いまから、一覧の番号へかけてよい（日時はデータベースの時刻。取込で登録したタスクの最初の予定）。
    実際にかけるか・いつかけるかは門が決める（時間帯・間隔・停止など）。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=False,
                   target=Target(tasks.list_number(tx, task_id)["contact_id"]))


def hold(tx: Tx, task_id: UUID, *, cause: str) -> int:
    """判定待ちにする（かけない・終えない）。同じ取引で、その行のまだ発信していない予約を取り消す。"""
    seq = _append(tx, task_id, call_at=None, end_reason=None, cause=cause, hold=True)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    return seq


def end(tx: Tx, task_id: UUID, reason: str, *, cause: str) -> int:
    """終える（理由）を記録する。同じ取引で、その行のまだ発信していない予約を取り消す（門の判定に効く変更。5-2-1 の2）。"""
    seq = _append(tx, task_id, call_at=None, end_reason=reason, cause=cause)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    return seq


# ── 何を決めるか（1か所。DESIGN.md 7-2） ──
END_STATES = frozenset({"completed", "declined", "do_not_call", "out_of_scope", "concluded"})   # 目的を果たした・終わった


@dataclass(frozen=True)
class Decision:
    kind: str                    # call / end / hold（判定待ち）
    at: datetime | None          # call のとき
    promised: bool | None        # call のとき：お客様と約束したかけ直しか
    end_reason: str | None       # end のとき
    rule: str                    # どの決まりで決めたか（記録の cause に残す）
    target: Target | None = None     # call のとき：かける先（番号・目的）
    callee_at: datetime | None = None    # call のとき：お客様が自分で指定した時刻（NG時間でもかけてよい根拠）

    @property
    def contact_id(self) -> UUID | None:
        return None if self.target is None else self.target.contact_id


def _call(at: datetime, promised: bool, rule: str, target: Target, callee_at: datetime | None = None) -> Decision:
    return Decision("call", at, promised, None, rule, target, callee_at)


def _end(reason: str, rule: str) -> Decision:
    return Decision("end", None, None, reason, rule)


def _hold(rule: str) -> Decision:
    return Decision("hold", None, None, None, rule)


NO_OPENING_RECHECK = timedelta(days=1)


def _no_opening(now: datetime, promised: bool, target: Target, callee_at: datetime | None = None) -> Decision:
    """これから先（31日）、かけてよい時間帯が見つからない：行を終えず、1日後にもう一度確かめる（1か所）。
    時間帯が無いのは設定のその時の形（依頼者の時間帯を空にした・NG時間が時間帯を覆う・祝日だけの時間帯など）で、
    お客様の側の理由ではない。前は行を終えていて、設定を戻しても取り返せなかった（2026-10-16 の見直し G6。
    2026-10-16 ユーザー：「何でもかんでも止めるんじゃないよ」）。門が時間帯の外で止め、次の動きがまた確かめる。"""
    return _call(now + NO_OPENING_RECHECK, promised, "これから先、かけてよい時間帯が見つからない：1日後にもう一度確かめる",
                 target, callee_at)


@dataclass(frozen=True)
class Alternate:
    """お客様が「別の番号にかけ直して」と言ったときの材料（締めの処理が集める）。"""
    contact_id: UUID | None      # 行の番号に足した連絡先（そろえられない番号なら None）
    allowed: bool                # 業務の設定 callback.call_alternate_number（かけ直すか）
    blocked: str | None          # その番号へかけない理由の名前（target_policy の1か所。そろえられない番号は number_unknown）


@dataclass(frozen=True)
class Recheck:
    """お客様が言った番号が番号違いだったときの材料（締めの処理が集める。番号の確かめ＝core/number_recheck.py）。"""
    outcome: str                         # misheard / as_said / cannot_check
    corrected: Alternate | None          # 聞き違いのとき：正しい番号（行に足したもの）とその材料
    source_contact_id: UUID              # 番号を聞いた通話でかけた番号（聞き直しはここへかける）
    asked_again_before: bool             # この行で、前にも聞き直しをした


def _alternate_target(alt: Alternate) -> Target:
    if not alt.allowed:
        return Target(None, "alternate_not_called")          # 業務の設定でかけない：番号を記録して依頼者へ渡すだけ（3-5）
    if alt.blocked is not None:
        return Target(None, f"alternate_blocked:{alt.blocked}")   # かけない種類・発信禁止・そろえられない：記録だけ
    return Target(alt.contact_id)


def choose_number(j: Judgement, *, dialed: Target, alternate: Alternate | None, recheck: Recheck | None) -> Target:
    """次にかける先（番号と発信の目的。1か所。DESIGN.md 3-5）。dialed＝いまの通話でかけた先（番号・目的）。
    - お客様が「別の番号にかけ直して」と言った ⇒ その番号（設定でかけない・かけない理由があれば、かけない）。
    - お客様が言った番号が番号違いだった ⇒ 番号の確かめのとおり：聞き違い ⇒ 正しい番号／言われたとおり・確かめられない ⇒
      番号を聞いた通話の番号へ「番号の聞き直し」（同じ行で2度目なら、聞き直さずに終える）。
    - 聞き直しの通話で、会話に至らなかった ⇒ 目的を引き継ぐ（いつかけるか・何回までかは decide_after_call：
      聞き直しの回数の上限まで、毎回、前と別の時間帯に。2026-10-15 ユーザー）。
    - それ以外 ⇒ いまかけた番号のまま（ふつうの目的）。"""
    cb = j.detail.get("callback") if j.state == "callback_scheduled" else None
    if cb is not None and cb["reason"] == "other_number":
        if alternate is None:
            raise ValueError("別の番号へのかけ直しなのに、その番号の材料がありません")
        return _alternate_target(alternate)
    if j.state == "not_reached" and j.detail.get("not_reached") == "wrong_alternate":
        if recheck is None:
            raise ValueError("言われた番号が番号違いだったのに、番号の確かめがありません")
        if recheck.outcome == "misheard":
            return _alternate_target(recheck.corrected)
        if recheck.asked_again_before:
            return Target(None, "alternate_wrong_again")     # 聞き直しても、また番号違い：記録して終える
        return Target(recheck.source_contact_id, None, "ask_number_again", dialed.contact_id)
    if j.state == "open" and dialed.purpose == "ask_number_again":
        return dialed
    return Target(dialed.contact_id)


@dataclass(frozen=True)
class AskAgain:
    """別の時間帯にかけ直す電話（番号の聞き直し・お詫び）に出なかったときの材料（2026-10-15 ユーザー：「3回までなどの制限を
    かけ、別の時間帯にかけ直す」）。"""
    tries: int                   # この行で、その目的でかけた回数（発信まで進んだもの全部。今回を含む）
    limit: int                   # 設定 retry.ask_number_again_max／retry.apology_max
    at: datetime | None          # まだ試していない時間帯の、次にかけられる時刻（無ければ None）


def decide_after_call(j: Judgement, *, cut_off: bool = False, normal: gate.Pacing, promised: gate.Pacing, rules: Rules,
                      times_with_key: int, now: datetime, target: Target, retry_at: datetime | None,
                      ask: AskAgain | None = None, forbidden: bool = False) -> Decision:
    """通話のあとの次の動き。normal / promised＝次にかける先へ、ふつうの予定・約束したかけ直しにした場合の門の数え方。
    times_with_key＝この行で、今回の結果と同じ再発信の方針の鍵の結果が出た回数（今回を含む）。
    target＝`choose_number` の答え（次にかける先、または終える理由）。ask＝聞き直しで出なかったときの材料（そのときだけ）。
    retry_at＝この結果の再発信の方針の間隔（n 時間おき／n 日後）が指す時刻（`_retry_at` の1か所で出す。かけてよい時間帯が
    これから先に無ければ None）。forbidden＝この通話で、言ってはいけないことを言った（通話後の検証で、2つの耳で確かめた）。"""
    # 判定できない：何も決めない（終える理由も含めて）。終えずに残し、原因が直ったら判定し直す（2026-10-16 ユーザー決定）。
    # ⚠️ 先に見る：言ってはいけないこと・検証の失敗・苦情で先に終えると、判定し直す道が無くなり、断られたかが決まらないまま
    # 同じお客様のほかの行を止め続けた（2026-10-17 の見直し）。判定し直したあと、この順でもう一度決まる
    if j.state == "unknown":
        return _hold(f"判定できなかった（{j.detail.get('unknown_cause')}）：判定待ちで残す")
    if forbidden:   # 2026-10-15 ユーザー：「その行はひとまず終える。依頼主でなく、管理者へ報告。場合によっては、お詫びを入れる」
        return _end("forbidden_speech", "言ってはいけないことを言った：その行はひとまず終える（契約者へ知らせた）")
    if j.state in END_STATES:
        return _end(j.state, "目的を果たした・終わった結果")
    if j.complaint:
        return _end("complaint", "苦情があった")
    if target.contact_id is None:
        return _end(target.end_reason, "お客様が言った番号へは、かけない（番号は記録して依頼者へ渡す）")
    key = j.detail.get("policy_key")
    if j.state == "not_reached" and j.detail.get("not_reached") == "wrong_alternate":
        # お客様に頼まれたかけ直しが、まだ果たせていない（番号違い）：約束したかけ直しのまま、すぐ（門の最も早い時刻）
        return _call(promised.earliest or now, True, "言われた番号が番号違い：番号の確かめのとおり", target)
    # 途中で切れた（お客様が出たあと、こちらの仕組みが止まった・回線が切れた＝お客様の意思ではない）：復旧次第、お詫びとともに
    # すぐかけ直す（2026-10-17 ユーザー：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」）。目的「続き」で、会話は
    # お詫びから始めて続ける。お客様が時刻を言ったかけ直しの約束があれば、その時刻を守る（下）
    if cut_off and target.purpose in ("normal", "resume") and not (
            j.state == "callback_scheduled" and j.detail["callback"]["at"] is not None):
        return _call(promised.earliest or now, True, "途中で切れた：復旧次第、お詫びとともにすぐかけ直す",
                     Target(target.contact_id, purpose="resume"))
    if j.state == "callback_scheduled":
        cb = j.detail["callback"]
        callee_at = None
        if cb["at"] is not None:
            at = callee_at = datetime.fromisoformat(cb["at"])   # お客様が言った時刻（NG時間でもかけてよい）
        else:
            p = rules.retry[key]
            if p.action == "end" or times_with_key > p.max_times:
                return _end(key, "かけ直しの時刻が決まっていない約束：再発信の方針で終える")
            if retry_at is None:
                return _no_opening(now, True, target)
            at = retry_at
        return _call(max(at, promised.earliest or now), True, "お客様と約束したかけ直し", target, callee_at)
    if target.purpose == "ask_number_again":   # 聞き直しの電話に出なかった（会話に至らなかった）
        if ask is None:
            raise ValueError("番号の聞き直しで出なかったのに、その材料がありません")
        if ask.tries >= ask.limit:
            return _end("ask_number_again_unanswered", f"番号の聞き直し：{ask.limit} 回かけても出なかった（番号は依頼者へ渡す）")
        if ask.at is None:
            return _no_opening(now, True, target)
        return _call(max(ask.at, promised.earliest or ask.at), True,
                     f"番号の聞き直し：出なかった ⇒ 別の時間帯に（{ask.tries + 1} 回目）", target)
    if normal.remaining <= 0:
        return _end("max_attempts", "この行にかけてよい回数を使い切った")
    if key is None:
        return _hold("再発信の方針の鍵が無い（判定の処理の誤り）：判定待ちで残す")
    p = rules.retry[key]
    if p.action == "end":
        return _end(key, f"再発信の方針：{key} は終える")
    if times_with_key > p.max_times:
        return _end(f"policy_max:{key}", f"再発信の方針：{key} は {p.max_times} 回まで")
    if retry_at is None:
        return _no_opening(now, False, target)
    return _call(max(retry_at, normal.earliest or now), False, f"再発信の方針：{key}", target)


def decide_after_denial(d: gate.Denied, *, current_promised: bool, current: Target | None, pacing: gate.Pacing,
                        now: datetime, opening: datetime | None, retry_wait: int,
                        current_callee_at: datetime | None = None) -> Decision | None:
    """門で止められたあとの次の動き。何も書かないときは None。かける先（番号・目的）と、お客様が指定した時刻は、いまの予定のまま。"""
    if d.reason in ("awaiting_decision", "not_due", "no_schedule", "task_ended", "judge_pending", "client_inactive"):
        # 締めの処理・いまの予定に任せる（先に書き直さない）。予定の無い行・終えた行に、門の答えから書かない。無効の依頼者は
        # 依頼者全体の状態なので、行ごとに予定を書き直さない（前は5分ごとに書き直し続けた＝2026-10-18 の見直し。有効に戻せば、
        # いまの予定のままかける。候補を出す処理は client_active の1か所で無効の依頼者を除く）
        return None
    if current is None or current.contact_id is None:
        raise ValueError("いまの予定に番号がありません（かけ直す予定は必ず番号を持つ）")
    if d.kind == "permanent":
        return _end(d.reason, f"門：待っても変わらない理由（{d.reason}）")
    if d.reason == "outside_hours":
        if opening is None:
            return _no_opening(now, current_promised, current, current_callee_at)
        return _call(max(opening, pacing.earliest or opening), current_promised, "門：時間帯の外 ⇒ 次の時間帯の始まり",
                     current, current_callee_at)
    return _call(max(now + timedelta(seconds=retry_wait), pacing.earliest or now), current_promised,
                 f"門：待てば変わる理由（{d.reason}）", current, current_callee_at)


def _write(tx: Tx, task_id: UUID, d: Decision) -> int:
    if d.kind == "end":
        return end(tx, task_id, d.end_reason, cause=d.rule)
    if d.kind == "hold":
        return hold(tx, task_id, cause=d.rule)
    return _append(tx, task_id, call_at=d.at, end_reason=None, cause=d.rule, promised=d.promised, target=d.target,
                   callee_at=d.callee_at)


# 途中で切れた通話のあとの、お詫びしてすぐかけ直す電話（目的 resume）は、同じ行で続けて2回まで。それでも切れたら、ふつうの
# 再発信の方針（間隔をあけてかけ直す）へ移す（2026-10-18 ユーザー 案A。電波の悪いお客様に、お詫びの電話をきりなくかけ続けないため）
RESUME_IN_A_ROW = 2


def after_call(tx: Tx, task_id: UUID, j: Judgement, *, rules: Rules, dialed: Target,
               dialed_at: datetime, alternate: Alternate | None, recheck: Recheck | None, forbidden: bool,
               cut_off: bool = False) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。判定の結果から次の動きを決めて記録する（同じ取引）。
    cut_off＝お客様が出たあと、お客様の意思と関係なく途中で切れた（決めるのは締めの処理の1か所）。
    dialed＝この通話でかけた先（番号・目的）、dialed_at＝かけた時刻。alternate＝お客様が言った別の番号の材料（言われたときだけ）。
    recheck＝言われた番号が番号違いだったときの番号の確かめ（そのときだけ）。"""
    now = tx.one("SELECT now() AS now")["now"]
    key = j.detail.get("policy_key")
    times = 0 if key is None else tx.one(
        "SELECT count(*) AS n FROM salescall.task_results WHERE task_id = %s AND kind IN ('judged', 'rejudged')"
        " AND detail ->> 'policy_key' = %s", (task_id, key))["n"]
    target = choose_number(j, dialed=dialed, alternate=alternate, recheck=recheck)
    count_at = target.contact_id or dialed.contact_id     # 終えるときは数え方を使わない（いまの番号で数えておく）
    promised = gate.pacing(tx, task_id, contact_id=count_at, promised=True)
    ask = None
    if target.purpose == "ask_number_again" and j.state == "open":
        ask = _new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="ask_number_again")
    policy = rules.retry.get(key) if key is not None else None
    retry_at = _retry_at(tx, task_id, policy.interval, now, dialed_at) if policy and policy.action == "call" else None
    if cut_off:   # すでに続けて何回、お詫びしてすぐかけ直したか（今回を含む）
        resumed = tx.one(
            "SELECT coalesce(min(n) FILTER (WHERE purpose <> 'resume') - 1, count(*)) AS n FROM ("
            "  SELECT purpose, row_number() OVER (ORDER BY claimed_at DESC) AS n FROM salescall.attempts"
            "  WHERE task_id = %s AND state NOT IN ('claimed', 'released')) x", (task_id,))["n"]
        cut_off = resumed < RESUME_IN_A_ROW
    d = decide_after_call(j, cut_off=cut_off, normal=gate.pacing(tx, task_id, contact_id=count_at, promised=False),
                          promised=promised, rules=rules, times_with_key=times, now=now, target=target,
                          retry_at=retry_at, ask=ask, forbidden=forbidden)
    _write(tx, task_id, d)
    return d


def _scope(tx: Tx, task_id: UUID) -> dict[str, Any]:
    return tx.one("SELECT t.tenant_id, t.client_id, r.business_id FROM salescall.tasks t"
                  " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id WHERE t.task_id = %s",
                  (task_id,))


def _retry_at(tx: Tx, task_id: UUID, interval: tuple[str, int], now: datetime, dialed_at: datetime) -> datetime | None:
    """再発信の方針の間隔が指す時刻（1か所。2026-10-15 ユーザー：「2時間おき・3時間おき・○時間おき・○日後」を業務ごとに）。
    - n 時間おき ⇒ いまから n 時間後（時間帯の外・NG時間に当たれば、門が止め、次の動きが次の始まりへ回す）。
    - n 日後 ⇒ n 日後の日の、前回と別の時間帯で、かけてよい最も早い時刻（「時間帯により出られないことはよくある」）。
      その日に別の時間帯が無ければ、その日以降で、かけてよい最も早い時刻。これから先に無ければ None。"""
    unit, n = interval
    if unit == "hours":
        return now + timedelta(hours=n)
    scope = _scope(tx, task_id)
    get = lambda key: settings.resolve(tx, key, **scope)   # noqa: E731
    boundaries = list(get("retry.time_band_boundaries"))
    hours, length = gate.calling_hours_for(tx, **scope), timedelta(seconds=int(get("gate.claim_lifetime")))
    clock = tx.one("SELECT salescall.jst(%s) AS now, salescall.jst(%s) AS tried", (now, dialed_at))
    day = datetime.combine(clock["now"].date() + timedelta(days=n), datetime.min.time())
    try:
        found = (calling_hours.next_opening_in_new_band(hours, holiday_checker(tx), day, length, boundaries,
                                                        {calling_hours.band_of(clock["tried"], boundaries)}, horizon_days=0)
                 or calling_hours.next_opening(hours, holiday_checker(tx), day, length))
    except CalendarOutOfRange:   # 暦の無い日に当たる：決めつけず、その日の始まりにして、門にもう一度確かめさせる
        found = day
    return None if found is None else tx.one("SELECT salescall.from_jst(%s) AS t", (found,))["t"]


_BAND_RETRY_LIMITS = {"ask_number_again": "retry.ask_number_again_max", "apology": "retry.apology_max"}


def _new_band_retry(tx: Tx, task_id: UUID, after: datetime, *, purpose: str) -> AskAgain:
    """別の時間帯にかけ直す電話（番号の聞き直し・お詫び）で出なかったときの材料：その目的でかけた回数と試した時間帯
    （門の数え方）・まだ試していない時間帯の次にかけられる時刻。"""
    scope = _scope(tx, task_id)
    get = lambda key: settings.resolve(tx, key, **scope)   # noqa: E731
    boundaries = list(get("retry.time_band_boundaries"))
    dials = gate.purpose_dials(tx, task_id, purpose=purpose)
    # 回数：発信まで進んだもの全部（回線の側で繋がらなかったものも数える。数えないと、繋がらない番号へ上限なくかけ続ける＝
    # 2026-10-16 の見直しで見つけた穴。ふつうの再発信も、再発信の方針の回数は結果ごとに全部数える）。
    # 試した時間帯：お客様が出られた発信だけ（回線の都合で繋がらなかった時間帯は、まだ試していない）。
    # 最小の間隔：お客様と約束したかけ直しは門の最小の間隔の対象外なので、この電話は、ここで前の発信から間を空ける。
    local = tx.one("SELECT salescall.jst(%s) AS t", (after,))["t"]
    if dials:
        local = max(local, max(d.ended for d in dials) + timedelta(seconds=int(get("gate.same_contact_min_interval"))))
    hours, length = gate.calling_hours_for(tx, **scope), timedelta(seconds=int(get("gate.claim_lifetime")))
    try:
        found = calling_hours.next_opening_in_new_band(hours, holiday_checker(tx), local, length, boundaries,
                                                       {calling_hours.band_of(d.at, boundaries) for d in dials if d.counted})
        if found is None:   # 試していない時間帯が無い（夜だけ開くお店など）：直後にかけ直さず、翌日以降の同じ頃から
            found = calling_hours.next_opening(hours, holiday_checker(tx), local + timedelta(days=1), length)
        at = None if found is None else tx.one("SELECT salescall.from_jst(%s) AS t", (found,))["t"]
    except CalendarOutOfRange:   # 暦の無い日に当たる：決めつけず、待ってから門にもう一度確かめさせる
        at = after + timedelta(seconds=int(get("gate.denied_retry_wait")))
    return AskAgain(len(dials), int(get(_BAND_RETRY_LIMITS[purpose])), at)


def schedule_apology(tx: Tx, task_id: UUID, contact_id: UUID, *, cause: str) -> int:
    """お詫びの電話の予定を書く（core/apology.py の `order` だけが呼ぶ）。いまから、その番号へ。お客様と約束したかけ直しと同じく、
    回数の上限・1日の回数・最小の間隔では止めない（お詫びは営業の電話ではない）。時間帯・NG時間・発信禁止は門が止める。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=True,
                   target=Target(contact_id, purpose="apology"))


def decide_after_apology(*, delivered: bool, refused: bool, retry: AskAgain, promised: gate.Pacing,
                         target: Target, now: datetime, stop_reason: str | None = None) -> Decision:
    """お詫びの電話のあとの次の動き（業務の結果は変えない）。伝えた・断られた ⇒ 終える。届かなかった ⇒ 別の時間帯に、決めた回数まで。"""
    if refused:
        return _end("do_not_call", "お詫びの電話で、発信を断られた")
    if stop_reason is not None:   # 番号違い・FAX：その番号へもうかけない（締めの処理が judge.refusal で決めた）
        return _end(stop_reason, "お詫びの電話：もうかけない（番号違い・FAX）")
    if delivered:
        return _end("apology_done", "お詫びを伝えた")
    if retry.tries >= retry.limit:
        return _end("apology_unreached", f"お詫びの電話：{retry.limit} 回かけても、お客様に届かなかった")
    if retry.at is None:
        return _no_opening(now, True, target)
    return _call(max(retry.at, promised.earliest or retry.at), True,
                 f"お詫びの電話：届かなかった ⇒ 別の時間帯に（{retry.tries + 1} 回目）", target)


def after_apology(tx: Tx, task_id: UUID, *, delivered: bool, refused: bool, dialed: Target,
                  stop_reason: str | None = None) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。お詫びの電話のあとの次の動きを決めて記録する（同じ取引）。"""
    now = tx.one("SELECT now() AS now")["now"]
    promised = gate.pacing(tx, task_id, contact_id=dialed.contact_id, promised=True)
    d = decide_after_apology(delivered=delivered, refused=refused,
                             retry=_new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="apology"),
                             promised=promised, target=dialed, now=now, stop_reason=stop_reason)
    _write(tx, task_id, d)
    return d


def after_denial(tx: Tx, task_id: UUID, denied: gate.Denied, *, tenant_id: UUID, client_id: UUID,
                 business_id: UUID) -> Decision | None:
    """門で止められたあとに呼ぶ（候補を出す処理が、門の答えを受けて呼ぶ）。決めた予定を記録する。"""
    now = tx.one("SELECT now() AS now")["now"]
    cur = current(tx, task_id)
    opening = None
    if denied.reason == "outside_hours":
        lifetime = timedelta(seconds=int(settings.resolve(tx, "gate.claim_lifetime", tenant_id=tenant_id,
                                                          client_id=client_id, business_id=business_id)))
        hours = gate.calling_hours_for(tx, tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    retry_wait = int(settings.resolve(tx, "gate.denied_retry_wait", tenant_id=tenant_id, client_id=client_id,
                                      business_id=business_id))
    if denied.reason == "outside_hours":
        local = tx.one("SELECT salescall.jst(now()) AS t")["t"]
        try:
            found = calling_hours.next_opening(hours, holiday_checker(tx), local, lifetime)
        except CalendarOutOfRange:   # 次の時間帯を探す途中に暦の無い日がある ⇒ 決めつけず、待ってからもう一度
            found = None
            denied = gate.Denied("calendar_out_of_range", *gate.REASONS["calendar_out_of_range"])
        opening = None if found is None else tx.one("SELECT salescall.from_jst(%s) AS t", (found,))["t"]
    now_target = None if cur is None or cur["kind"] != "call" else Target(cur["contact_id"], None, cur["purpose"],
                                                                            cur["wrong_contact_id"])
    d = decide_after_denial(denied, current_promised=bool(cur and cur["promised"]), current=now_target,
                            pacing=gate.pacing(tx, task_id), now=now, opening=opening, retry_wait=retry_wait,
                            current_callee_at=cur["callee_at"] if cur else None)
    if d is not None:
        _write(tx, task_id, d)
    return d
