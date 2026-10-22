"""次の動き（Q4。DESIGN.md 7-2・4-1）。持ち主 CL-2。

- 次の予定の正本は「次の動きの記録」の最新だけ（「かけ直す（日時）」か「終える（理由）」か「判定待ち」）。
- **判定待ち**（hold。2026-10-16 ユーザー決定）：判定できなかった（業務の状態 unknown）・判定の結果から次の動きを決められなかった
  行は、終えずに残す（かけない）。原因が直ったら、締めの処理が判定し直して、ふつうの次の動きを書く（core/settle.py の `rejudge`）。
- 次の動きの記録を足すのは、この部品だけ（I-7・I-12。DESIGN.md 2章 Q4：次の予定を書くのは、ここだけ。app/checks が見張る）。
- **何を決めるかは `decide_after_call`・`decide_after_denial` の1か所**（データベースを使わない部品。同じ入力からは同じ答え）。
  通話のあと（締めの処理 core/settle.py が判定と一緒に呼ぶ `after_call`）と、門で止められたあと（`after_denial`）の両方が使う。
  - 回数と間隔は門の数え方（`gate.pacing`）を読む（数え直さない）。再発信の方針は業務の決まり（core/business_rules.py）。
  - 決める予定は、門の最も早い時刻より前にしない（門で止まるだけの予定を作らない）。
  - 門で止められた理由が「前の発信の締めがまだ」「予定の時刻より前」なら、何も書かない（締めより先に予定を書き直さない）。
- **お客様と約束したかけ直し**（2026-10-19 ユーザー決定。表の作り 0086・0089・0093・0095）：時刻ちょうど（「明日の15時」）・
  時間の幅（「夕方」「3時まで」「金曜なら」「13日なら」など）の約束の次の動きは `_keep_promise` の1か所（時刻ちょうどの約束は、
  その時刻から1時間の中で5分後・さらに10分後・さらに20分後＝`_exact_next_try`、話せなければ次の日の同じ時刻、3日で打ち止め）。
  時刻の決まっていない約束（「あとで」・日だけの約束が過ぎたあと）は `_unspecified_next` の1か所。幅の中でかけられる最も早い
  時刻と、話せなかったときの次の回（お客様がその日を選んだ理由に合わせる：曜日・週末など＝翌週の同じ日／時間の言葉＝次の日の
  同じ幅／月ぎめの理由＝翌月）を探すのは `_window_opening` の1か所。理由の分からない日だけの約束が過ぎたら、時刻の決まっていない約束の方針（`_window_over`）。
  幅は次の予定に持たせ、門が幅の外ではかけない。ちょうどの時刻は、お客様が指定した時刻（その時刻を含むNG時間の枠でもかけて
  よい＝`calling_hours.excuse_ng`）。
- **どの番号へかけるかも、ここだけが決める**（`choose_number`。DESIGN.md 3-5）。予定（かけ直す）は必ず番号を持ち、門はその番号へ
  かける。決め方：お客様が「別の番号にかけ直して」と言った ⇒ その番号（業務の設定で「かけない」・かけない理由がある ⇒ 終える。
  番号は記録して依頼者へ渡す）／それ以外 ⇒ いまかけた番号のまま（門で止められたあとは、いまの予定の番号のまま）。
  最初の予定（取込）は一覧の番号。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import attempts, business_rules, calling_hours, gate, settings, spoken_time, tasks
from salescall.core.access import Tx
from salescall.core.business_rules import Rules
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.judge import Judgement
from salescall.core.tasks import TaskNotFound, lock


def current(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """いまの予定（いちばん新しい記録）。無ければ None（まだ何も決めていない）。"""
    return tx.one(
        "SELECT task_id, seq, kind, call_at, end_reason, cause, promised, contact_id, purpose, wrong_contact_id, callee_at,"
        "       window_until, window_daily, window_days, window_from, window_repeat, window_round, window_missed, apology_owed,"
        "       apology_reasons, decided_at"
        " FROM salescall.current_next_actions WHERE task_id = %s",   # いまの予定を選ぶのはデータベースの見方の1か所（0094）
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


@dataclass(frozen=True)
class Window:
    """お客様が言ったかけ直しの時間の幅（次の予定に持たせ、門が守る）。どれも無い幅は持たない（＝幅なし）。
    幅は「回」で数える：いまの回は [since, until)。その回にお客様と話せなかったら、repeat（お客様がその日を選んだ理由。
    `spoken_time.Reading.repeat`）で次の回へ移り、round を1つ上げる（0089。2026-10-19 ユーザー決定）。"""
    until: datetime | None               # いまの回の終わり（含まない）
    daily: tuple[int, int] | None        # 1日の中の幅（0時からの秒）。時間の言葉があるときだけ
    days: tuple[str, ...] | None = None  # かけてよい日の種類（「平日」など。曜日・祝日の鍵）
    since: datetime | None = None        # いまの回の本当の始まり（約束の日にかけられたかを見る・次の回を数える）
    repeat: str | None = None            # 話せなかったときの次の回（spoken_time.DAY_REPEAT・WEEK・"month:..."。None＝次の回は無い）
    round: int = 0                       # 何回目の回か（最初の回は0。時刻の決まっていない約束の方針の回数まで）
    missed: int = 0                      # 過ぎた回のうち、こちらが一度もかけられなかった回の数（打ち止めに数えない。0113）

    @property
    def empty(self) -> bool:
        return self.until is None and self.daily is None and self.days is None and self.repeat is None


def window_said(cb: dict[str, Any]) -> Window:
    """判定の結果のかけ直し（`judge` の detail.callback）の、お客様が言った時間の幅（1か所）。"""
    iso = lambda v: None if v is None else datetime.fromisoformat(v)   # noqa: E731
    return Window(iso(cb.get("until")), None if cb.get("daily") is None else tuple(cb["daily"]),
                  None if cb.get("days") is None else tuple(cb["days"]), iso(cb.get("since")), cb.get("repeat"))


def window_of(row: dict[str, Any] | None) -> Window | None:
    """次の予定の記録の幅（無ければ None）。"""
    if row is None:
        return None
    w = Window(row["window_until"], None if row["window_daily"] is None else tuple(row["window_daily"]),
               None if row.get("window_days") is None else tuple(row["window_days"]), row.get("window_from"),
               row.get("window_repeat"), row.get("window_round") or 0, row.get("window_missed") or 0)
    return None if w.empty else w


# お客様につながったら、まずお詫びすること（次の予定の印 apology_reasons。予約証で会話の処理へ渡す＝gate.Claim.apology。0113）
APOLOGY_REASONS = ("missed_promise", "cut_off")   # 約束した日（回）に一度もかけられなかった／前の電話が途中で切れた
# 決めたお詫びの印：None＝前の予定から引き継ぐ／False＝外す（目的のお客様と話せた）／理由1つ＝前の印に足す／理由の並び（tuple）＝
# その並びにする（目的のお客様と話せた通話が、また途中で切れた＝前の印は済み、新しい理由だけ）
Apology = bool | str | tuple[str, ...] | None


def _apology_reasons(prev: tuple[str, ...], apology: Apology) -> tuple[str, ...]:
    """次の予定のお詫びの理由（1か所）。"""
    if apology is None:
        return prev
    if apology is False:
        return ()
    if isinstance(apology, str):
        apology = tuple(dict.fromkeys(prev + (apology,)))
    if apology is True or any(r not in APOLOGY_REASONS for r in apology):
        raise ValueError(f"お詫びの理由は {APOLOGY_REASONS} のどれかです")
    return tuple(apology)


def _append(tx: Tx, task_id: UUID, *, call_at: datetime | None, end_reason: str | None, cause: str,
            call_now: bool = False, promised: bool | None = None, target: Target | None = None,
            callee_at: datetime | None = None, hold: bool = False, window: Window | None = None,
            apology: Apology = None) -> int:
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
        raise ValueError(f"発信の目的は {'・'.join(PURPOSES)} のどれか（聞き直しは、番号違いだった番号を必ず持つ）")
    if callee_at is not None and not (calls and promised):
        raise ValueError("お客様が指定した時刻を持てるのは、約束したかけ直しだけです")
    if window is not None and not calls:
        raise ValueError("時間の幅を持てるのは、かけ直す予定だけです")
    if window is not None and window.empty:
        window = None
    if window is not None and window.until is None:
        # 終わりの無い幅（「平日なら」「すぐ」）は、まだ回が決まっていない：次の回の決め方は、回を決めるとき（`_window_opening`）に
        # 次の日にする。回の無い幅は、次の回の決め方を持たない（表の作り 0089 の約束）
        window = replace(window, repeat=None, since=None)
    lock(tx, task_id)  # 同じタスクへの追記を1つずつ順にする（tasks.py の説明）
    prev = current(tx, task_id)
    reasons = _apology_reasons(tuple(prev["apology_reasons"]) if prev else (), apology)
    row = tx.one(
        "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, end_reason, cause,"
        "                                         promised, contact_id, purpose, wrong_contact_id, callee_at, window_until,"
        "                                         window_daily, window_days, window_from, window_repeat, window_round,"
        "                                         window_missed, apology_owed, apology_reasons)"
        " SELECT t.tenant_id, t.client_id, t.task_id,"
        "        coalesce((SELECT max(n.seq) FROM salescall.task_next_actions n WHERE n.task_id = t.task_id), 0) + 1,"
        "        %s, CASE WHEN %s THEN now() ELSE %s::timestamptz END, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        "        %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING seq",
        ("hold" if hold else "call" if calls else "end", call_now, call_at, end_reason, cause, promised,
         target and target.contact_id, target and target.purpose, target and target.wrong_contact_id, callee_at,
         window and window.until, None if window is None or window.daily is None else list(window.daily),
         None if window is None or window.days is None else list(window.days), window and window.since,
         window and window.repeat, 0 if window is None else window.round, 0 if window is None else window.missed,
         bool(reasons), list(reasons), task_id),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row["seq"]


def schedule_now(tx: Tx, task_id: UUID, *, cause: str) -> int:
    """いまから、一覧の番号へかけてよい（日時はデータベースの時刻。取込で登録したタスクの最初の予定）。
    実際にかけるか・いつかけるかは門が決める（時間帯・間隔・停止など）。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=False,
                   target=Target(tasks.list_number(tx, task_id)["contact_id"]))


