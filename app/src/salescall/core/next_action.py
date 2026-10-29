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
  その時刻から1回分（初め30分）の中で5分後・さらに10分後・そして1回分の終わり＝`_exact_next_try`、話せなければ次の日の同じ時刻、3日で打ち止め）。
  時刻の決まっていない約束（「あとで」・日だけの約束が過ぎたあと）は `_unspecified_next` の1か所。幅の中でかけられる最も早い
  時刻と、話せなかったときの次の回（お客様がその日を選んだ理由に合わせる：曜日・週末など＝翌週の同じ日／時間の言葉＝次の日の
  同じ幅／月ぎめの理由＝翌月）を探すのは `_window_opening` の1か所。理由の分からない日だけの約束が過ぎたら、時刻の決まっていない約束の方針（`_window_over`）。
  幅は次の予定に持たせ、門が幅の外ではかけない。お客様が言った幅（言葉の表の幅・数の幅・時刻ちょうど）はすべてお客様の指定で、
  依頼者の時間帯・NG時間の外でもかける。端の決まっていない側（「朝」の始まり・「午後」の終わり・「3時まで」の始まりなど）は
  お客様指定の歯止め時間（設定 callee.designated_limit）で埋めて切る（お客様が口にした端はその外でも切らない）。お客様が言った日は依頼者の休みの
  日でもかけ、日を言っていない次の日の回は依頼者のかける日だけ（門と同じ `calling_hours.callee_hours` の1か所。2026-10-22・10-23
  ユーザー・見直し16）。
- **同じ日にかけ直さないか**は `not_today` の1か所（ふつうの電話・約束の電話・番号の聞き直し・お詫びの電話）。
- **お客様のところで電話が鳴らない電話が続いた番号**（回線の部品が番号を受け付けない・お客様が出る前に回線やこちらの側で終わる）
  へはかけない（`usable`。決まりは attempts.number_unusable の1か所＝門も同じ）。鳴らなかった電話のあとは、決まった順（設定
  dial.failed_retry_days。初めは、その日にもう1回・次の日に1回・そのさらに2日後に1回）で同じ番号へかけ直す（`_failed_retry_at`。
  2026-10-22 深夜 ユーザー。お客様が出る前に終わる番号も同じ＝2026-10-23 ユーザー）。
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

from salescall.core import (attempts, business_rules, calling_hours, gate, number_recheck, settings, spoken_time, target_policy,
                            tasks)
from salescall.core.access import Tx
from salescall.core.business_rules import Rules
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.judge import Judgement
from salescall.core.phone import normalize
from salescall.core.tasks import TaskNotFound, lock