def hold(tx: Tx, task_id: UUID, *, cause: str, apology: Apology = None) -> int:
    """判定待ちにする（かけない・終えない）。同じ取引で、その行のまだ発信していない予約を取り消す。"""
    seq = _append(tx, task_id, call_at=None, end_reason=None, cause=cause, hold=True, apology=apology)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    return seq


def end(tx: Tx, task_id: UUID, reason: str, *, cause: str, apology: Apology = None) -> int:
    """終える（理由）を記録する。同じ取引で、その行のまだ発信していない予約を取り消す（門の判定に効く変更。5-2-1 の2）。"""
    seq = _append(tx, task_id, call_at=None, end_reason=reason, cause=cause, apology=apology)
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
    window: Window | None = None         # call のとき：お客様が言ったかけ直しの時間の幅（門が守る）
    apology: Apology = None              # お詫びの印（`Apology`。None＝前の予定から引き継ぐ）

    @property
    def contact_id(self) -> UUID | None:
        return None if self.target is None else self.target.contact_id


def _call(at: datetime, promised: bool, rule: str, target: Target, callee_at: datetime | None = None,
          window: Window | None = None, apology: Apology = None) -> Decision:
    return Decision("call", at, promised, None, rule, target, callee_at, window, apology)


def _end(reason: str, rule: str) -> Decision:
    return Decision("end", None, None, reason, rule)


def _hold(rule: str) -> Decision:
    return Decision("hold", None, None, None, rule)


NO_OPENING_RECHECK = timedelta(days=1)


def _no_opening(now: datetime, promised: bool, target: Target, callee_at: datetime | None = None,
                window: Window | None = None) -> Decision:
    """これから先（31日）、かけてよい時間帯が見つからない：行を終えず、1日後にもう一度確かめる（1か所）。
    時間帯が無いのは設定のその時の形（依頼者の時間帯を空にした・NG時間が時間帯を覆う・祝日だけの時間帯など）で、
    お客様の側の理由ではない。前は行を終えていて、設定を戻しても取り返せなかった（2026-10-16 の見直し G6。
    2026-10-16 ユーザー：「何でもかんでも止めるんじゃないよ」）。門が時間帯の外で止め、次の動きがまた確かめる。"""
    return _call(now + NO_OPENING_RECHECK, promised, "これから先、かけてよい時間帯が見つからない：1日後にもう一度確かめる",
                 target, callee_at, window)


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


def choose_number(j: Judgement, *, dialed: Target, alternate: Alternate | None, recheck: Recheck | None,
                  cut_off: bool = False) -> Target:
    """次にかける先（番号と発信の目的。1か所。DESIGN.md 3-5）。dialed＝いまの通話でかけた先（番号・目的）。
    - お客様が「別の番号にかけ直して」と言った ⇒ その番号（設定でかけない・かけない理由があれば、かけない）。
    - お客様が言った番号が番号違いだった ⇒ 番号の確かめのとおり：聞き違い ⇒ 正しい番号／言われたとおり・確かめられない ⇒
      番号を聞いた通話の番号へ「番号の聞き直し」（同じ行で2度目なら、聞き直さずに終える）。
    - 聞き直しの通話で、会話に至らなかった・目的のお客様に繋がらなかった（受付が出て不在・取り次げない）・途中で切れた ⇒ 目的を
      引き継ぐ（いつかけるか・何回までかは decide_after_call：聞き直しの回数の上限まで、毎回、前と別の時間帯に。途中で切れたときは
      すぐ。2026-10-15 ユーザー：「最初の相手に…再度、かけ直し先の番号を伺う」。⛔ 2026-10-22 の見直し14：前は会話に至らなかった
      ときだけ引き継ぎ、受付が出た・切れたときは、番号を聞き直さずに用件から話すふつうの電話になった）。
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
    if dialed.purpose == "ask_number_again" and (j.state in ("open", "not_reached") or cut_off):
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
                      ask: AskAgain | None = None, forbidden: bool = False, kept: Decision | None = None,
                      resume_window: Window | None = None, resume_callee_at: datetime | None = None) -> Decision:
    """通話のあとの次の動き。normal / promised＝次にかける先へ、ふつうの予定・約束したかけ直しにした場合の門の数え方。
    times_with_key＝この行で、今回の結果と同じ再発信の方針の鍵の結果が出た回数（今回を含む）。
    target＝`choose_number` の答え（次にかける先、または終える理由）。ask＝聞き直しで出なかったときの材料（そのときだけ）。
    retry_at＝この結果の再発信の方針の間隔（n 時間おき／n 日後）が指す時刻（`_retry_at` の1か所で出す。かけてよい時間帯が
    これから先に無ければ None）。forbidden＝この通話で、言ってはいけないことを言った（通話後の検証で、2つの耳で確かめた）。
    kept＝お客様と約束したかけ直しを守る次の動き（時刻・幅の約束は `_keep_promise`、時刻の決まっていない約束は
    `_unspecified_next` の1か所が決めたもの）：お客様がかけ直しを約束した通話のあと、または約束の電話で話せなかった（出なかった・
    目的のお客様に繋がらなかった）通話のあとだけ。
    resume_window・resume_callee_at＝途中で切れた通話が守っていた、お客様が言った時間の幅・時刻（すぐかけ直す電話も、同じ約束の
    回のまま）。"""
    # 判定できない：何も決めない（終える理由も含めて）。終えずに残し、原因が直ったら判定し直す（2026-10-16 ユーザー決定）。
    # ⚠️ 先に見る：言ってはいけないこと・検証の失敗・苦情で先に終えると、判定し直す道が無くなり、断られたかが決まらないまま
    # 同じお客様のほかの行を止め続けた（2026-10-17 の見直し）。判定し直したあと、この順でもう一度決まる
    if j.state == "unknown":
        return _hold(f"判定できなかった（{j.detail.get('unknown_cause')}）：判定待ちで残す")
    if forbidden:   # 2026-10-15 ユーザー：「その行はひとまず終える。依頼主でなく、管理者へ報告。場合によっては、お詫びを入れる」
        return _end("forbidden_speech", "言ってはいけないことを言った：その行はひとまず終える（契約者と管理者へ知らせ、CL-1 へ修正依頼を出した）")
    if j.state in END_STATES:
        return _end(j.state, "目的を果たした・終わった結果")
    if j.complaint:
        return _end("complaint", "苦情があった")
    if target.contact_id is None:
        return _end(target.end_reason, "お客様が言った番号へは、かけない（番号は記録して依頼者へ渡す）")
    key = j.detail.get("policy_key")
    if j.state == "not_reached" and j.detail.get("not_reached") == "wrong_alternate":
        # お客様に頼まれたかけ直しが、まだ果たせていない（番号違い）：約束したかけ直しのまま、すぐ（門の最も早い時刻）。
        # 元の約束に時刻・幅があれば、その約束のまま（kept＝`_keep_promise`）
        if kept is not None:
            return kept
        return _call(promised.earliest or now, True, "言われた番号が番号違い：番号の確かめのとおり", target)
    # 途中で切れた（お客様が出たあと、こちらの仕組みが止まった・回線が切れた＝お客様の意思ではない）：復旧次第、お詫びとともに
    # すぐかけ直す（2026-10-17 ユーザー：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」）。目的「続き」で、会話は
    # お詫びから始めて続ける。お客様が時刻を言ったかけ直しの約束があれば、その時刻を守る（下）
    # 番号の聞き直しの電話が切れたときは、聞き直しの目的のまま（`choose_number`）。続きの電話に出なかったときも、次の電話で
    # お詫びから始める（印 cut_off。外すのは目的のお客様と話せた締めだけ。⛔ 2026-10-22 の見直し14）
    if cut_off and target.purpose in ("normal", "resume", "ask_number_again") and not (
            j.state == "callback_scheduled" and j.detail["callback"]["at"] is not None):
        again = target if target.purpose == "ask_number_again" else Target(target.contact_id, purpose="resume")
        return _call(promised.earliest or now, True, "途中で切れた：復旧次第、お詫びとともにすぐかけ直す",
                     again, resume_callee_at if resume_window else None, resume_window, "cut_off")
    if j.state == "callback_scheduled":
        # お客様と約束したかけ直し：次の動きは、時刻・幅を言った約束は `_keep_promise`、時刻の決まっていない約束は
        # `_unspecified_next` の1か所が決めたもの
        if kept is None:
            raise ValueError("お客様と約束したかけ直しなのに、約束を守る次の動き（kept）がありません")
        return kept
    if kept is not None and j.state in ("open", "not_reached"):
        # 約束の電話で話せなかった：約束のまま（回数・1日の回数・最小の間隔の上限の外。同じ回の中か、次の回へ。2026-10-19
        # ユーザー決定。決めるのは `_keep_promise` の1か所）
        return kept
    if target.purpose == "ask_number_again":   # 聞き直しの電話に出なかった・目的のお客様に繋がらなかった
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
                        current_callee_at: datetime | None = None, current_window: Window | None = None) -> Decision | None:
    """門で止められたあとの次の動き。何も書かないときは None。かける先（番号・目的）と、お客様が指定した時刻と、お客様が言った
    時間の幅は、いまの予定のまま（幅の約束が時間帯の外・幅の外で止められたときは、この関数ではなく `_keep_promise` が決める
    ＝`after_denial`。幅の外 callback_window で止められるのは幅のある予定だけなので、ここには来ない）。"""
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
            return _no_opening(now, current_promised, current, current_callee_at, current_window)
        return _call(max(opening, pacing.earliest or opening), current_promised, "門：時間帯の外 ⇒ 次の時間帯の始まり",
                     current, current_callee_at, current_window)
    return _call(max(now + timedelta(seconds=retry_wait), pacing.earliest or now), current_promised,
                 f"門：待てば変わる理由（{d.reason}）", current, current_callee_at, current_window)


def _write(tx: Tx, task_id: UUID, d: Decision) -> int:
    if d.kind == "end":
        return end(tx, task_id, d.end_reason, cause=d.rule, apology=d.apology)
    if d.kind == "hold":
        return hold(tx, task_id, cause=d.rule, apology=d.apology)
    return _append(tx, task_id, call_at=d.at, end_reason=None, cause=d.rule, promised=d.promised, target=d.target,
                   callee_at=d.callee_at, window=d.window, apology=d.apology)


# 途中で切れた通話のあとの、お詫びしてすぐかけ直す電話（目的 resume）は、同じ行で続けて2回まで。それでも切れたら、ふつうの
# 再発信の方針（間隔をあけてかけ直す）へ移す（2026-10-18 ユーザー 案A。電波の悪いお客様に、お詫びの電話をきりなくかけ続けないため）
RESUME_IN_A_ROW = 2


def after_call(tx: Tx, task_id: UUID, j: Judgement, *, rules: Rules, dialed: Target,
               dialed_at: datetime, alternate: Alternate | None, recheck: Recheck | None, forbidden: bool,
               cut_off: bool = False, reached_target: bool = False) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。判定の結果から次の動きを決めて記録する（同じ取引）。
    cut_off＝お客様が出たあと、お客様の意思と関係なく途中で切れた（決めるのは締めの処理の1か所）。
    dialed＝この通話でかけた先（番号・目的）、dialed_at＝かけた時刻。alternate＝お客様が言った別の番号の材料（言われたときだけ）。
    recheck＝言われた番号が番号違いだったときの番号の確かめ（そのときだけ）。reached_target＝目的のお客様と話せた（約束の日に
    かけられなかったお詫びは、ここで済んだものとして外す＝会話の処理が、予約証の印を見て最初にお詫びしている）。"""
    now = tx.one("SELECT now() AS now")["now"]
    # かけた予定（発信のときに効いていた次の予定）が持っていた、お客様が言った時間の幅。いまの予定ではなく、発信の時刻までに
    # 決めた予定から読む（判定し直しのときは、いまの予定が判定待ちになっていて、幅を失うため）
    dialed_row = _plan_at(tx, task_id, dialed_at)
    dialed_window = window_of(dialed_row)
    dialed_exact = dialed_window is not None and dialed_row["callee_at"] is not None   # 時刻ちょうどの約束の回
    key = j.detail.get("policy_key")
    target = choose_number(j, dialed=dialed, alternate=alternate, recheck=recheck, cut_off=cut_off)
    count_at = target.contact_id or dialed.contact_id     # 終えるときは数え方を使わない（いまの番号で数えておく）
    promised = gate.pacing(tx, task_id, contact_id=count_at, promised=True)
    normal = gate.pacing(tx, task_id, contact_id=count_at, promised=False)
    times = 0 if key is None else _times(tx, task_id, key)
    ask = None
    if target.purpose == "ask_number_again" and not cut_off:
        ask = _new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="ask_number_again")
    policy = rules.retry.get(key) if key is not None else None
    retry_at = _retry_at(tx, task_id, policy.interval, now, dialed_at) if policy and policy.action == "call" else None
    # 事情があり取り次げず、あとでまたかけてよいとは言われなかった（約束の電話で、当日はかけ直さない＝下の2か所。2026-10-22
    # ユーザー：「OKなら、当日の約束の時刻の幅の中かけ直してもいいと思うけど、そうでなければ、当日の幅はやめたほうがいい」）
    not_transferred = j.state == "not_reached" and j.detail.get("not_reached") == "not_transferred"
    not_today = not_transferred and not j.detail.get("again")
    # お客様が言った時刻・時間の幅の約束（「明日の15時」「夕方」「金曜なら」「13日なら」など）を守る次の動き（`_keep_promise` の1か所）
    kept = None
    cb = j.detail.get("callback") if j.state == "callback_scheduled" else None
    if target.contact_id is not None and cb is not None:
        if cb["at"] is None:
            kept = _unspecified_next(tx, task_id, target, now=now, base=now, tried=dialed_at)
        elif cb.get("exact", True):
            said = datetime.fromisoformat(cb["at"])
            kept = _keep_promise(tx, task_id, said, _exact_window(tx, task_id, said), target, now=now, exact=True)
        else:
            kept = _keep_promise(tx, task_id, max(now, datetime.fromisoformat(cb["at"])), window_said(cb), target, now=now)
    elif target.contact_id is not None and j.state == "not_reached" and j.detail.get("not_reached") == "wrong_alternate":
        # お客様に頼まれた番号が番号違いだった：確かめた番号へは、元の約束の幅・時刻のまま、すぐ（幅の外なら次の回へ）。
        # 番号を聞き直す電話（ask_number_again）は約束の電話ではない（すぐ。そのあとは別の時間帯に決めた回数まで＝`_new_band_retry`）
        if target.purpose == "normal" and dialed_window is not None:
            kept = _keep_promise(tx, task_id, now, dialed_window, target, now=now, exact=dialed_exact)
    elif target.contact_id is not None and dialed_window is not None and j.state in ("open", "not_reached"):
        # 約束の電話で話せなかった：同じ回の中でもう一度か、次の回か。時刻ちょうどの約束は、前の電話から決めた間をあけて
        # （`_exact_next_try`）。幅の約束は、この結果の再発信の方針の間隔で、その回にかけた数が方針の回数の中で、回の終わりより
        # 前だけ（行全体の回数ではなく、その回にかけた数で決める＝時刻ちょうどの約束と同じ形。⛔ 2026-10-22 の見直し14：前は、
        # 約束の回に出なかった分まで数え、別の結果の回数と混ぜていた）
        if not_today:   # 幅が当日を越えて続くときも、当日の分はやめる
            after = min(dialed_window.until or _next_day(now), _next_day(now))
        elif dialed_exact:
            after = _exact_next_try(tx, task_id, dialed_window, now)
        else:
            # 取り次げなかったが、あとでまたかけてよいと言われた ⇒ 目的のお客様がいま出られない＝「不在」の方針の間隔で（「取り次げ
            # なかった」の方針＝日を改めて1回は、約束の電話には使わない＝約束の時刻が先）
            p = rules.retry["not_reached_absent"] if not_transferred else policy
            at = _retry_at(tx, task_id, p.interval, now, dialed_at) if p.action == "call" else None
            within = (at is not None and _round_calls(tx, task_id, dialed_window, times) <= p.max_times
                      and (dialed_window.until is None or at < dialed_window.until))
            after = at if within else (dialed_window.until or now)
        kept = _keep_promise(tx, task_id, after, dialed_window, target, now=now, exact=dialed_exact)
    elif (target.contact_id is not None and j.state in ("open", "not_reached")
          and _served_unspecified(tx, task_id, dialed_at, dialed.purpose)):
        # 時刻の決まっていない約束の電話（「あとで」・日だけの約束が過ぎたあと・その途中で切れたあとの続き など）で話せなかった：
        # 約束のまま（回数の上限の外）。取り次げず、またかけてよいと言われなければ、当日はかけない（次の日から数える）
        base = _next_day(now) if not_today else now
        kept = _unspecified_next(tx, task_id, target, now=now, base=base, tried=dialed_at)
    if cut_off:   # 続けて何回、途中で切れたか（今回を含む）。すぐのかけ直しは続けて2回まで
        cut_off = _cut_in_a_row(tx, task_id) <= RESUME_IN_A_ROW
    d = decide_after_call(j, cut_off=cut_off, normal=normal,
                          promised=promised, rules=rules, times_with_key=times, now=now, target=target,
                          retry_at=retry_at, ask=ask, forbidden=forbidden, kept=kept, resume_window=dialed_window,
                          resume_callee_at=dialed_row["callee_at"] if dialed_row else None)
    d = _apologised(d, reached_target)
    _write(tx, task_id, d)
    return d