def current(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """いまの予定（いちばん新しい記録）。無ければ None（まだ何も決めていない）。"""
    return tx.one(
        "SELECT task_id, seq, kind, call_at, end_reason, cause, promised, contact_id, purpose, wrong_contact_id, callee_at,"
        "       window_until, window_daily, window_days, window_from, window_repeat, window_round, window_missed, window_said,"
        "       window_day_said, apology_owed, apology_reasons, successor, number_again, decided_at"
        " FROM salescall.current_next_actions WHERE task_id = %s",   # いまの予定を選ぶのはデータベースの見方の1か所（0094）
        (task_id,),
    )


NUMBER_AGAIN = ("wrong_party", "unreachable")   # 番号を伺い直す理由：お客様が言った番号が別の方のお電話だった／繋がらなかった（0130）
PURPOSES = ("normal", "ask_number_again", "apology", "resume")   # 発信の目的：ふつう／番号の聞き直し（3-5）／お詫び（10-1-2）／
#                                                                   続き（途中で切れた通話のかけ直し。お詫びから始めて続ける）


@dataclass(frozen=True)
class Target:
    """次の予定でかける先（決めるのは `choose_number` の1か所）。かけられないときは contact_id が None で end_reason を持つ。"""
    contact_id: UUID | None
    end_reason: str | None = None
    purpose: str = "normal"
    wrong_contact_id: UUID | None = None    # 番号の聞き直しのとき：番号違いだった番号
    successor: bool = False                 # 目的のお客様の代わりに、後任のご担当者様をお呼びする（お呼びした方が在籍していない。0114）
    number_again: str | None = None         # 番号の聞き直しのとき：なぜ伺い直すか（NUMBER_AGAIN。0130。予約証へ写し、会話が理由ごとの
    #                                         一言で伺う＝CL-1 の連絡帳 4-41 の (3)）


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
    said: tuple[bool, bool] | None = None  # 1日の中の幅（daily）の始まり・終わりを、お客様が実際に口にしたか（歯止めの外でも切らない
    #                                        端。0118＝calling_hours.CalleeWindow.said）
    day_said: bool = False               # お客様がこの回の日を言った（依頼者の休みの日でもかける。0117）

    @property
    def empty(self) -> bool:
        return self.until is None and self.daily is None and self.days is None and self.repeat is None

    @property
    def callee(self) -> calling_hours.CalleeWindow:
        """かけてよい時間帯を決める材料（門と同じ `gate.window_hours` に渡す）。"""
        return calling_hours.CalleeWindow(self.daily, self.days, self.said, self.day_said)

    def later(self) -> Window:
        """次の回からの幅（1か所）：日を言っていない次の日の回（「夕方なら」「明日の15時」の次の日・「すぐ」の次の日）は、お客様が
        その日を言っていない（依頼者のかける日だけ）。曜日・月の理由の回（「金曜なら」の次の金曜）と、日の種類を言った幅（「土日祝なら」）
        は、お客様が選んだ日のまま。"""
        return replace(self, day_said=False) if self.repeat == spoken_time.DAY_REPEAT and self.days is None else self


def window_said(cb: dict[str, Any]) -> Window:
    """判定の結果のかけ直し（`judge` の detail.callback）の、お客様が言った時間の幅（1か所）。幅はすべてお客様の指定（依頼者の時間帯・
    NG時間の外でもかける）。端の決まっていない側の埋め方（お客様指定の歯止め時間）は `calling_hours.callee_range` の1か所（口にした
    端＝said はその外でも切らない）。"""
    iso = lambda v: None if v is None else datetime.fromisoformat(v)   # noqa: E731
    daily = None if cb.get("daily") is None else tuple(cb["daily"])
    return Window(iso(cb.get("until")), daily, None if cb.get("days") is None else tuple(cb["days"]), iso(cb.get("since")),
                  cb.get("repeat"), said=None if daily is None else tuple(cb.get("said") or (False, False)),
                  day_said=bool(cb.get("day_said")))


def window_of(row: dict[str, Any] | None) -> Window | None:
    """次の予定の記録の幅（無ければ None）。"""
    if row is None:
        return None
    w = Window(row["window_until"], None if row["window_daily"] is None else tuple(row["window_daily"]),
               None if row.get("window_days") is None else tuple(row["window_days"]), row.get("window_from"),
               row.get("window_repeat"), row.get("window_round") or 0, row.get("window_missed") or 0,
               None if row.get("window_said") is None else tuple(row["window_said"]), bool(row.get("window_day_said")))
    return None if w.empty else w


# お客様につながったら、まずお詫びすること（次の予定の印 apology_reasons。予約証で会話の処理へ渡す＝gate.Claim.apology。0113）
APOLOGY_REASONS = ("missed_promise", "cut_off")   # 約束した日（回）に一度もかけられなかった／前の電話が途中で切れた
# 決めたお詫びの印：None＝前の予定から引き継ぐ／False＝外す（目的のお客様と話せた）／理由1つ（str）・理由の集まり（frozenset）＝
# 前の印に足す／理由の並び（tuple）＝その並びにする（目的のお客様と話せた通話が、また途中で切れた＝前の印は済み、新しい理由だけ）
Apology = bool | str | frozenset[str] | tuple[str, ...] | None


def _plus(apology: Apology, reason: str) -> Apology:
    """決めたお詫びの印に、理由を1つ足す（1か所）。"""
    if apology is None or apology is False:
        return reason
    if isinstance(apology, str):
        return frozenset({apology, reason})
    if isinstance(apology, frozenset):
        return apology | {reason}
    return tuple(dict.fromkeys(tuple(apology) + (reason,)))


def _apology_reasons(prev: tuple[str, ...], apology: Apology) -> tuple[str, ...]:
    """次の予定のお詫びの理由（1か所）。"""
    if apology is None:
        return prev
    if apology is False:
        return ()
    if isinstance(apology, str):
        apology = frozenset({apology})
    if isinstance(apology, frozenset):
        apology = tuple(dict.fromkeys(prev + tuple(sorted(apology))))
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
                               or (target.purpose == "ask_number_again") != (target.wrong_contact_id is not None)
                               or (target.purpose == "ask_number_again") != (target.number_again in NUMBER_AGAIN)):
        raise ValueError(f"発信の目的は {'・'.join(PURPOSES)} のどれか（聞き直しは、番号違いだった番号と、伺い直す理由を必ず持つ）")
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
    # 順番の番号（seq）はデータベースが付ける（next_seq の仕掛け＝0109。ここで「最大＋1」を数えない）。お詫びの印があるか
    # （apology_owed）は、データベースが理由（apology_reasons）から作る（0116）
    row = tx.one(
        "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, end_reason, cause,"
        "                                         promised, contact_id, purpose, wrong_contact_id, callee_at, window_until,"
        "                                         window_daily, window_days, window_from, window_repeat, window_round,"
        "                                         window_missed, window_said, window_day_said, apology_reasons, successor,"
        "                                         number_again)"
        " SELECT t.tenant_id, t.client_id, t.task_id,"
        "        %s, CASE WHEN %s THEN now() ELSE %s::timestamptz END, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        "        %s, %s, %s, %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING seq",
        ("hold" if hold else "call" if calls else "end", call_now, call_at, end_reason, cause, promised,
         target and target.contact_id, target and target.purpose, target and target.wrong_contact_id, callee_at,
         window and window.until, None if window is None or window.daily is None else list(window.daily),
         None if window is None or window.days is None else list(window.days), window and window.since,
         window and window.repeat, 0 if window is None else window.round, 0 if window is None else window.missed,
         None if window is None or window.said is None else list(window.said), bool(window and window.day_said),
         list(reasons), bool(target and target.contact_id and target.successor),
         target.number_again if target is not None and target.purpose == "ask_number_again" else None, task_id),
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


@dataclass(frozen=True)
class Numbers:
    """行の番号の材料（締めの処理の時点。`numbers_of` の1か所で集める）。"""
    unusable: frozenset[UUID] = frozenset()   # 回線の部品が続けて受け付けなかった番号（attempts.number_unusable の1か所。0118）
    said: tuple[UUID, ...] = ()                # お客様が言った別の番号のうち、かけない理由の無いもの（新しい順）
    sources: tuple[tuple[UUID, UUID], ...] = ()   # お客様が言った番号 → その番号を言われた通話でかけた番号（伺い直す先）
    asked_again: bool = False                  # この行で、前にも番号を伺い直すと決めた（number_recheck.asked_again_before の1か所）


def choose_number(j: Judgement, *, dialed: Target, alternate: Alternate | None, recheck: Recheck | None,
                  cut_off: bool = False, numbers: Numbers = Numbers()) -> Target:
    """次にかける先（番号と発信の目的。1か所。下の _choose）。後任の方を追う印（successor）は、この通話で目的のお客様が後任の方に
    替わった（判定の印）か、かけた電話が後任の方を追う電話だったら、行が終わるまで引き継ぐ（後任の方に繋がったあとも、これからの
    電話は後任のご担当者様をお呼びする。2026-10-22 ユーザー：「基本は後任へつないでもらう」）。決めた番号が、回線の部品に続けて受け付け
    られなかった番号なら `usable` で替える。"""
    t = usable(_choose(j, dialed=dialed, alternate=alternate, recheck=recheck, cut_off=cut_off), numbers)
    if t.contact_id is not None and (j.detail.get("successor") or dialed.successor):
        return replace(t, successor=True)
    return t


def usable(t: Target, numbers: Numbers) -> Target:
    """かける番号が、お客様のところで鳴らない電話が続いた番号（attempts.number_unusable：設定 dial.failed_retry_days の並びの数
    だけ続いた）なら、その番号へはかけない（1か所。計画書 10-3「無限に再試行しない」。⛔ 見直し15：発信に失敗した電話は行の回数に
    数えないので、前は行が終わらず、同じ番号へかけ続けた）：お客様が言った別の番号（かけない理由の無いもの）があれば、そちらへ（ふつうの電話・続きの電話だけ。
    番号の聞き直し・お詫びは、その番号のお客様へかける電話なので替えない）。無ければ行を終える（number_unusable。番号は記録に
    残り、依頼者へ渡す）。"""
    if t.contact_id is None or t.contact_id not in numbers.unusable:
        return t
    if t.purpose in ("normal", "resume"):
        for c in numbers.said:
            if c not in numbers.unusable and c != t.contact_id:
                return replace(t, contact_id=c)
        # お客様が言った番号に繋がらなかった：その番号を言われた通話の番号へ、番号を伺い直す（同じ行で1回だけ。番号違いの聞き直しと
        # 同じ数え方＝numbers.asked_again。2026-10-24 ユーザー：「別の方のお電話よりも、繋がらない番号のパターンも必要じゃないですか？」）
        source = dict(numbers.sources).get(t.contact_id)
        if source is not None and source not in numbers.unusable and not numbers.asked_again:
            return Target(source, None, "ask_number_again", t.contact_id, number_again="unreachable")
    return Target(None, "number_unusable")


def _choose(j: Judgement, *, dialed: Target, alternate: Alternate | None, recheck: Recheck | None,
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
        return Target(recheck.source_contact_id, None, "ask_number_again", dialed.contact_id, number_again="wrong_party")
    if dialed.purpose == "ask_number_again" and (j.state in ("open", "not_reached") or cut_off):
        return replace(dialed, successor=False)
    return Target(dialed.contact_id)


@dataclass(frozen=True)
class AskAgain:
    """別の時間帯にかけ直す電話（番号の聞き直し・お詫び）に出なかったときの材料（2026-10-15 ユーザー：「3回までなどの制限を
    かけ、別の時間帯にかけ直す」）。"""
    tries: int                   # この行で、その目的でかけた回数（お客様に届いた発信＝gate.purpose_dials。今回を含む）
    limit: int                   # 設定 retry.ask_number_again_max／retry.apology_max
    at: datetime | None          # まだ試していない時間帯の、次にかけられる時刻（無ければ None）


def decide_after_call(j: Judgement, *, cut_off: bool = False, normal: gate.Pacing, promised: gate.Pacing, rules: Rules,
                      times_with_key: int, now: datetime, target: Target, retry_at: datetime | None,
                      ask: AskAgain | None = None, forbidden: bool = False, kept: Decision | None = None,
                      resume_window: Window | None = None, resume_callee_at: datetime | None = None,
                      resume: bool | None = None, failed_at: datetime | None = None) -> Decision:
    """通話のあとの次の動き（下の `_decide`）。途中で切れた通話（cut_off）のあとは、かけ直す・判定待ちのどれでも、次にお客様に
    つながったときにお詫びから始める（印 cut_off を付けるのは、ここの1か所。すぐにかけ直すか＝resume とは別に決める。⛔ 見直し15：
    前は続けて3回切れると、すぐのかけ直しをやめるのと一緒にお詫びの印も外れ、次の電話でお詫びしなかった）。
    resume＝すぐにかけ直してよいか（続けて切れた数で after_call が決める。None＝cut_off と同じ）。
    failed_at＝かける番号へ、鳴らない電話が続いているときの次の時刻（`_failed_retry_at`。続いていなければ None）。"""
    d = _decide(j, cut_off=cut_off, normal=normal, promised=promised, rules=rules, times_with_key=times_with_key, now=now,
                target=target, retry_at=retry_at, ask=ask, forbidden=forbidden, kept=kept, resume_window=resume_window,
                resume_callee_at=resume_callee_at, resume=cut_off if resume is None else resume, failed_at=failed_at)
    return replace(d, apology=_plus(d.apology, "cut_off")) if cut_off and d.kind in ("call", "hold") else d


def _decide(j: Judgement, *, cut_off: bool, normal: gate.Pacing, promised: gate.Pacing, rules: Rules,
            times_with_key: int, now: datetime, target: Target, retry_at: datetime | None,
            ask: AskAgain | None, forbidden: bool, kept: Decision | None,
            resume_window: Window | None, resume_callee_at: datetime | None, resume: bool,
            failed_at: datetime | None = None) -> Decision:
    """通話のあとの次の動き。normal / promised＝次にかける先へ、ふつうの予定・約束したかけ直しにした場合の門の数え方。
    times_with_key＝この行で、今回の結果と同じ再発信の方針の鍵の結果が出た回数（今回を含む）。
    target＝`choose_number` の答え（次にかける先、または終える理由）。ask＝聞き直しで出なかったときの材料（そのときだけ）。
    retry_at＝この結果の再発信の方針の間隔（n 時間おき／n 日後）が指す時刻（`_retry_at` の1か所で出す。かけてよい時間帯が
    これから先に無ければ None）。forbidden＝この通話で、言ってはいけないことを言った（通話後の検証で、2つの耳で確かめた）。
    kept＝お客様と約束したかけ直しを守る次の動き（時刻・幅の約束は `_keep_promise`、時刻の決まっていない約束は
    `_unspecified_next` の1か所が決めたもの）：お客様がかけ直しを約束した通話のあと、約束の申し出のある通話が途中で切れて判定
    できなかったあと（judge の promise_offer）、または約束の電話で話せなかった（出なかった・目的のお客様に繋がらなかった）通話のあとだけ。
    resume_window・resume_callee_at＝途中で切れた通話が守っていた、お客様が言った時間の幅・時刻（すぐかけ直す電話も、同じ約束の
    回のまま）。"""
    # 判定できない：何も決めない（終える理由も含めて）。終えずに残し、原因が直ったら判定し直す（2026-10-16 ユーザー決定）。
    # ⚠️ 先に見る：言ってはいけないこと・検証の失敗・苦情で先に終えると、判定し直す道が無くなり、断られたかが決まらないまま
    # 同じお客様のほかの行を止め続けた（2026-10-17 の見直し）。判定し直したあと、この順でもう一度決まる
    if j.state == "unknown":
        if cut_off and not forbidden and target.contact_id is not None:
            # 途中で切れて、判定できなかった：通話のあとの確かめで断りの言葉が見つかっていない（見つかっていれば、判定が営業拒否に
            # している＝judge.refusal）ので、お詫びのかけ直しをする（2026-10-22 ユーザー：「通話のあとの確かめで断りの言葉が見つから
            # なければ、お詫びのかけ直しをします。断り（営業拒否）の言葉が見つかったときは、営業拒否」）。判定できなかった結果は依頼者へ
            # 渡さず、判定し直しの対象のまま（settle.awaiting_rejudge の1か所）。
            # 会話の報告にかけ直しの約束の申し出があれば、すぐにはかけず約束を守る（kept。ユーザー決定7：「あとで」と言ったお客様の電話が
            # 途中で切れたら、約束の間隔でお詫びから。時刻の読めない約束は、時刻の決まっていない約束の間隔。⛔ 見直し15：前は約束の申し出
            # があっても、すぐかけ直した）。無ければ、すぐ（続けて2回まで＝resume）
            if kept is not None:
                return kept
            if resume:
                again = target if target.purpose == "ask_number_again" else replace(target, purpose="resume")
                return _call(promised.earliest or now, True, "途中で切れて判定できなかった：お詫びとともにすぐかけ直す（判定し直しは続ける）",
                             again, resume_callee_at if resume_window else None, resume_window)
        return _hold(f"判定できなかった（{j.detail.get('unknown_cause')}）：判定待ちで残す")
    if forbidden:   # 2026-10-15 ユーザー：「その行はひとまず終える。依頼主でなく、管理者へ報告。場合によっては、お詫びを入れる」
        return _end("forbidden_speech", "言ってはいけないことを言った：その行はひとまず終える（契約者と管理者へ知らせ、CL-1 へ修正依頼を出した）")
    if j.state in END_STATES:
        return _end(j.state, "目的を果たした・終わった結果")
    if j.complaint:
        return _end("complaint", "苦情があった")
    if target.contact_id is None:
        return _end(target.end_reason, f"かける番号が無い（{target.end_reason}。番号は記録して依頼者へ渡す）")
    key = j.detail.get("policy_key")
    if key in business_rules.SYSTEM_ENDS:
        # お客様の側の事情で、電話ではかけ直さない結果（ほかの手段を望まれた・在籍していない。2026-10-22 ユーザー決定）：約束した
        # かけ直し・途中で切れたあとのかけ直しより先に終える（お客様が電話でないことを望んだ・お客様がもういない）
        return _end(key, f"システムの方針：{key} は電話ではかけ直さずに終える")
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
    # この通話でかけ直しの約束ができていれば（時刻・幅・時刻の決まっていない「あとで」も）、すぐにはかけず約束を守り、お詫びの印だけ
    # 付ける（下。2026-10-22 ユーザー：「あとで」と言ったお客様の電話が途中で切れたとき＝「OK」）
    # 続けて切れた（3回目から＝resume でない）ときは、すぐにはかけず、下のふつうの決まりで（お詫びの印は付ける＝decide_after_call）
    if resume and target.purpose in ("normal", "resume", "ask_number_again") and j.state != "callback_scheduled":
        again = target if target.purpose == "ask_number_again" else replace(target, purpose="resume")
        return _call(promised.earliest or now, True, "途中で切れた：復旧次第、お詫びとともにすぐかけ直す",
                     again, resume_callee_at if resume_window else None, resume_window)
    if j.state == "callback_scheduled":
        # お客様と約束したかけ直し：次の動きは、時刻・幅を言った約束は `_keep_promise`、時刻の決まっていない約束は
        # `_unspecified_next` の1か所が決めたもの（途中で切れていれば、お詫びの印は decide_after_call が付ける）
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
        at = ask.at if failed_at is None else failed_at   # 鳴らなかった電話のあとは、決まった順で同じ番号へ
        if at is None:
            return _no_opening(now, True, target)
        return _call(max(at, promised.earliest or at), True,
                     f"番号の聞き直し：出なかった ⇒ 別の時間帯に（{ask.tries + 1} 回目）" if failed_at is None else
                     "番号の聞き直し：お客様のところで鳴らなかった ⇒ 決まった順で同じ番号へ（dial.failed_retry_days）",
                     target)
    if normal.remaining <= 0:
        return _end("max_attempts", "この行にかけてよい回数を使い切った")
    if failed_at is not None:
        # お客様のところで鳴らなかった（回線の部品が番号を受け付けなかった・お客様が出る前に終わった。お客様の結果ではない）：
        # 結果ごとの再発信の方針でなく、決まった順で同じ番号へ（2026-10-22 深夜 ユーザー：「当日2回＆翌日1回、さらに2日後1回の計4回」。
        # 2026-10-23 ユーザー：お客様が出る前に終わる番号も「番号を受け付けないときと同じ」。順を使い切った番号へは、もうかけない＝`usable`）
        return _call(max(failed_at, normal.earliest or failed_at), False,
                     "お客様のところで鳴らなかった（発信に失敗・お客様が出る前に終わった）：決まった順で同じ番号へかけ直す（dial.failed_retry_days）", target)
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
    ＝`after_denial`。幅の外 callback_window で止められるのは幅のある予定だけなので、ここには来ない）。
    受けるのは、門の理由の表で扱いが plan・plan_if_known の理由だけ（ほかは行の予定を書き直さない＝gate.PLAN_KEPT。⛔ 見直し17a：
    前は、どの集まりにも入らない理由を「決まった間のあと」へ書き直し続けた）。待つ理由は、門と同じ pacing が答える時刻へ1回書く。"""
    if gate.REASONS[d.reason].handling not in ("plan", "plan_if_known"):
        raise ValueError(f"{d.reason} は、行の予定を書き直さない理由です（gate.REASONS の扱い {gate.REASONS[d.reason].handling}）")
    if d.reason == "callback_window":   # 幅のある約束だけが幅の外で止められ、その次の回は `_keep_promise` の1か所が決める
        raise ValueError("幅の外（callback_window）の次の回は、after_denial の _keep_promise が決めます")
    if d.reason in ("awaiting_decision", "not_due", "no_schedule", "task_ended", "judge_pending"):
        # 締めの処理・いまの予定に任せる（先に書き直さない）。予定の無い行・終えた行に、門の答えから書かない
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
    # 間隔の待ち（pacing が門と同じ時刻を答える）。門のあとで変わって、もう待たなくてよい・時刻が分からない（まれ）ときだけ、
    # 決まった間のあと（その時刻に門がもう一度確かめる。時刻の分からない待ちは候補に出さない＝gate.waits_unknown）
    at = pacing.earliest if pacing.earliest is not None and pacing.earliest > now else now + timedelta(seconds=retry_wait)
    return _call(at, current_promised, f"門：待てば変わる理由（{d.reason}）", current, current_callee_at, current_window)


def _write(tx: Tx, task_id: UUID, d: Decision) -> int:
    if d.kind == "end":
        return end(tx, task_id, d.end_reason, cause=d.rule, apology=d.apology)
    if d.kind == "hold":
        return hold(tx, task_id, cause=d.rule, apology=d.apology)
    return _append(tx, task_id, call_at=d.at, end_reason=None, cause=d.rule, promised=d.promised, target=d.target,
                   callee_at=d.callee_at, window=d.window, apology=d.apology)


# 目的のお客様に繋がらなかった理由のうち、同じ日にまたかけてよいかを聞く（判定の again）もの（1か所）：不在・取り次げない・在籍して
# いないで後任の方を追う（ほかの手段・在籍していないで終えるは、かけ直さない＝システムの方針。番号違いは番号の確かめの道）
UNREACHED_REASONS = ("absent", "not_transferred", "departed")


def _line_fault_again(tx: Tx, task_id: UUID, dialed: Target, dialed_at: datetime, now: datetime,
                      blame: str | None) -> Decision | None:
    """お客様のところで鳴らなかった発信のうち、番号のせいでないと分かったもの（`attempts.REDO_AS_PLANNED`＝回線のせい line・
    こちらのせい ours）のあとの次の動き（1か所。ふつうの電話・約束の電話・お詫びの電話のどれも）：かけなかったのと同じに、かけた予定を
    そのまま（かける先・約束・時刻・幅・お詫びの印）。回数・番号の続き・約束の回の本数のどれにも数えない。
    - line（テスト番号が鳴らなかった・回線を使わなくした＝回線のせいと分かった。0143）：いまから。回線はその回線ごとの処理が休ませ、
      続けば止めて知らせる（ほかの回線は、すぐかけられる）。2026-10-24 ユーザー：「回線の問題が明らかな場合は行は取りやめでなく一時中止
      だけ」。まだ分からないあいだは締めないので（settle.settle）、決まったときに、ここで1回だけ決める。
    - ours（こちらのプログラムが固まった・甚大な問題で終えた・通話を進める部品が落ちた＝試行の trouble。0136）：いまから。ただし門と
      候補を出す処理が、復旧するまで待たせる（データベースの salescall.awaiting_recovery の1か所＝お客様が出たあとに終えた電話のすぐの
      かけ直しと同じ。復旧＝同じ契約者で、こちらの不具合なしにお客様と話し終えた電話がある・その不具合を直したと記録され、そのあと
      プログラムが替わった＝salescall.recovered_after。0141・0143。
      2026-10-17 管理者：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」。⛔ 0141 の前は、復旧を見ずに同じお客様への
      最短の間隔だけをあけた＝お客様が出たあとの電話とは別の決め方だった）。
    それ以外（番号のせい・まだ分からない・鳴った）・かけた予定が読めなければ None（ふつうの決め方で決める）。"""
    if blame not in attempts.REDO_AS_PLANNED:
        return None
    row = _plan_at(tx, task_id, dialed_at)
    if row is None or row["kind"] != "call":
        return None
    target = _dialed_marks(dialed, row)
    why = ("回線のせいで鳴らなかった（テスト番号も鳴らなかった・回線を使わなくした）：回線の一時中止だけで、かけた予定のまま（どの数にも数えない）"
           if blame == "line" else
           "こちらのせいで鳴らなかった（こちらのプログラムの不具合）：かけた予定のまま、復旧次第（どの数にも数えない）")
    return _call(now, bool(row["promised"]), why, target, row["callee_at"] if row["promised"] else None, window_of(row))


def after_call(tx: Tx, task_id: UUID, j: Judgement, *, rules: Rules, dialed: Target,
               dialed_at: datetime, alternate: Alternate | None, recheck: Recheck | None, forbidden: bool,
               cut_off: bool = False, reached_target: bool = False, blame: str | None = None) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。判定の結果から次の動きを決めて記録する（同じ取引）。
    cut_off＝お客様が出たあと、お客様の意思と関係なく途中で切れた（決めるのは締めの処理の1か所）。
    dialed＝この通話でかけた先（番号・目的）、dialed_at＝かけた時刻。alternate＝お客様が言った別の番号の材料（言われたときだけ）。
    recheck＝言われた番号が番号違いだったときの番号の確かめ（そのときだけ）。reached_target＝目的のお客様と話せた（約束の日に
    かけられなかったお詫びは、ここで済んだものとして外す＝会話の処理が、予約証の印を見て最初にお詫びしている）。
    blame＝鳴らなかった発信のせい（attempts.blame。回線のせい・こちらのせいなら `_line_fault_again` の1か所で、かけた予定のまま）。"""
    now = tx.one("SELECT now() AS now")["now"]
    again = _line_fault_again(tx, task_id, dialed, dialed_at, now, blame)
    if again is not None:
        _write(tx, task_id, again)
        return again
    # かけた予定（発信のときに効いていた次の予定）が持っていた、お客様が言った時間の幅。いまの予定ではなく、発信の時刻までに
    # 決めた予定から読む（判定し直しのときは、いまの予定が判定待ちになっていて、幅を失うため）
    dialed_row = _plan_at(tx, task_id, dialed_at)
    dialed_window = window_of(dialed_row)
    dialed_exact = dialed_window is not None and dialed_row["callee_at"] is not None   # 時刻ちょうどの約束の回
    dialed = _dialed_marks(dialed, dialed_row)
    key = j.detail.get("policy_key")
    target = choose_number(j, dialed=dialed, alternate=alternate, recheck=recheck, cut_off=cut_off,
                           numbers=numbers_of(tx, task_id))
    count_at = target.contact_id or dialed.contact_id     # 終えるときは数え方を使わない（いまの番号で数えておく）
    promised = gate.pacing(tx, task_id, contact_id=count_at, promised=True)
    normal = gate.pacing(tx, task_id, contact_id=count_at, promised=False)
    times = 0 if key is None else _times(tx, task_id, key)
    # 目的のお客様に繋がらなかった（不在・取り次げない・在籍していないで後任の方を追う）通話で、同じ日にまたかけてよいとは言われ
    # なかった：同じ日にはかけ直さない（`not_today` の1か所。ふつうの電話・時刻の決まっていない約束は方針の時刻が今日なら次の日へ
    # ＝`_policy_at`。約束の幅の電話は当日の幅をやめて次の回へ。番号の聞き直しは次の日から探す＝`_new_band_retry`）
    no_today = not_today(*unreached_of(j))
    ask = None
    if target.purpose == "ask_number_again" and not cut_off:
        ask = _new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="ask_number_again",
                              not_today=no_today, now=now)
    failed_at = _failed_retry_at(tx, task_id, target.contact_id, now, dialed_at)
    policy = rules.retry.get(key) if key is not None else None
    retry_at = None if policy is None or policy.action != "call" else \
        _policy_at(tx, task_id, policy.interval, now, dialed_at, not_today=no_today)
    unreached = j.state == "not_reached" and j.detail.get("not_reached") in UNREACHED_REASONS
    # お客様が言った時刻・時間の幅の約束（「明日の15時」「夕方」「金曜なら」「13日なら」など）を守る次の動き（`_keep_promise` の1か所）。
    # 約束の申し出のある通話が途中で切れて判定できなかったとき（judge の promise_offer）も、同じ約束として守る（見直し15）
    kept = None
    cb = j.detail.get("callback") if j.state == "callback_scheduled" else \
        j.detail.get("promise_offer") if j.state == "unknown" and cut_off else None
    if target.contact_id is not None and cb is not None:
        if cb["at"] is None:
            kept = _unspecified_next(tx, task_id, target, now=now, base=now, tried=dialed_at)
        elif cb.get("exact", True):
            said = datetime.fromisoformat(cb["at"])
            kept = _keep_promise(tx, task_id, said, _exact_window(tx, task_id, said, day_said=bool(cb.get("day_said")),
                                                                  beyond_ok=bool(cb.get("beyond_ok"))),
                                 target, now=now, exact=True)
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
        if no_today:   # 幅が当日を越えて続くときも、当日の分はやめる
            after = min(dialed_window.until or _next_day(now), _next_day(now))
        elif dialed_exact:
            after = _exact_next_try(tx, task_id, dialed_window, now)
        else:
            # 目的のお客様に繋がらなかったが、あとでまたかけてよいと言われた ⇒ 目的のお客様がいま出られない＝「不在」の方針の間隔で
            # （「取り次げなかった」の方針＝日を改めて1回は、約束の電話には使わない＝約束の時刻が先）
            p = rules.retry["not_reached_absent"] if unreached else policy
            at = _retry_at(tx, task_id, p.interval, now, dialed_at) if p.action == "call" else None
            within = (at is not None and _round_calls(tx, task_id, dialed_window, times) <= p.max_times
                      and (dialed_window.until is None or at < dialed_window.until))
            after = at if within else (dialed_window.until or now)
        kept = _keep_promise(tx, task_id, after, dialed_window, target, now=now, exact=dialed_exact)
    elif (target.contact_id is not None and j.state in ("open", "not_reached")
          and _served_unspecified(tx, task_id, dialed_at, dialed.purpose)):
        # 時刻の決まっていない約束の電話（「あとで」・日だけの約束が過ぎたあと・その途中で切れたあとの続き など）で話せなかった：
        # 約束のまま（回数の上限の外）。取り次げず、またかけてよいと言われなければ、当日はかけない（方針の時刻が今日なら次の日へ）
        kept = _unspecified_next(tx, task_id, target, now=now, base=now, tried=dialed_at, not_today=no_today)
    # お客様のところで鳴らない電話（番号を受け付けない・お客様が出る前に終わる）が続いている番号へは、約束の電話・お詫び・続きを含めて、
    # 決まった順（`_failed_retry_at`）より前にはかけない（`_not_before_failed` の1か所。ふつうの電話・番号の聞き直しは `_decide` が
    # 同じ時刻を使う）。⛔ 見直し17a：前は約束を守る次の動きを先に返し、時刻ちょうどの約束の電話で 15:00・15:05・15:10・15:15 と
    # 4回失敗して、その日のうちに番号も行も終えた（2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の
    # 計4回」は、電話の種類によらない）
    kept = _not_before_failed(tx, task_id, kept, failed_at, now=now)
    # すぐにかけ直してよいか：途中で切れた通話のあと、お詫びしてすぐかけ直すのは、同じ行で続けて2回まで（2026-10-18 ユーザー 案A。
    # 決まりはデータベースの1か所 salescall.resume_allowed＝その発信が切れたかも、そこで見る。それを超えて切れた通話は、ふつうの電話の
    # 回数に数える＝dial_kind）。お詫びの印は、それと関係なく付ける＝decide_after_call
    resume = attempts.resume_allowed(tx, task_id)
    d = decide_after_call(j, cut_off=cut_off, normal=normal,
                          promised=promised, rules=rules, times_with_key=times, now=now, target=target,
                          retry_at=retry_at, ask=ask, forbidden=forbidden, kept=kept, resume_window=dialed_window,
                          resume_callee_at=dialed_row["callee_at"] if dialed_row else None, resume=resume,
                          failed_at=failed_at)
    d = _apologised(d, reached_target)
    _write(tx, task_id, d)
    return d


def unreached_of(j: Judgement) -> tuple[str | None, bool]:
    """同じ日にかけ直さないかの材料（目的のお客様に繋がらなかった理由・同じ日にまたかけてよいと言われたか）を、判定の中身から読む
    （値を決めるのは judge.unreached_today の1か所。お詫びの電話の締めは、業務の判定を書かないので、そちらを直に読む）。"""
    return (j.detail.get("not_reached") if j.state == "not_reached" else None), bool(j.detail.get("again"))


def not_today(not_reached: str | None, again: bool) -> bool:
    """同じ日にはかけ直さないか（1か所）：目的のお客様に繋がらなかった（不在・取り次げない・在籍していないで後任の方を追う）通話で、
    同じ日にまたかけてよいとは言われなかった。ふつうの電話・約束の電話・番号の聞き直し・お詫びの電話のどれも、これで決める。
    2026-10-22 ユーザー：「本日、本人が戻られる可能性があるのかと…後ほどおかけしてもよろしいでしょうかというお尋ねが必要…
    同じ日に2回以上、その言葉がなくかけるのであれば煙たがられますよ。（営業拒否につながる。）」「OKなら、当日の約束の時刻の幅の中
    かけ直してもいいと思うけど、そうでなければ、当日の幅はやめたほうがいい」。⛔ 見直し15：前は番号の聞き直し・お詫びの電話で
    見ず、同じ日にかけ直した。"""
    return not_reached in UNREACHED_REASONS and not again


def numbers_of(tx: Tx, task_id: UUID) -> Numbers:
    """行の番号の材料（1か所）：回線の部品が続けて受け付けなかった番号と、お客様が言った別の番号のうち、かけない理由の無いもの
    （かけない理由は target_policy.block_reason の1か所）。"""
    rows = tx.all("SELECT n.contact_id, n.phone_e164, n.source, t.client_ref, s.contact_id AS source_contact"
                  " FROM salescall.task_numbers n JOIN salescall.tasks t ON t.task_id = n.task_id"
                  " LEFT JOIN salescall.attempts s ON s.attempt_id = n.source_attempt_id"
                  " WHERE n.task_id = %s ORDER BY n.added_at DESC, n.phone_e164", (task_id,))
    scope = tasks.scope(tx, task_id)
    unusable = frozenset(r["contact_id"] for r in rows if attempts.number_unusable(tx, r["contact_id"], **scope))
    said = tuple(r["contact_id"] for r in rows if r["source"] == "callee" and r["contact_id"] not in unusable
                 and target_policy.block_reason(tx, normalize(r["phone_e164"]), client_ref=r["client_ref"], **scope) is None)
    sources = tuple((r["contact_id"], r["source_contact"]) for r in rows
                    if r["source_contact"] is not None and r["source_contact"] != r["contact_id"])
    return Numbers(unusable, said, sources, number_recheck.asked_again_before(tx, task_id))


def _failed_retry_at(tx: Tx, task_id: UUID, contact_id: UUID | None, now: datetime, dialed_at: datetime) -> datetime | None:
    """その番号へ、お客様のところで鳴らない電話（回線の部品が受け付けなかった・お客様が出る前に終わった＝attempts.failed_run）が
    続いているとき、次にかける時刻（1か所）。
    続いていない・番号のせいと分かった発信で並びを使い切った（その番号へはかけない＝`usable`）ときは None。並び（設定 dial.failed_retry_days。1＝最初に失敗
    した日）の次の日（前の失敗の日から並びの差より前にはしない）が今日（か、もう過ぎた）なら、最後の失敗から同じお客様への最短の間隔をあけて。先の日なら、その日の、前と別の
    時間帯で（`_retry_at`）。2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回受け付けられ
    なかったら、その番号へはかけない」。"""
    if contact_id is None:
        return None
    run = attempts.failed_run(tx, contact_id)   # 続いた数・最初の日・最後の終わり（データベースの1か所。⛔ 見直し16b：前は最初の日を
    k = run["n_sure"]                           # 別の問い合わせで求め直していた。回線のせいの発信は数えない＝0129）
    # 日をまたいで送るのは、番号のせいと分かった数だけ（0143。まだ分からない電話の行は、決まるまで締めない＝settle.settle。⛔ 0129〜0141：
    # まだ分からない電話も数えて次の日へ送り、壊れた回線の2時間で行が次の日へ送られた＝見直し21a の場面 a6）
    scope = tasks.scope(tx, task_id)
    days = attempts.failed_retry_days(tx, **scope)
    gap = timedelta(seconds=int(settings.resolve(tx, "gate.same_contact_min_interval", **scope)))
    if k == 0 or k >= len(days):
        return None
    # 次の日：「最初の失敗の日＋並び」と「前の失敗の日＋並びの差」の遅いほう（⛔ 見直し18c の (1)：前は最初の日からだけ数え、前の分が
    # 時間帯の外などで次の日にずれると、残りが同じ日に詰まった＝当日2回・翌日1回の「翌日」が、ずれた2回目と同じ日になった）
    last_day = calling_hours.local(run["last_end"]).date()
    day = max(run["first_day"] + timedelta(days=days[k] - 1), last_day + timedelta(days=days[k] - days[k - 1]))
    today = calling_hours.local(now).date()
    if day <= today:
        return max(now, run["last_end"] + gap)
    return _retry_at(tx, task_id, ("days", (day - today).days), now, dialed_at)


def _not_before_failed(tx: Tx, task_id: UUID, d: Decision | None, failed_at: datetime | None, *,
                       now: datetime) -> Decision | None:
    """鳴らない電話が続く番号へ、次にかける時刻の下限（1か所。`_failed_retry_at` の時刻より前にはかけない）。お客様が言った時刻・幅の
    約束は、その時刻から約束を守り直す（`_keep_promise`。同じ回の中か、次の回か。かけられなかった回のお詫びも、そこで決まる）。"""
    if d is None or failed_at is None or d.kind != "call" or d.at >= failed_at:
        return d
    if d.window is not None:
        # 約束は、決まった順の日の、約束の時刻・幅で（ふつうの電話の「前と別の時間帯」は使わない＝その日の始まりから約束を守り直す）
        day = calling_hours.local(failed_at).date()
        after = failed_at if day <= calling_hours.local(now).date() else \
            datetime.combine(day, datetime.min.time(), tzinfo=calling_hours.JST)
        again = _keep_promise(tx, task_id, after, d.window, d.target, now=now, exact=d.callee_at is not None)
        return again if again.apology is not None else replace(again, apology=d.apology)
    return replace(d, at=failed_at, rule=d.rule + "（鳴らない電話が続く番号：決まった順 dial.failed_retry_days より前にはかけない）")


def _round_calls(tx: Tx, task_id: UUID, window: Window, fallback: int) -> int:
    """約束の回 [since, until) に、この行でかけた電話のうち、お客様に届いた1回の数（今回を含む。1か所）。比べる相手の再発信の方針の
    回数（`_times`）と同じ数え方＝数え方の種類 dial_kind の 'reached'（`attempts.reached`。約束の電話なので約束かどうかは見ない。
    ⛔ 見直し16b：前は「鳴った・鳴りえた電話」で数え、方針の回数と数え方が混ざっていた）。回の始まりの分からない幅は fallback
    （その結果の方針の回数）。"""
    if window.since is None:
        return fallback
    return tx.one(f"SELECT count(*) AS n FROM salescall.attempts WHERE task_id = %s AND {attempts.reached()}"
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
    if isinstance(d.apology, str):
        return replace(d, apology=(d.apology,))
    return replace(d, apology=tuple(sorted(d.apology)) if isinstance(d.apology, frozenset) else d.apology)


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


def _policy_at(tx: Tx, task_id: UUID, interval: tuple[str, int], now: datetime, dialed_at: datetime, *,
               not_today: bool) -> datetime | None:
    """再発信の方針（ふつうの電話の結果ごとの方針・時刻の決まっていない約束の方針）の間隔が指す時刻（1か所）。同じ日にかけ直さない
    （`not_today`）ときは、方針どおりの時刻が今日に当たれば、次の日の、前と別の時間帯へ送る（方針が「n 日後」なら、もともと今日には
    ならない。⛔ 見直し15：前は時刻の決まっていない約束で、次の日から数え始め、「1日後」が2日後になった）。"""
    at = _retry_at(tx, task_id, interval, now, dialed_at)
    if not_today and at is not None and at < _next_day(now):
        at = _retry_at(tx, task_id, ("days", 1), now, dialed_at)
    return at


def _window_opening(tx: Tx, task_id: UUID, after: datetime, window: Window, *,
                    now: datetime) -> tuple[datetime | None, Window | None]:
    """お客様が言った時間の幅の中で、after（いまより前なら、いま）以降にかけられる最も早い時刻と、そのときの幅（1か所。DESIGN.md
    7-2）。探す時間帯は、門の時刻の確かめと同じ決まり（gate.window_hours＝`calling_hours.callee_hours` の1か所）：お客様が言った幅は、
    依頼者の時間帯・NG時間の外でも探す（端の決まっていない側はお客様指定の歯止め時間で埋めて切る。口にした端
    ＝window.said はその外でも）。お客様が日を言った回は、
    依頼者の休みの曜日・祝日でも、その日に依頼者の時間を当てる。日を言っていない次の日の回（`Window.later`）は、依頼者のかける日だけ
    （休みの日の回は、かけられる時刻の無い回として数えずに飛ばす）。いまの回 [since, until) の中に無ければ、お客様がその日を選んだ理由（window.repeat）で次の回へ移る。
    回の数（round）は、過ぎた回をすべて数える（話せないまま過ぎた回。かけられる時刻の無い回＝店休日などは数えない）。
    終わりの無い幅（「平日なら」「すぐ」）は、かけられる日の1日を、いまの回にする（話せなければ、次の日の同じ幅）。
    答え：(時刻, その回の幅)／(None, 幅)＝これから先、かけてよい時間帯そのものが無い（設定のその時の形。幅を保ち、確かめ直す）／
    (None, None)＝約束の回がもう無い（理由の分からない日だけの約束が過ぎた・お客様の幅が、かけてよい時間帯と重ならない）。"""
    scope = tasks.scope(tx, task_id)
    all_hours = gate.calling_hours_for(tx, **scope)
    hours = gate.window_hours(tx, **scope, window=window.callee)   # いまの回（門と同じ決まり＝`calling_hours.callee_hours`）
    later_w = window.later()                                       # 次の回から
    later = hours if later_w == window else gate.window_hours(tx, **scope, window=later_w.callee)
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

    now_ok, later_ok = _can_overlap(hours, window), _can_overlap(later, window)
    if not now_ok and not later_ok:
        # お客様の幅が、かけてよい時間帯と1日も重ならない：回を何年も探さずに、重ならない道へ（暦を先まで読まない。⛔ 2026-10-22
        # の見直し14：前は回を400回先まで探して暦の外に当たり、5分ごとに同じ予定を書き続け、行が終わらなかった）。依頼者の時間帯が
        # 1日も無い（NG時間を引いて残らない）のに、言葉の表の幅・日だけを言われたとき（依頼者の時間帯が1日も無ければ、深夜早朝の
        # 歯止めの時間を当てる＝`callee_hours`。それも重ならない幅）
        try:
            return none_left(local)
        except CalendarOutOfRange:
            return None, window
    try:
        upcoming = calling_hours.next_opening(hours, holiday, local, length) if now_ok else None
        if until is None:
            first = window
            if upcoming is not None and later_w != window and upcoming.date() != calling_hours.local(now).date():
                # お客様が日を言わずに「すぐ」などと言った：お客様が言った日は、いま話している今日だけ。今日にかけられなければ、
                # 次の日からは依頼者のかける日だけ（`Window.later`。見直し16）
                first = later_w
                upcoming = calling_hours.next_opening(later, holiday, local, length) if later_ok else None
            if upcoming is None:
                return none_left(local)
            day = datetime.combine(upcoming.date(), datetime.min.time())
            return from_jst(upcoming), replace(first, until=from_jst(day + timedelta(days=1)), since=from_jst(day),
                                               repeat=window.repeat or spoken_time.DAY_REPEAT)
        if upcoming is not None and upcoming < until:
            return from_jst(upcoming), window
        if window.repeat is None:
            # 理由の分からない日だけの約束が過ぎた（時刻の決まっていない約束へ）。いまの回にかけてよい時間帯そのものが無い（設定の
            # その時の形）なら、幅を保って確かめ直す
            return none_left(local) if upcoming is None else (None, None)
        if not later_ok:
            return None, None   # 次の回からは、かけてよい時間帯と重ならない（日を言っていない幅で、依頼者のかける日が無い など）
    except CalendarOutOfRange:   # 暦の無い日に当たる（暦をまだ取っていない）：決めつけず、幅を保って1日後に確かめ直す
        return None, window
    start, end, rounds = since or (until - timedelta(days=1)), until, window.round
    for _ in range(_MAX_ROUNDS_SEARCHED):
        start, end = (calling_hours.local(t) for t in spoken_time.next_occurrence(
            window.repeat, from_jst(start), from_jst(end)))
        try:
            first = calling_hours.next_opening(later, holiday, start, length)
        except CalendarOutOfRange:
            # 暦のある範囲（1年以上先まで）を探しても、かけられる回が無い ⇒ これから先に無いとみなす。暦がまだ近くまでしか
            # 無い（年の替わり目で翌年の暦をまだ取っていない など）⇒ 幅を保って、1日後に確かめ直す
            return (None, None) if start - local >= _CALENDAR_SEARCH_ENOUGH else (None, window)
        if first is None:
            return none_left(start)
        if first >= end:
            continue   # かけられる時刻の無い回（店休日など）：数えない
        rounds += 1
        found = first if first >= local else calling_hours.next_opening(later, holiday, local, length)
        if found is not None and found < end:
            return from_jst(found), replace(later_w, until=from_jst(end), since=from_jst(start), round=rounds)
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
        話せなければ、その日の時間（callback.exact_slot＝1回分。初め30分）の中で、前の電話から決めた間をあけ、最後の1本は1回分の
        終わりに（`_exact_next_try`。ユーザー：「15時から16時に数回かける」「5分後→さらに10分後→さらに20分後」⇒ 2026-10-23
        「時刻ちょうどの約束も含めて、30分」・案A＝15:00・15:05・15:15・15:30）。
    (4) 次の回が無い（理由の分からない日だけの約束が過ぎた・幅がかけてよい時間帯と重ならない）⇒ 日が過ぎてから、時刻の決まって
        いない約束の方針の間隔で（`_window_over`）。かけてよい時間帯そのものが無い ⇒ 幅を保って1日後に確かめ直す（`_no_opening`）。
    (5) 過ぎた回に、一度もかけられなかった ⇒ 次にお客様につながったときに、お詫びから始める（印 apology）。"""
    at, w2 = _window_opening(tx, task_id, after, window, now=now)
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


def _exact_gaps(tx: Tx, task_id: UUID) -> list[int]:
    """時刻ちょうどの約束の1日目の、前の電話からあける間の並び（秒。設定 callback.exact_retry_gaps。初め 5分・10分）。"""
    return [int(g) for g in settings.resolve(tx, "callback.exact_retry_gaps", **tasks.scope(tx, task_id))]


def _exact_window(tx: Tx, task_id: UUID, at: datetime, *, day_said: bool, beyond_ok: bool = False) -> Window:
    """時刻ちょうどの約束（「明日の15時」）の最初の回（1か所）：約束の時刻から、設定 callback.exact_slot（1回分。初め30分）の長さ。
    最後の1本は1回分の終わりに置く（`_exact_next_try`）ので、それが回線のふさがり・見守り役の回りで少し遅れてもかけられるように、門が
    見る回の幅は、1回分の終わりから並びの最初の間（callback.exact_retry_gaps の初め＝5分）だけ長い（その日の中まで。2026-10-23
    ユーザー：「時刻ちょうどの約束も含めて、30分」・案A＝15:00・15:05・15:15・15:30）。
    お客様が口にしたのは約束の時刻だけ（依頼者の時間帯・NG時間の外でもかける）。回の終わりはこちらが作った端なので、お客様指定の
    歯止め時間で切る（「21時45分」の約束は 21:45・21:50 だけ。22:00・22:15 にはかけない）。約束の時刻が歯止め時間の外なら、作った幅は
    通話の中で確かめて「はい」と言われたとき（beyond_ok＝会話の報告の callback.beyond_limit_ok）だけ使い（口にした時刻から1回分）、
    確かめていなければ、口にした時刻そのものの1本だけ（遅れの許し＝並びの最初の間を含む）。決めるのは `calling_hours.callee_range` の
    made の1か所（⛔ 見直し18a の (4)・18c の (2)：前は回の両端を口にした端にし、歯止め時間の外の 22:00・22:15 にもかけた）。
    day_said＝お客様がその日を言った（「土曜の15時」）。
    話せなければ、次の日の同じ時刻の回へ（repeat＝次の日。次の日はお客様が言った日ではない＝`Window.later`）。"""
    callee = gate.callee_settings(tx, **tasks.scope(tx, task_id))
    local = at.astimezone(calling_hours.JST)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = int((local - midnight).total_seconds())
    late = _exact_gaps(tx, task_id)[0]
    end = min(start + callee.exact_slot + late, 24 * 3600)
    limit = callee.designated_limit
    got = calling_hours.callee_range((start, end), (True, False), limit, end - start if beyond_ok else None, made=True)
    daily = got or (start, min(start + late, 24 * 3600))   # 確かめていない歯止めの外：口にした時刻そのものの1本
    # 門が同じ幅を読めるように（gate.window_hours も callee_range）：歯止め時間の中の約束は、終わりが作った端（歯止め時間で切った
    # もの）。外の約束は、確かめた1回分・口にした時刻そのものなので、両端ともお客様が認めた端
    said = (True, False) if limit[0] <= start < limit[1] else (True, True)
    return Window(midnight + timedelta(seconds=daily[1]), daily, None, at, spoken_time.DAY_REPEAT, said=said, day_said=day_said)


def _slot_of(window: Window) -> tuple[datetime, datetime]:
    """時刻ちょうどの約束の、その回の時間（約束の時刻から callback.exact_slot。1か所）。"""
    day = window.since.astimezone(calling_hours.JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(seconds=window.daily[0]), day + timedelta(seconds=window.daily[1])


def _exact_plan(start: datetime, final: datetime, gaps: list[int], round_: int) -> list[datetime]:
    """時刻ちょうどの約束の、その回にかける時刻の並び（1か所）。1日目（回0）は、約束の時刻から間の並び（callback.exact_retry_gaps。
    初め 5分・10分）を順に足した時刻のうち1回分の終わり（final）より前のものと、最後の1本＝1回分の終わり（初め30分）：15時の約束なら
    15:00・15:05・15:15・15:30（2026-10-19 ユーザー：「5分後→さらに10分後→さらに20分後」⇒ 2026-10-23 ユーザー：「時刻ちょうどの約束も
    含めて、30分」・案A「4本目を30分の終わりに合わせる」）。1回分を短くしても、最後の1本は必ず1回分の終わりに来る（間の並びの
    うち、そこを越えるものは使わない）。2日目・3日目（回1以上）は、その日の約束の時刻の1本だけ（2026-10-22 ユーザー）。"""
    if round_ != 0:
        return [start]
    plan, t = [start], start
    for g in gaps:
        t += timedelta(seconds=g)
        if t >= final:
            break
        plan.append(t)
    return plan + [final]


def _exact_next_try(tx: Tx, task_id: UUID, window: Window, now: datetime) -> datetime:
    """時刻ちょうどの約束の電話で話せなかった：その回の時間の中の、次にかける時刻（1か所）。かける時刻の並びは `_exact_plan`。
    本数は、その回にかけた電話のうち、お客様に届いた1回（`attempts.reached`＝ふつうの電話の方針の回数と同じ数え方 salescall.dial_kind の
    'reached'。発信に失敗・お客様が出る前の回線の失敗・すぐのお詫びのかけ直しの内の途中の切れは数えない。⛔ 見直し17c：前は「鳴りえた
    電話」で数え、お客様が出る前の回線の失敗が続くだけで、その回を使い切った）で数え、それが並びの数に届いた・回の時間が過ぎた ⇒
    回の終わり（次の回へ）。前の電話（発信まで進んだ電話すべての、いちばん新しいもの）が遅れたときも、そこから並びの最初の間（5分）は
    あける（⛔ 2026-10-22 の見直し14：前は鳴った電話だけから数え、回線の側でかけられなかった電話のあとは、すぐかけ直し続けた）。
    ⛔ 2026-10-23：前は前の電話からの間だけで決め（15:00・15:05・15:15・15:35）、4本目が30分の外に出た。"""
    start, end = _slot_of(window)
    gaps = _exact_gaps(tx, task_id)
    final = start + timedelta(seconds=gate.callee_settings(tx, **tasks.scope(tx, task_id)).exact_slot)   # 1回分の終わり
    plan = _exact_plan(start, final, gaps, window.round)
    dialed = tx.all(f"SELECT claimed_at, {attempts.reached()} AS rang FROM salescall.attempts WHERE task_id = %s AND"
                    f" {attempts.DIALED} AND claimed_at >= %s AND claimed_at < %s ORDER BY claimed_at",
                    (task_id, start, end))
    rang = sum(1 for d in dialed if d["rang"])
    if rang >= len(plan):
        return window.until
    nxt = plan[rang]
    if dialed:
        nxt = max(nxt, dialed[-1]["claimed_at"] + timedelta(seconds=gaps[0]))
    nxt = max(nxt, now)
    return window.until if nxt >= end else nxt


def _missed(tx: Tx, task_id: UUID, window: Window) -> bool:
    """その回（[since, until)）に、一度もかけられなかったか：その回の予定を初めて決めたあと（約束をした通話そのものは数えない。
    まだその回の予定が無い＝約束をした通話の締めでは、回の始まりから：その回の中でお客様と話したので、お詫びは要らない）、
    お客様のところで鳴った電話（`attempts.rang`＝数え方の種類 dial_kind が「届いた1回」か「途中で切れた」。発信に失敗・お客様が出る前の
    回線の失敗は、かけられなかったのと同じ＝お詫びの印が付く。⛔ 見直し17c：前は「鳴りえた電話」で見て、回線の失敗だけの回を
    「かけた」とし、お詫びが付かなかった）が1本も無い。回の始まりの分からない幅は数えない（False）。"""
    if window.since is None or window.until is None:
        return False
    return tx.one(
        f"SELECT NOT EXISTS (SELECT 1 FROM salescall.attempts WHERE task_id = %s AND {attempts.rang()}"
        "   AND claimed_at < %s AND claimed_at >= greatest(%s::timestamptz, (SELECT min(decided_at)"
        "     FROM salescall.task_next_actions WHERE task_id = %s AND window_from = %s AND window_until = %s))) AS m",
        (task_id, window.until, window.since, task_id, window.since, window.until))["m"]


def _unspecified_policy(tx: Tx, task_id: UUID) -> business_rules.Policy:
    version = tx.one("SELECT salescall.run_current_version(t.run_id) AS v FROM salescall.tasks t WHERE t.task_id = %s",
                     (task_id,))["v"]
    return business_rules.load(tx, version).retry["callback_unspecified_time"]


_BAND_RETRY_LIMITS = {"ask_number_again": "retry.ask_number_again_max", "apology": "retry.apology_max"}


def _new_band_retry(tx: Tx, task_id: UUID, after: datetime, *, purpose: str, not_today: bool = False,
                    now: datetime | None = None) -> AskAgain:
    """別の時間帯にかけ直す電話（番号の聞き直し・お詫び）で出なかったときの材料：その目的でかけた回数と試した時間帯
    （門の数え方）・まだ試していない時間帯の次にかけられる時刻。not_today＝同じ日にはかけ直さない（`not_today` の1か所。
    次の日から探す）。"""
    scope = tasks.scope(tx, task_id)
    get = lambda key: settings.resolve(tx, key, **scope)   # noqa: E731
    boundaries = list(get("retry.time_band_boundaries"))
    dials = gate.purpose_dials(tx, task_id, purpose=purpose)
    # 回数・試した時間帯：お客様に届いた発信だけ（gate.purpose_dials＝ふつうの電話の回数と同じ決まり salescall.dial_kind の1か所。
    # 回線の都合で繋がらなかった時間帯は、まだ試していない）。鳴らない電話が続く番号は、ここで数えず、番号ごとの歯止めで止める
    # （`usable`。⛔ 見直し15：前は発信まで進んだもの全部を数え、ふつうの電話と決まりが違った）。
    # 最小の間隔：お客様と約束したかけ直しは門の最小の間隔の対象外なので、この電話は、ここで前の発信から間を空ける。
    local = calling_hours.local(after)
    if not_today:
        local = max(local, calling_hours.local(_next_day(now or after)))
    if dials:
        local = max(local, max(d.ended for d in dials) + timedelta(seconds=int(get("gate.same_contact_min_interval"))))
    hours, length = gate.calling_hours_for(tx, **scope), timedelta(seconds=int(get("gate.claim_lifetime")))
    try:
        found = calling_hours.next_opening_in_new_band(hours, holiday_checker(tx), local, length, boundaries,
                                                       {calling_hours.band_of(d.at, boundaries) for d in dials})
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
                         target: Target, now: datetime, stop_reason: str | None = None, cut_off: bool = False,
                         failed_at: datetime | None = None) -> Decision:
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
    if target.contact_id is None:   # その番号へはかけない（回線の部品が続けて受け付けなかった＝`usable`）
        return _end(target.end_reason, f"お詫びの電話：かける番号が無い（{target.end_reason}）")
    if retry.tries >= retry.limit:
        return _end("apology_unreached", f"お詫びの電話：{retry.limit} 回かけても、お客様に届かなかった")
    if cut_off:
        return _call(promised.earliest or now, True, "お詫びの電話が途中で切れた：復旧次第、すぐかけ直す", target)
    if failed_at is not None:   # お客様のところで鳴らなかった：決まった順で同じ番号へ（ふつうの電話と同じ＝`_failed_retry_at`）
        return _call(max(failed_at, promised.earliest or failed_at), True,
                     "お詫びの電話：お客様のところで鳴らなかった ⇒ 決まった順で同じ番号へ（dial.failed_retry_days）", target)
    if retry.at is None:
        return _no_opening(now, True, target)
    return _call(max(retry.at, promised.earliest or retry.at), True,
                 f"お詫びの電話：届かなかった ⇒ 別の時間帯に（{retry.tries + 1} 回目）", target)


def after_apology(tx: Tx, task_id: UUID, *, delivered: bool, refused: bool, dialed: Target,
                  stop_reason: str | None = None, reached_target: bool = False,
                  not_reached: str | None = None, again: bool = False, dialed_at: datetime | None = None,
                  blame: str | None = None) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。お詫びの電話のあとの次の動きを決めて記録する（同じ取引）。
    reached_target＝目的のお客様と話せた（約束の日にかけられなかったお詫びの印を外す＝`_apologised`）。途中で切れて、すぐかけ直して
    よいかは、締めの処理が試行に残した「切れたか」を、データベースの resume_allowed の1か所が読む。not_reached・again＝目的のお客様に繋がらなかった理由と、同じ日にまた
    かけてよいと言われたか（会話の報告。同じ日にかけ直さないかは `not_today` の1か所＝ふつうの電話と同じ）。
    blame＝鳴らなかった発信のせい（dialed_at＝かけた時刻。ふつうの電話と同じ `_line_fault_again` の1か所）。"""
    now = tx.one("SELECT now() AS now")["now"]
    redo = _line_fault_again(tx, task_id, dialed, dialed_at, now, blame) if dialed_at is not None else None
    if redo is not None:
        _write(tx, task_id, redo)
        return redo
    promised = gate.pacing(tx, task_id, contact_id=dialed.contact_id, promised=True)
    retry = _new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="apology",
                            not_today=not_today(None if reached_target else not_reached, again), now=now)
    target = usable(dialed, numbers_of(tx, task_id))
    d = decide_after_apology(delivered=delivered, refused=refused, retry=retry,
                             promised=promised, target=target, now=now, stop_reason=stop_reason,
                             cut_off=attempts.resume_allowed(tx, task_id),
                             failed_at=_failed_retry_at(tx, task_id, target.contact_id, now, now))
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
                                                                            cur["wrong_contact_id"], bool(cur["successor"]),
                                                                            cur["number_again"])
    if denied.reason == "number_unusable" and now_target is not None:
        # その番号は、鳴らない電話が決まった数だけ続いた（門と同じ attempts.number_unusable）：お客様が言った別の番号へ替えるか、行を
        # 終える（通話のあとと同じ `usable` の1か所）
        t = usable(now_target, numbers_of(tx, task_id))
        if t.contact_id is None:
            d = _end(t.end_reason, "門：その番号は、鳴らない電話が続いた（もうかけない）")
        elif t.purpose == "ask_number_again":   # お客様が言った番号に繋がらない：言われた通話の番号へ、番号を伺い直す（すぐ。
            # 番号の聞き直しは約束の印を必ず持つ＝データベースの salescall.needs_promise_mark の1か所）
            d = _call(now, True, "門：お客様が言った番号は、鳴らない電話が続いた ⇒ 言われた通話の番号へ、番号を伺い直す", t)
        else:
            d = _call(now, bool(cur["promised"]), "門：その番号は、鳴らない電話が続いた ⇒ お客様が言った別の番号へ", t,
                      cur["callee_at"], window)
        _write(tx, task_id, d)
        return d
    if denied.reason in ("outside_hours", "callback_window") and window is not None and now_target is not None:
        # お客様が言った時間の幅の約束：幅の中の次の始まり・話せなかった回のあとは理由に合わせた次の回（`_keep_promise` の1か所）
        d = _keep_promise(tx, task_id, now, window, now_target, now=now, exact=cur["callee_at"] is not None)
        _write(tx, task_id, d)
        return d
    if denied.reason == "outside_hours":   # 次の時間帯の始まり（門の1日の回数の待ちと同じ gate.next_opening の1か所。暦の無い
        # 日に当たれば、決めつけず次の日の始まり＝その日に門がもう一度確かめる。⛔ 見直し17a：前は決まった間のあとへ書き直し続けた）
        opening = gate.next_opening(tx, now, tenant_id=tenant_id, client_id=client_id, business_id=business_id)
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
                      apology: bool | None = None, not_today: bool = False) -> Decision:
    """時刻の決まっていない約束の次の動き（1か所。2026-10-19 の見直し）：お客様が時刻を言わずに約束した通話のあと・その約束の電話で
    話せなかったあと・理由の分からない日だけの約束が過ぎたあと。契約者の再発信の方針（callback_unspecified_time。実施回のいまの版
    ＝`_unspecified_policy`）の間隔で、約束したかけ直しのまま（行の回数の上限・1日の回数の外）。終えるのは、方針が「終える」・
    「あとで」と言われた回数が方針の回数を超えた・約束をした通話のあとにかけた、時刻の決まっていない約束の電話の数が方針の
    回数に届いたとき（`_unspecified_calls`）。
    base＝間隔を数え始める時刻、tried＝前にかけた時刻（「n 日後」は別の時間帯にする）。not_today＝同じ日にはかけ直さない（`_policy_at`）。⚠️ 前は、約束の電話に出ないと、約束でない
    ふつうのかけ直しになり、行の回数の上限で終わった。"""
    p = _unspecified_policy(tx, task_id)
    said = _said(tx, task_id, "callback_unspecified_time")   # 「あとで」と言われた回数（言った回数なので、どの通話も数える）
    calls = _unspecified_calls(tx, task_id)
    if p.action == "end" or said > p.max_times or calls >= p.max_times:
        return replace(_end("callback_unspecified_time",
                            f"時刻の決まっていない約束：再発信の方針（{p.max_times} 回まで）で終える"), apology=apology)
    at = _policy_at(tx, task_id, p.interval, base, tried, not_today=not_today)
    if at is None:
        return replace(_no_opening(now, True, target), apology=apology)
    return _call(max(at, gate.pacing(tx, task_id, contact_id=target.contact_id, promised=True).earliest or at), True,
                 "お客様と約束したかけ直し（時刻の決まっていない約束：再発信の方針 callback_unspecified_time の間隔で）",
                 target, None, None, apology)