def _cut_in_a_row(tx: Tx, task_id: UUID) -> int:
    """この行で、いちばん新しい発信から続けて、途中で切れた発信の数（1か所。途中で切れたかは、締めの処理が judge.cut_off で決めて
    試行に残したもの。ふつうの電話・続き・お詫びの電話・番号の聞き直しで同じ数え方）。"""
    return tx.one(
        "SELECT coalesce(min(n) FILTER (WHERE NOT coalesce(cut_off, false)) - 1, count(*)) AS n FROM ("
        "  SELECT cut_off, row_number() OVER (ORDER BY claimed_at DESC) AS n FROM salescall.attempts"
        f"  WHERE task_id = %s AND {attempts.DIALED}) x", (task_id,))["n"]


def _round_calls(tx: Tx, task_id: UUID, window: Window, fallback: int) -> int:
    """約束の回 [since, until) に、この行でかけた電話の数（鳴った・鳴りえた電話。今回を含む。1か所）。回の始まりの分からない幅は
    fallback（その結果の方針の回数）。"""
    if window.since is None:
        return fallback
    return tx.one(f"SELECT count(*) AS n FROM salescall.attempts WHERE task_id = %s AND {attempts.placed()}"
                  " AND claimed_at >= %s AND (%s::timestamptz IS NULL OR claimed_at < %s)",
                  (task_id, window.since, window.until, window.until))["n"]


def _apologised(d: Decision, reached_target: bool) -> Decision:
    """目的のお客様と話せた通話のあと：前の印のお詫び（約束の日にかけられなかった・途中で切れた）は済んだ（会話の処理が、予約証の
    印を見て最初にお詫びしている）。印を外すのは、ここの1か所（ふつうの通話・お詫びの電話の締めの両方）。この通話で新しく付けた
    理由（また途中で切れた など）だけは残す。"""
    if not reached_target:
        return d
    if d.apology is None or d.apology is False:
        return replace(d, apology=False)
    return replace(d, apology=(d.apology,) if isinstance(d.apology, str) else d.apology)


def _times(tx: Tx, task_id: UUID, key: str) -> int:
    """この行で、その再発信の方針の鍵の結果が出た通話のうち、行の回数に数える発信の数（1か所。数える発信の決まりは門と同じ
    `attempts.counts_as_try`＝お客様の意思と関係なく繋がらなかった・切れた発信と、お客様と約束したかけ直しは数えない。通話ごとに
    最新の判定の版＝見方 call_judgements で数える＝判定し直しを二重に数えない）。⛔ 2026-10-22 の見直し14：前は、その結果の出た
    通話をすべて数え、回線の側の失敗・切れた通話・約束の電話で、お客様に一度も繋がらないまま行が終わった。"""
    return tx.one(
        "SELECT count(*) AS n FROM salescall.call_judgements j JOIN salescall.attempts a ON a.attempt_id = j.attempt_id"
        f" WHERE j.task_id = %s AND j.detail ->> 'policy_key' = %s AND {attempts.counts_as_try('a')}",
        (task_id, key))["n"]


def _said(tx: Tx, task_id: UUID, key: str) -> int:
    """この行で、その再発信の方針の鍵の結果が出た通話の数（お客様が言った回数。数える発信かに関係なく。1か所）。"""
    return tx.one(
        "SELECT count(*) AS n FROM salescall.call_judgements WHERE task_id = %s AND detail ->> 'policy_key' = %s",
        (task_id, key))["n"]


def _retry_at(tx: Tx, task_id: UUID, interval: tuple[str, int], now: datetime, dialed_at: datetime) -> datetime | None:
    """再発信の方針の間隔が指す時刻（1か所。2026-10-15 ユーザー：「2時間おき・3時間おき・○時間おき・○日後」を業務ごとに）。
    - n 時間おき ⇒ いまから n 時間後（時間帯の外・NG時間に当たれば、門が止め、次の動きが次の始まりへ回す）。
    - n 日後 ⇒ n 日後の日の、前回と別の時間帯で、かけてよい最も早い時刻（「時間帯により出られないことはよくある」）。
      その日に別の時間帯が無ければ、その日以降で、かけてよい最も早い時刻。これから先に無ければ None。"""
    unit, n = interval
    if unit == "hours":
        return now + timedelta(hours=n)
    scope = tasks.scope(tx, task_id)
    get = lambda key: settings.resolve(tx, key, **scope)   # noqa: E731
    boundaries = list(get("retry.time_band_boundaries"))
    hours, length = gate.calling_hours_for(tx, **scope), timedelta(seconds=int(get("gate.claim_lifetime")))
    day = datetime.combine(calling_hours.local(now).date() + timedelta(days=n), datetime.min.time())
    try:
        found = (calling_hours.next_opening_in_new_band(hours, holiday_checker(tx), day, length, boundaries,
                                                        {calling_hours.band_of(calling_hours.local(dialed_at), boundaries)}, horizon_days=0)
                 or calling_hours.next_opening(hours, holiday_checker(tx), day, length))
    except CalendarOutOfRange:   # 暦の無い日に当たる：決めつけず、その日の始まりにして、門にもう一度確かめさせる
        found = day
    return None if found is None else calling_hours.from_local(found)


def _window_opening(tx: Tx, task_id: UUID, after: datetime, window: Window, *,
                    now: datetime, exact: bool = False) -> tuple[datetime | None, Window | None]:
    """お客様が言った時間の幅の中で、after（いまより前なら、いま）以降にかけられる最も早い時刻と、そのときの幅（1か所。DESIGN.md
    7-2）。かけてよい時間帯から NG時間を引いた残りを、1日の中の幅・日の種類に絞って探す（幅はお客様が時刻を指定したものでは
    ないので、NG時間は守る）。いまの回 [since, until) の中に無ければ、お客様がその日を選んだ理由（window.repeat）で次の回へ移る。
    回の数（round）は、過ぎた回をすべて数える（話せないまま過ぎた回。かけられる時刻の無い回＝店休日などは数えない）。
    終わりの無い幅（「平日なら」「すぐ」）は、かけられる日の1日を、いまの回にする（話せなければ、次の日の同じ幅）。
    答え：(時刻, その回の幅)／(None, 幅)＝これから先、かけてよい時間帯そのものが無い（設定のその時の形。幅を保ち、確かめ直す）／
    (None, None)＝約束の回がもう無い（理由の分からない日だけの約束が過ぎた・お客様の幅が、かけてよい時間帯と重ならない）。
    exact＝時刻ちょうどの約束の回（お客様が指定した時刻を含むNG時間の枠は引かずに探す＝門の callee_at と同じ決まり
    `calling_hours.excuse_ng`。ほかのNG時間の枠は控える）。"""
    scope = tasks.scope(tx, task_id)
    all_hours = gate.calling_hours_for(tx, **scope, callee_of_day=window.daily[0] if exact else None)
    hours = calling_hours.restrict(all_hours, window.daily, window.days)
    length = timedelta(seconds=int(settings.resolve(tx, "gate.claim_lifetime", **scope)))
    holiday = holiday_checker(tx)
    to_jst = lambda t: None if t is None else calling_hours.local(t)   # noqa: E731
    from_jst = calling_hours.from_local
    # 回の始まりより前は探さない（⛔ 2026-10-22 の見直し14：前は、いまが回の始まりより前だと、始まりより前の時刻を返し、「週末なら」
    # の約束を金曜にかけえた。門も回の始まりを見る＝calling_hours.in_window）
    local, until, since = to_jst(max(after, now)), to_jst(window.until), to_jst(window.since)
    if since is not None and local < since:
        local = since

    def none_left(at: datetime) -> tuple[None, Window | None]:   # 幅の中に無い：時間帯そのものが無いのか、幅が重ならないのか
        return None, (window if calling_hours.next_opening(all_hours, holiday, at, length) is None else None)

    if not _can_overlap(hours, window):
        # お客様の幅が、かけてよい時間帯と1日も重ならない（例：「週末なら」と言われたが、依頼者の時間帯に土日が無い）：回を何年も
        # 探さずに、重ならない道へ（暦を先まで読まない。⛔ 2026-10-22 の見直し14：前は回を400回先まで探して暦の外に当たり、5分
        # ごとに同じ予定を書き続け、行が終わらなかった）。⏳ お客様が依頼者の時間帯の外を指定したときにどうするかは、管理者に
        # 決めていただく点（いまは、時刻の決まっていない約束にする＝`_window_over`）
        try:
            return none_left(local)
        except CalendarOutOfRange:
            return None, window
    try:
        upcoming = calling_hours.next_opening(hours, holiday, local, length)
        if upcoming is None:
            return none_left(local)
        if until is None:
            day = datetime.combine(upcoming.date(), datetime.min.time())
            return from_jst(upcoming), replace(window, until=from_jst(day + timedelta(days=1)), since=from_jst(day),
                                               repeat=window.repeat or spoken_time.DAY_REPEAT)
        if upcoming < until:
            return from_jst(upcoming), window
        if window.repeat is None:
            return None, None   # 理由の分からない日だけの約束が過ぎた（時刻の決まっていない約束へ）
    except CalendarOutOfRange:   # 暦の無い日に当たる（暦をまだ取っていない）：決めつけず、幅を保って1日後に確かめ直す
        return None, window
    start, end, rounds = since or (until - timedelta(days=1)), until, window.round
    for _ in range(_MAX_ROUNDS_SEARCHED):
        start, end = (calling_hours.local(t) for t in spoken_time.next_occurrence(
            window.repeat, from_jst(start), from_jst(end)))
        try:
            first = calling_hours.next_opening(hours, holiday, start, length)
        except CalendarOutOfRange:
            # 暦のある範囲（1年以上先まで）を探しても、かけられる回が無い ⇒ これから先に無いとみなす。暦がまだ近くまでしか
            # 無い（年の替わり目で翌年の暦をまだ取っていない など）⇒ 幅を保って、1日後に確かめ直す
            return (None, None) if start - local >= _CALENDAR_SEARCH_ENOUGH else (None, window)
        if first is None:
            return none_left(start)
        if first >= end:
            continue   # かけられる時刻の無い回（店休日など）：数えない
        rounds += 1
        found = first if first >= local else upcoming
        if found < end:
            return from_jst(found), replace(window, until=from_jst(end), since=from_jst(start), round=rounds)
        # 話せないまま過ぎた回（見守り役が止まっていたなど）：数えて、次の回へ（打ち止めには数えない＝`_keep_promise`）
    return None, None


def _can_overlap(hours: dict, window: Window) -> bool:
    """お客様の幅（回）が、かけてよい時間帯（1日の幅・日の種類に絞ったもの）と重なりうるか（暦を読まずに決める。1か所）。
    回が曜日で決まる幅（週ごとの回）は、その回の曜日と祝日だけ。ほかの幅（日ごと・月ごと・終わりの無い幅）は、どの日も。"""
    keys = set(calling_hours.DAY_KEYS) | {"holiday"}
    if window.repeat == spoken_time.WEEK and window.since is not None and window.until is not None:
        first = calling_hours.local(window.since).date()
        last = (calling_hours.local(window.until) - timedelta(microseconds=1)).date()
        days = min((last - first).days + 1, 7)
        keys = {calling_hours.DAY_KEYS[(first + timedelta(days=k)).weekday()] for k in range(days)} | {"holiday"}
    return any(hours.get(k) for k in keys)