def _plan_at(tx: Tx, task_id: UUID, at: datetime) -> dict[str, Any] | None:
    """その時刻に効いていた次の予定（その時刻までに決めた、いちばん新しい予定。1か所）。かけた電話が守っていた予定を読むのに使う
    （いまの予定ではない：判定し直しのときは、いまの予定が判定待ちになっていて、幅を失うため）。"""
    return tx.one(
        "SELECT kind, window_until, window_daily, window_days, window_from, window_repeat, window_round, window_missed,"
        "       window_said, window_day_said, callee_at, promised, successor,"
        "       salescall.number_again_of(purpose, number_again) AS number_again"   # 前の予定の読み替えは1か所（0136）
        " FROM salescall.task_next_actions WHERE task_id = %s AND decided_at <= %s ORDER BY seq DESC LIMIT 1",
        (task_id, at))


def _dialed_marks(dialed: Target, row: dict[str, Any] | None) -> Target:
    """かけた電話の印（後任の方を追う・番号を伺い直す理由）を、かけたときに効いていた予定から写す（1か所。試行には持たない印）。
    番号を伺い直す理由の無い前の予定（0130 より前）の読み替えは、データベースの salescall.number_again_of の1か所（`_plan_at`・門が読む）。"""
    if row is not None and row["successor"]:   # 後任の方を追う電話だった（印は行が終わるまで引き継ぐ＝choose_number）
        dialed = replace(dialed, successor=True)
    if dialed.purpose == "ask_number_again":
        dialed = replace(dialed, number_again=(row or {}).get("number_again"))
    return dialed


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
    数えるのは、お客様に届いた1回＝`attempts.reached`。ふつうの電話の方針の回数と同じ数え方。発信に失敗・お客様が出る前の回線の
    失敗は数えない＝⛔ 見直し17c：前は「鳴りえた電話」で数え、回線の失敗で約束を使い切った）。⚠️ 前は約束のあとの電話をすべて数えたので、日だけの約束の日にかけた分まで入り、
    日が過ぎて時刻の決まっていない約束に移ったとたん、1回もかけずに終えた。"""
    rows = tx.all(
        f"SELECT a.claimed_at, a.purpose FROM salescall.attempts a WHERE a.task_id = %s AND {attempts.reached('a')}"
        " AND a.claimed_at > coalesce((SELECT max(c.claimed_at) FROM salescall.attempts c"
        "   JOIN salescall.call_judgements r ON r.attempt_id = c.attempt_id"
        "   WHERE c.task_id = %s AND r.business_state = 'callback_scheduled'), '-infinity')",
        (task_id, task_id))
    return sum(_served_unspecified(tx, task_id, r["claimed_at"], r["purpose"]) for r in rows)