_CALENDAR_SEARCH_ENOUGH = timedelta(days=366)   # 暦のある範囲で、これだけ先まで回を探して無ければ、これから先に無いとみなす
_MAX_ROUNDS_SEARCHED = 400   # 次の回を探す数の上限（かけられる回が見つからないまま、これだけ進んだら、これから先に無いとみなす）


def _keep_promise(tx: Tx, task_id: UUID, after: datetime, window: Window, target: Target, *, now: datetime,
                  exact: bool = False) -> Decision:
    """お客様が言った時刻・時間の幅の約束を守る次の動き（1か所。通話のあと・門で止められたあとの両方。2026-10-19 ユーザー決定）。
    (1) after 以降で、いまの回の中・次の回の中の、かけられる最も早い時刻へ（約束したかけ直しのまま＝回数・1日の回数・最小の間隔の
        上限の外。候補を出す処理は、ふつうの電話より先に出す）。
    (2) こちらがかけた回の数（過ぎた回のうち、一度もかけられなかった回＝window.missed は数えない）が上限を超えたら終える：幅の
        約束は時刻の決まっていない約束の方針の回数（ユーザー「OK」）。時刻ちょうどの約束（exact）は設定 callback.exact_days の日数
        （ユーザー：「翌日の15時、翌々日の15時で打ち止め」）。⛔ 2026-10-22 の見直し14：前は、こちらが止まっていた間に過ぎた回も
        数え、約束したお客様に一度もかけずに打ち切った（2026-10-19 ユーザー：「約束を入れた電話は優先枠」）。
    (3) 時刻ちょうどの約束は、回ごとにその日の約束の時刻にかけ（お客様が指定した時刻＝NG時間の枠でもかけてよい。callee_at）、
        話せなければ、その日の時間（callback.exact_slot）の中で、前の電話から決めた間をあけて（`_exact_next_try`。ユーザー：
        「15時から16時に数回かける」「5分後→さらに10分後→さらに20分後」）。
    (4) 次の回が無い（理由の分からない日だけの約束が過ぎた・幅がかけてよい時間帯と重ならない）⇒ 日が過ぎてから、時刻の決まって
        いない約束の方針の間隔で（`_window_over`）。かけてよい時間帯そのものが無い ⇒ 幅を保って1日後に確かめ直す（`_no_opening`）。
    (5) 過ぎた回に、一度もかけられなかった ⇒ 次にお客様につながったときに、お詫びから始める（印 apology）。"""
    at, w2 = _window_opening(tx, task_id, after, window, now=now, exact=exact)
    moved = w2 is None or w2.round > window.round
    skipped = 0 if w2 is None else max(0, w2.round - window.round - 1)   # 間の回が、かけないまま過ぎた
    missed_now = moved and _missed(tx, task_id, window)                  # いまの回に、一度もかけられなかった
    apology = "missed_promise" if moved and (skipped or missed_now) else None
    if at is None and w2 is None:
        return _window_over(tx, task_id, now, target, window, apology=apology)
    if w2.round > window.round:
        w2 = replace(w2, missed=window.missed + int(missed_now) + skipped)
        called = w2.round - w2.missed                                    # こちらがかけた回の数
        if exact:
            days = int(settings.resolve(tx, "callback.exact_days", **tasks.scope(tx, task_id)))
            if called >= days:
                return replace(_end("callback_exact_unreached",
                                    f"時刻ちょうどの約束：{days} 日、その時刻にかけて話せなかった（打ち止め）"), apology=apology)
        else:
            p = _unspecified_policy(tx, task_id)
            if p.action == "end" or called > p.max_times:
                return replace(_end("callback_unspecified_time",
                                    f"お客様が選んだ日（回）に {called} 回かけて話せなかった：時刻の決まっていない約束の方針の回数を"
                                    "超えた"), apology=apology)
    callee = _slot_of(w2)[0] if exact else None   # その回の約束の時刻
    if at is None:
        return replace(_no_opening(now, True, target, callee, w2), apology=apology)
    if callee is not None and callee >= max(after, now):
        at = callee                                # 回の初めは、約束の時刻ちょうどに（NG時間の枠でもかけてよい）
    earliest = gate.pacing(tx, task_id, contact_id=target.contact_id, promised=True).earliest
    if w2.round == window.round:
        rule = "お客様と約束したかけ直し（お客様が言った時刻・時間の幅の中）"
    elif exact:
        rule = f"お客様と約束したかけ直し（時刻ちょうどの約束に話せなかった：次の日の同じ時刻＝{w2.round + 1} 日目）"
    else:
        rule = f"お客様と約束したかけ直し（話せなかった：お客様が選んだ日の理由に合わせて次の回＝{w2.round} 回目）"
    return _call(max(at, earliest or at), True, rule, target, callee, w2, apology)


def _next_day(now: datetime) -> datetime:
    """日本時間の次の日の0時（当日の終わり。1か所）。"""
    local = now.astimezone(calling_hours.JST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _exact_window(tx: Tx, task_id: UUID, at: datetime) -> Window:
    """時刻ちょうどの約束（「明日の15時」）の最初の回（1か所）：約束の時刻から、設定 callback.exact_slot の長さ（その日の中まで）。
    話せなければ、次の日の同じ時刻の回へ（repeat＝次の日）。"""
    slot = int(settings.resolve(tx, "callback.exact_slot", **tasks.scope(tx, task_id)))
    local = at.astimezone(calling_hours.JST)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = int((local - midnight).total_seconds())
    end = min(start + slot, 24 * 3600)
    return Window(midnight + timedelta(seconds=end), (start, end), None, at, spoken_time.DAY_REPEAT)


def _slot_of(window: Window) -> tuple[datetime, datetime]:
    """時刻ちょうどの約束の、その回の時間（約束の時刻から callback.exact_slot。1か所）。"""
    day = window.since.astimezone(calling_hours.JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(seconds=window.daily[0]), day + timedelta(seconds=window.daily[1])


def _exact_next_try(tx: Tx, task_id: UUID, window: Window, now: datetime) -> datetime:
    """時刻ちょうどの約束の電話で話せなかった：その回の時間の中の、次にかける時刻（1か所）。前の電話から、設定
    callback.exact_retry_gaps の間を順にあける（初め：5分後・さらに10分後・さらに20分後＝15時・15時05分・15時15分・15時35分。
    2026-10-19 ユーザー）。本数は、その回にかけた電話のうち、お客様のところで鳴った（鳴りえた）電話で数え（回線の側でかけられ
    なかった電話は数えない）、それが約束の時刻の1回＋並びの数に届いた・時間が過ぎた ⇒ 回の終わり（次の回へ）。間は、発信まで
    進んだ電話すべての、いちばん新しいものから数える（⛔ 2026-10-22 の見直し14：前は鳴った電話だけから数え、回線の側で
    かけられなかった電話のあとは、すぐかけ直し続けた）。"""
    start, end = _slot_of(window)
    gaps = [int(g) for g in settings.resolve(tx, "callback.exact_retry_gaps", **tasks.scope(tx, task_id))]
    dialed = tx.all(f"SELECT claimed_at, {attempts.placed()} AS rang FROM salescall.attempts WHERE task_id = %s AND"
                    f" {attempts.DIALED} AND claimed_at >= %s AND claimed_at < %s ORDER BY claimed_at",
                    (task_id, start, end))
    rang = sum(1 for d in dialed if d["rang"])
    if rang > len(gaps):
        return window.until
    nxt = start if not dialed else dialed[-1]["claimed_at"] + timedelta(seconds=gaps[max(rang - 1, 0)])
    nxt = max(nxt, now)
    return window.until if nxt >= end else nxt


def _missed(tx: Tx, task_id: UUID, window: Window) -> bool:
    """その回（[since, until)）に、一度もかけられなかったか：その回の予定を初めて決めたあと（約束をした通話そのものは数えない。
    まだその回の予定が無い＝約束をした通話の締めでは、回の始まりから：その回の中でお客様と話したので、お詫びは要らない）、
    発信まで進んだ電話（回線の側でかけられなかったものを除く）が1本も無い。回の始まりの分からない幅は数えない（False）。"""
    if window.since is None or window.until is None:
        return False
    return tx.one(
        f"SELECT NOT EXISTS (SELECT 1 FROM salescall.attempts WHERE task_id = %s AND {attempts.placed()}"
        "   AND claimed_at < %s AND claimed_at >= greatest(%s::timestamptz, (SELECT min(decided_at)"
        "     FROM salescall.task_next_actions WHERE task_id = %s AND window_from = %s AND window_until = %s))) AS m",
        (task_id, window.until, window.since, task_id, window.since, window.until))["m"]


def _unspecified_policy(tx: Tx, task_id: UUID) -> business_rules.Policy:
    version = tx.one("SELECT salescall.run_current_version(t.run_id) AS v FROM salescall.tasks t WHERE t.task_id = %s",
                     (task_id,))["v"]
    return business_rules.load(tx, version).retry["callback_unspecified_time"]


_BAND_RETRY_LIMITS = {"ask_number_again": "retry.ask_number_again_max", "apology": "retry.apology_max"}


def _new_band_retry(tx: Tx, task_id: UUID, after: datetime, *, purpose: str) -> AskAgain:
    """別の時間帯にかけ直す電話（番号の聞き直し・お詫び）で出なかったときの材料：その目的でかけた回数と試した時間帯
    （門の数え方）・まだ試していない時間帯の次にかけられる時刻。"""
    scope = tasks.scope(tx, task_id)
    get = lambda key: settings.resolve(tx, key, **scope)   # noqa: E731
    boundaries = list(get("retry.time_band_boundaries"))
    dials = gate.purpose_dials(tx, task_id, purpose=purpose)
    # 回数：発信まで進んだもの全部（回線の側で繋がらなかったものも数える。数えないと、繋がらない番号へ上限なくかけ続ける＝
    # 2026-10-16 の見直しで見つけた穴。ふつうの再発信も、再発信の方針の回数は結果ごとに全部数える）。
    # 試した時間帯：お客様が出られた発信だけ（回線の都合で繋がらなかった時間帯は、まだ試していない）。
    # 最小の間隔：お客様と約束したかけ直しは門の最小の間隔の対象外なので、この電話は、ここで前の発信から間を空ける。
    local = calling_hours.local(after)
    if dials:
        local = max(local, max(d.ended for d in dials) + timedelta(seconds=int(get("gate.same_contact_min_interval"))))
    hours, length = gate.calling_hours_for(tx, **scope), timedelta(seconds=int(get("gate.claim_lifetime")))
    try:
        found = calling_hours.next_opening_in_new_band(hours, holiday_checker(tx), local, length, boundaries,
                                                       {calling_hours.band_of(d.at, boundaries) for d in dials if d.counted})
        if found is None:   # 試していない時間帯が無い（夜だけ開くお店など）：直後にかけ直さず、翌日以降の同じ頃から
            found = calling_hours.next_opening(hours, holiday_checker(tx), local + timedelta(days=1), length)
        at = None if found is None else calling_hours.from_local(found)
    except CalendarOutOfRange:   # 暦の無い日に当たる：決めつけず、待ってから門にもう一度確かめさせる
        at = after + timedelta(seconds=int(get("gate.denied_retry_wait")))
    return AskAgain(len(dials), int(get(_BAND_RETRY_LIMITS[purpose])), at)


def schedule_apology(tx: Tx, task_id: UUID, contact_id: UUID, *, cause: str) -> int:
    """お詫びの電話の予定を書く（core/apology.py の `order` だけが呼ぶ）。いまから、その番号へ。お客様と約束したかけ直しと同じく、
    回数の上限・1日の回数・最小の間隔では止めない（お詫びは営業の電話ではない）。時間帯・NG時間・発信禁止は門が止める。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=True,
                   target=Target(contact_id, purpose="apology"))


def decide_after_apology(*, delivered: bool, refused: bool, retry: AskAgain, promised: gate.Pacing,
                         target: Target, now: datetime, stop_reason: str | None = None, cut_off: bool = False) -> Decision:
    """お詫びの電話のあとの次の動き（業務の結果は変えない）。伝えた・断られた ⇒ 終える。届かなかった ⇒ 別の時間帯に、決めた回数まで。
    途中で切れた（cut_off。続けて2回まで＝ふつうの電話と同じ決まり）⇒ 決めた回数の中で、復旧次第すぐかけ直す（2026-10-17 ユーザー：
    「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」。⛔ 2026-10-22 の見直し14：前はお詫びの電話だけ、別の時間帯に
    回していた）。"""
    if refused:
        return _end("do_not_call", "お詫びの電話で、発信を断られた")
    if stop_reason is not None:   # 番号違い・FAX：その番号へもうかけない（締めの処理が judge.refusal で決めた）
        return _end(stop_reason, "お詫びの電話：もうかけない（番号違い・FAX）")
    if delivered:
        return _end("apology_done", "お詫びを伝えた")
    if retry.tries >= retry.limit:
        return _end("apology_unreached", f"お詫びの電話：{retry.limit} 回かけても、お客様に届かなかった")
    if cut_off:
        return _call(promised.earliest or now, True, "お詫びの電話が途中で切れた：復旧次第、すぐかけ直す", target)
    if retry.at is None:
        return _no_opening(now, True, target)
    return _call(max(retry.at, promised.earliest or retry.at), True,
                 f"お詫びの電話：届かなかった ⇒ 別の時間帯に（{retry.tries + 1} 回目）", target)


def after_apology(tx: Tx, task_id: UUID, *, delivered: bool, refused: bool, dialed: Target,
                  stop_reason: str | None = None, reached_target: bool = False, cut_off: bool = False) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。お詫びの電話のあとの次の動きを決めて記録する（同じ取引）。
    reached_target＝目的のお客様と話せた（約束の日にかけられなかったお詫びの印を外す＝`_apologised`）。cut_off＝途中で切れた
    （judge.cut_off の1か所。締めの処理が試行に残したもの）。"""
    now = tx.one("SELECT now() AS now")["now"]
    promised = gate.pacing(tx, task_id, contact_id=dialed.contact_id, promised=True)
    d = decide_after_apology(delivered=delivered, refused=refused,
                             retry=_new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="apology"),
                             promised=promised, target=dialed, now=now, stop_reason=stop_reason,
                             cut_off=cut_off and _cut_in_a_row(tx, task_id) <= RESUME_IN_A_ROW)
    d = _apologised(d, reached_target)
    _write(tx, task_id, d)
    return d


def after_denial(tx: Tx, task_id: UUID, denied: gate.Denied, *, tenant_id: UUID, client_id: UUID,
                 business_id: UUID) -> Decision | None:
    """門で止められたあとに呼ぶ（候補を出す処理が、門の答えを受けて呼ぶ）。決めた予定を記録する。"""
    now = tx.one("SELECT now() AS now")["now"]
    cur = current(tx, task_id)
    window = window_of(cur)
    opening = None
    retry_wait = int(settings.resolve(tx, "gate.denied_retry_wait", tenant_id=tenant_id, client_id=client_id,
                                      business_id=business_id))
    now_target = None if cur is None or cur["kind"] != "call" else Target(cur["contact_id"], None, cur["purpose"],
                                                                            cur["wrong_contact_id"])
    if denied.reason in ("outside_hours", "callback_window") and window is not None and now_target is not None:
        # お客様が言った時間の幅の約束：幅の中の次の始まり・話せなかった回のあとは理由に合わせた次の回（`_keep_promise` の1か所）
        d = _keep_promise(tx, task_id, now, window, now_target, now=now, exact=cur["callee_at"] is not None)
        _write(tx, task_id, d)
        return d
    if denied.reason == "outside_hours":
        lifetime = timedelta(seconds=int(settings.resolve(tx, "gate.claim_lifetime", tenant_id=tenant_id,
                                                          client_id=client_id, business_id=business_id)))
        hours = gate.calling_hours_for(tx, tenant_id=tenant_id, client_id=client_id, business_id=business_id)
        local = calling_hours.local(now)
        try:
            found = calling_hours.next_opening(hours, holiday_checker(tx), local, lifetime)
        except CalendarOutOfRange:   # 次の時間帯を探す途中に暦の無い日がある ⇒ 決めつけず、待ってからもう一度
            found = None
            denied = gate.Denied("calendar_out_of_range", *gate.REASONS["calendar_out_of_range"])
        opening = None if found is None else calling_hours.from_local(found)
    d = decide_after_denial(denied, current_promised=bool(cur and cur["promised"]), current=now_target,
                            pacing=gate.pacing(tx, task_id), now=now, opening=opening, retry_wait=retry_wait,
                            current_callee_at=cur["callee_at"] if cur else None, current_window=window)
    if d is not None:
        _write(tx, task_id, d)
    return d


def _lapsed(now: datetime, window: Window) -> datetime:
    """お客様が言った日だけの約束（「13日なら」など）が、話せないまま過ぎた・その日にかけられる時刻が無いとき、契約者の間隔を
    数え始める時刻（1か所）：その日が過ぎてから（⚠️ いまから数えると、お客様が言った日より前に
    かけてしまう＝2026-10-19 の見直し）。"""
    return now if window.until is None else max(now, window.until)


def _window_over(tx: Tx, task_id: UUID, now: datetime, target: Target | None, window: Window, *,
                 apology: bool | None = None) -> Decision:
    """理由の分からない日だけの約束（「13日なら」など）が、話せないまま過ぎた（その日にかけられる時刻が無い）：時刻の決まっていない
    約束と同じく（`_unspecified_next` の1か所）、日が過ぎてから（`_lapsed`）かけ直す（2026-10-19 ユーザー：「理由がどうしても
    分からないものは「時刻の決まっていない約束」の方針の方がいい」）。幅は外す。お客様と約束したかけ直しのまま。呼ぶのは
    `_keep_promise` だけ。"""
    if target is None:
        raise ValueError("いまの予定に番号がありません（かけ直す予定は必ず番号を持つ）")
    base = _lapsed(now, window)
    return _unspecified_next(tx, task_id, target, now=now, base=base, tried=base, apology=apology)


def _unspecified_next(tx: Tx, task_id: UUID, target: Target, *, now: datetime, base: datetime, tried: datetime,
                      apology: bool | None = None) -> Decision:
    """時刻の決まっていない約束の次の動き（1か所。2026-10-19 の見直し）：お客様が時刻を言わずに約束した通話のあと・その約束の電話で
    話せなかったあと・理由の分からない日だけの約束が過ぎたあと。契約者の再発信の方針（callback_unspecified_time。実施回のいまの版
    ＝`_unspecified_policy`）の間隔で、約束したかけ直しのまま（行の回数の上限・1日の回数の外）。終えるのは、方針が「終える」・
    「あとで」と言われた回数が方針の回数を超えた・約束をした通話のあとにかけた、時刻の決まっていない約束の電話の数が方針の
    回数に届いたとき（`_unspecified_calls`）。
    base＝間隔を数え始める時刻、tried＝前にかけた時刻（「n 日後」は別の時間帯にする）。⚠️ 前は、約束の電話に出ないと、約束でない
    ふつうのかけ直しになり、行の回数の上限で終わった。"""
    p = _unspecified_policy(tx, task_id)
    said = _said(tx, task_id, "callback_unspecified_time")   # 「あとで」と言われた回数（言った回数なので、どの通話も数える）
    calls = _unspecified_calls(tx, task_id)
    if p.action == "end" or said > p.max_times or calls >= p.max_times:
        return replace(_end("callback_unspecified_time",
                            f"時刻の決まっていない約束：再発信の方針（{p.max_times} 回まで）で終える"), apology=apology)
    at = _retry_at(tx, task_id, p.interval, base, tried)
    if at is None:
        return replace(_no_opening(now, True, target), apology=apology)
    return _call(max(at, gate.pacing(tx, task_id, contact_id=target.contact_id, promised=True).earliest or at), True,
                 "お客様と約束したかけ直し（時刻の決まっていない約束：再発信の方針 callback_unspecified_time の間隔で）",
                 target, None, None, apology)


def _plan_at(tx: Tx, task_id: UUID, at: datetime) -> dict[str, Any] | None:
    """その時刻に効いていた次の予定（その時刻までに決めた、いちばん新しい予定。1か所）。かけた電話が守っていた予定を読むのに使う
    （いまの予定ではない：判定し直しのときは、いまの予定が判定待ちになっていて、幅を失うため）。"""
    return tx.one(
        "SELECT kind, window_until, window_daily, window_days, window_from, window_repeat, window_round, callee_at, promised"
        " FROM salescall.task_next_actions WHERE task_id = %s AND decided_at <= %s ORDER BY seq DESC LIMIT 1",
        (task_id, at))


def _served_unspecified(tx: Tx, task_id: UUID, at: datetime, purpose: str) -> bool:
    """その電話（at にかけた・目的 purpose）が守っていたのが、時刻の決まっていない約束だったか（1か所）。
    - ふつうの電話：かけた時点の予定が、約束したかけ直しで、時刻も幅も無い（「あとで」・日だけの約束が過ぎたあと・時刻の無い約束で
      番号違いを直した番号への電話）。
    - 続き（途中で切れた通話のすぐのかけ直し）：切れた通話（その前の、続きでない電話）で、お客様が時刻を言わずに約束した、または
      その通話が時刻の決まっていない約束を守っていた（⚠️ 前は「ふつうの電話」だけを見ていて、続きの電話に出ないと約束が消えた）。
    - 番号の聞き直し・お詫び：約束を守る電話ではない（それぞれの回数の決まり）。"""
    if purpose == "resume":
        cut = tx.one(f"SELECT attempt_id, claimed_at, purpose FROM salescall.attempts WHERE task_id = %s AND {attempts.DIALED}"
                     " AND purpose <> 'resume' AND claimed_at < %s ORDER BY claimed_at DESC LIMIT 1", (task_id, at))
        if cut is None:
            return False
        said = tx.one("SELECT business_state::text AS state, detail -> 'callback' ->> 'at' AS at, detail ? 'callback' AS cb"
                      " FROM salescall.call_judgements WHERE attempt_id = %s", (cut["attempt_id"],))
        if said is not None and said["state"] == "callback_scheduled" and said["cb"] and said["at"] is None:
            return True
        return _served_unspecified(tx, task_id, cut["claimed_at"], cut["purpose"])
    if purpose != "normal":
        return False
    plan = _plan_at(tx, task_id, at)
    return (plan is not None and plan["kind"] == "call" and plan["promised"] and window_of(plan) is None
            and plan["callee_at"] is None)


def _unspecified_calls(tx: Tx, task_id: UUID) -> int:
    """お客様がかけ直しを約束した、いちばん新しい通話のあとに、この行でかけた、時刻の決まっていない約束の電話の数（1か所。
    回線の側でかけられなかった電話は数えない）。⚠️ 前は約束のあとの電話をすべて数えたので、日だけの約束の日にかけた分まで入り、
    日が過ぎて時刻の決まっていない約束に移ったとたん、1回もかけずに終えた。"""
    rows = tx.all(
        f"SELECT a.claimed_at, a.purpose FROM salescall.attempts a WHERE a.task_id = %s AND {attempts.placed('a')}"
        " AND a.claimed_at > coalesce((SELECT max(c.claimed_at) FROM salescall.attempts c"
        "   JOIN salescall.call_judgements r ON r.attempt_id = c.attempt_id"
        "   WHERE c.task_id = %s AND r.business_state = 'callback_scheduled'), '-infinity')",
        (task_id, task_id))
    return sum(_served_unspecified(tx, task_id, r["claimed_at"], r["purpose"]) for r in rows)
