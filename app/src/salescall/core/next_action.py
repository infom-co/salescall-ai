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
  へはかけない（`usable`。番号の欄が「使えない」＝DESIGN.md 17-1-3。core/health.number_dead の1か所＝門も同じ）。番号のせいの鳴らなかった
  電話のあとは、決まった順（設定 dial.failed_retry_days。初めは、その日にもう1回・次の日に1回・そのさらに2日後に1回）で同じ番号へかけ
  直す（`_failed_retry_at`。番号の欄の次の日と時刻の下限＝core/health.number_wait。2026-10-22 深夜 ユーザー。お客様が出る前に終わる番号も
  同じ＝2026-10-23 ユーザー）。鳴らなかった電話のせいは、試行のせいの欄（17-1-5）を締めの処理が渡す（`blame`）。⛔ 4段目の前：番号の続き・
  せいを、読むたびにデータベースの salescall.failed_run・unrung_blame で導き直した。
- **切れ方の分からない通話のあとの確かめのかけ直し**（2026-10-30 管理者の決定。予定を書くのは、ここの1か所＝`after_call`。門は予定を
  読むだけ）：営業拒否の言葉が無く、切れ方が決められない通話（judge の切れ方 unclear）のあとは、すぐお客様へかけ直す（目的「続き」・
  約束の印・お詫びの印。同じ行で1回だけ＝表 salescall.unclear_checks）。そのかけ直しが、(1) お客様の電話まで鳴って、お客様が出て話さ
  なかった ⇒ 営業拒否（判定の1か所＝judge.refusal。発信禁止もそこ）(2) お客様が出て話を続けた ⇒ その話の結果どおり (3) 鳴らなかった
  （状態の表の E2。回線は表が確かめる）⇒ 回線のせいとして、テスト番号をあける長さの並びの1つ目（初め15分）おいて、お詫びからかけ直す
  （そのあとは途中で切れた電話のふつうの決まり＝確かめのかけ直しではない）。答えを読むのは `check_outcome` の1か所。
- **どの番号へかけるかも、ここだけが決める**（`choose_number`。DESIGN.md 3-5）。予定（かけ直す）は必ず番号を持ち、門はその番号へ
  かける。決め方：お客様が「別の番号にかけ直して」と言った ⇒ その番号（業務の設定で「かけない」・かけない理由がある ⇒ 終える。
  番号は記録して依頼者へ渡す）／それ以外 ⇒ いまかけた番号のまま（門で止められたあとは、いまの予定の番号のまま）。
  最初の予定（取込）は一覧の番号。
- **取り消せない決定の保留**（2026-10-30 管理者のご決定 (6)。保留にするかは core/approval.decision_wait の1か所）：お客様の断り・番号違い・
  怒っていたが、まだ決まらない（検証モードの承認待ち・2つ目の判定とそろわず録音の確かめ待ち）通話のあとは、行を終えずに判定待ち
  （`decide_after_call` の waiting）。答えが出たら、締めの処理が締め直して、ふつうの次の動きを書く。
- **行が終わったら**（`end`）、確定の印の無い結果の版を承認の部分が確かめ直す（core/approval.after_ended の1か所）。
- **誤って入れた発信禁止を取り消したら**（core/dnc の取り消しの1か所と、見守り役の決まった間隔の仕事が呼ぶ `reopen_revoked`）、発信禁止で
  門が終えた行のうち、もう止まっていないものを、終える前の予定に戻す（2026-10-30 管理者のご決定 (7)）。
- **担当の期**（0203）：その予定・その通話が、どの担当の方の期のものか（`person_since`。期の始まり＝お呼びする方が替わった時刻＝データベースの
  salescall.task_person_since）。予定の期は `_append` が `_plan_person` で決め、通話は予約するときデータベースが予定から入れる（0210）。前の期の予定がいまの期へ移るか＝
  `_plan_person` のもとの期との比べ、引き継ぐことがあるか＝`_carried`、引き継ぎを確かめる印＝`_handover_due`（予定を書くたびに求める）。
  前の期の約束・お詫びを、いまの期の予定に持ち込まない（新しい担当の方への形は `_onto_dial` の1か所。決定を書く `_write` が必ず通し、`_append` が
  通っていない予定を書かない＝0206）。お呼びする方の名前などを直したあとの予定は `after_person_fix`（番号の直しの `after_dial_fix` と同じ1か所
  `_after_fix`。終えた行を戻すかも同じ1か所 `reopens_on_fix`＝0208）。名前などの直しで替わるのは、中身が変わったときだけ（データベースの
  salescall.person_fix_changes。読みだけの直し・同じ値の入れ直しは替わらない＝0208。全角・半角・空白など表記の揺れだけの直しも替わらない＝0209。
  値が本当に変わっても、契約者が「担当の方は同じ」を選べば替わらない＝計画書 7-1 (28)・0210）。
- **前の担当の方への通話**（tasks.former_person_call の1か所＝通話の期が、締めたときの期と違う。0202・0203。✅ 2026-11-07 ユーザー＝計画書
  7-1 (22)(23)(24)）：前の担当の方個人の答え（断り・用件の断り）では行を終えず、新しい担当の方へ引き継ぎを確かめる電話（答えの分け方は
  `FORMER_ANSWERS`・`answer_ends_row`・`_after_former`。止める
  番号は core/dnc.refusal_reach）。前の担当の方が新しいかけ直しの約束をしたら、その約束は前の番号のまま書き、契約者に知らせて、前の担当の方
  との約束を守るか新しい担当の方へかけるかを選んでいただく（選ぶまで門が止める＝tasks.old_promise_waiting。選びは `after_old_promise_choice`）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import (approval, attempts, business_rules, business_settings, call_report, calling_hours, gate, health, notify,
                            number_recheck, settings, spoken_time, target_policy, tasks)
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
        "       window_day_said, window_recover_until, apology_owed, apology_reasons, cut_call, successor, number_again, handover,"
        "       decided_at, person_since"
        " FROM salescall.current_next_actions WHERE task_id = %s",   # いまの予定を選ぶのはデータベースの見方の1か所（0094）
        (task_id,),
    )


NUMBER_AGAIN = ("wrong_party", "unreachable")   # 番号を伺い直す理由：お客様が言った番号が別の方のお電話だった／繋がらなかった（0130）
PURPOSES = ("normal", "ask_number_again", "apology", "resume")   # 発信の目的：ふつう／番号の聞き直し（3-5）／お詫び（10-1-2）／
#                                                                   続き（途中で切れた通話のかけ直し。お詫びから始めて続ける）
# 営業の電話の目的（1か所。0201）：番号を直したら、いまかける番号へ替える（`current_dial`）・鳴らない番号から、お客様が言った別の番号・サブへ
# 替える（`usable`）・引き継ぎを確かめる印 handover を持てる（`_handover_due`。データベースの CHECK task_next_actions_handover_calls も同じ組＝
# salescall.sales_purpose。同じかは試験 tests.test_settle が見張る）。ほかの目的（`NUMBER_BOUND`）は、その番号のお客様へかける電話
SALES_PURPOSES = ("normal", "resume")


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
    # 引き継ぎを確かめる印（handover）は、かける先に持たせない：予定を書く1か所（`_append`）が `_handover_due` で求める（0203）


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
    # こちらの止めで約束の時刻にかけられなかった時刻ちょうどの約束の回を、その日のうちに取り戻す電話の期限（今日のお客様指定の歯止め時間の
    # 終わり。決めるのは `_recover_today` の1か所。✅ 2026-11-08 ユーザー＝計画書 7-3 (4)「復旧しだい今日」。0216）。この回の終わりは、until でなく
    # これ（門＝calling_hours.in_window・`_window_opening`・`_exact_next_try`・`_missed`）。次の回へは持ち越さない（`later`）
    recover_until: datetime | None = None

    @property
    def empty(self) -> bool:
        return self.until is None and self.daily is None and self.days is None and self.repeat is None

    @property
    def callee(self) -> calling_hours.CalleeWindow:
        """かけてよい時間帯を決める材料（門と同じ `gate.window_hours` に渡す）。"""
        return calling_hours.CalleeWindow(self.daily, self.days, self.said, self.day_said, self.recover_until is not None)

    @property
    def round_end(self) -> datetime | None:
        """いまの回の終わり（取り戻す電話の回は、その期限。1か所）。"""
        return self.recover_until or self.until

    def later(self) -> Window:
        """次の回からの幅（1か所）：日を言っていない次の日の回（「夕方なら」「明日の15時」の次の日・「すぐ」の次の日）は、お客様が
        その日を言っていない（依頼者のかける日だけ）。曜日・月の理由の回（「金曜なら」の次の金曜）と、日の種類を言った幅（「土日祝なら」）
        は、お客様が選んだ日のまま。取り戻す電話（recover_until）は、その回だけ（次の回は約束の時刻にかける）。"""
        w = replace(self, recover_until=None)
        return replace(w, day_said=False) if w.repeat == spoken_time.DAY_REPEAT and w.days is None else w


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
               None if row.get("window_said") is None else tuple(row["window_said"]), bool(row.get("window_day_said")),
               row.get("window_recover_until"))
    return None if w.empty else w


# お客様につながったら、まずお詫びすること（次の予定の印 apology_reasons。予約証で会話の処理へ渡す＝gate.Claim.apology。0113）
# 約束した日（回）に一度もかけられなかった／前の電話が途中で切れた／前の通話で言ってはいけないことを言った（お客様がかけ直しを約束していた：
# 約束の電話の頭で、契約者が承認したお詫びの案を伝える。✅ 2026-11-08 ユーザー＝計画書 7-3 (2)。付けるのは `_decide` の1か所。0216）
APOLOGY_REASONS = ("missed_promise", "cut_off", "forbidden_speech")
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
            apology: Apology = None, cut_call: UUID | None = None, after: UUID | None = None,
            former_promise: bool = False, origin: dict[str, Any] | None = None) -> int:
    """次の予定を1つ足す（1か所）。cut_call＝この予定でお詫びの理由 cut_off を付けた元の電話（試行。付けた所だけが渡す＝
    `decide_after_call`・`_check_unrung`）。渡さなければ、理由 cut_off を引き継ぐときに元の電話も前の予定から引き継ぐ（0187）。
    after＝この予定が、どの通話のあとの予定か（通話のあと＝`after_call`・`after_apology`、お詫びの電話＝言ってはいけないことを言った通話）。
    予定の担当の期（person_since）は `_plan_person`、引き継ぎを確かめる印（handover）は `_handover_due` の1か所で、ここが求める（呼ぶ側は
    渡さない。0203）。former_promise＝前の担当の方への通話（after）で、その方がした新しいかけ直しの約束（`after_call` だけが決める＝`_plan_person`）。
    origin＝この予定が続ける元の予定（いまの予定でないとき。前の担当の方に戻したときの、やめていた約束の予定＝決定の resumes。`_write` だけが渡す。0214）。"""
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
    since, origin = _plan_person(tx, task_id, origin or prev, target if calls else None, after, former_promise=former_promise,
                                 promised=bool(promised))
    handover = calls and target.purpose in SALES_PURPOSES and since == person_now(tx, task_id) and _handover_due(tx, task_id, since)
    reasons = _apology_reasons(tuple(prev["apology_reasons"]) if prev else (), apology)
    # 前の担当の方の期の約束・お詫び・その方への用件（番号の聞き直し・続き）を、いまの期の予定に持ち込まない（守り。0206）：前の期から
    # いまの期へ移る予定は、どの道でも新しい担当の方への形（`_onto_dial` の1か所。決定を書く `_write` が必ず通す）になっている。なっていなければ、
    # 道の呼び忘れなので書かない（前の担当の方との約束を新しい担当の方へかけるより、止まる側に倒す）
    if calls and since != origin and _carried(tx, task_id, since) and \
            (promised or reasons or target.purpose != "normal" or callee_at is not None or window is not None):
        raise ValueError("前の担当の方の期の約束・お詫び・その方への用件を、いまの担当の方の期の予定に持ち込めません"
                         "（新しい担当の方への形は next_action._onto_dial の1か所で決めます）")
    # お詫びの理由「途中で切れた」の元の電話（どの電話についてのお詫びか。予約証の apology_when＝同じ日か、を決める。0187）
    cut = (cut_call if cut_call is not None else prev and prev["cut_call"]) if "cut_off" in reasons else None
    if "cut_off" in reasons and cut is None:
        raise ValueError("お詫びの理由「途中で切れた」には、どの電話が切れたか（cut_call）が要ります")
    # 順番の番号（seq）はデータベースが付ける（next_seq の仕掛け＝0109。ここで「最大＋1」を数えない）。お詫びの印があるか
    # （apology_owed）は、データベースが理由（apology_reasons）から作る（0116）
    row = tx.one(
        "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, end_reason, cause,"
        "                                         promised, contact_id, purpose, wrong_contact_id, callee_at, window_until,"
        "                                         window_daily, window_days, window_from, window_repeat, window_round,"
        "                                         window_missed, window_said, window_day_said, apology_reasons, successor,"
        "                                         number_again, cut_call, handover, person_since, window_recover_until)"
        " SELECT t.tenant_id, t.client_id, t.task_id,"
        "        %s, CASE WHEN %s THEN now() ELSE %s::timestamptz END, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        "        %s, %s, %s, %s, %s, %s, %s, %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING seq",
        ("hold" if hold else "call" if calls else "end", call_now, call_at, end_reason, cause, promised,
         target and target.contact_id, target and target.purpose, target and target.wrong_contact_id, callee_at,
         window and window.until, None if window is None or window.daily is None else list(window.daily),
         None if window is None or window.days is None else list(window.days), window and window.since,
         window and window.repeat, 0 if window is None else window.round, 0 if window is None else window.missed,
         None if window is None or window.said is None else list(window.said), bool(window and window.day_said),
         list(reasons), bool(target and target.contact_id and target.successor),
         target.number_again if target is not None and target.purpose == "ask_number_again" else None, cut,
         handover, since, window and window.recover_until, task_id),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row["seq"]


def schedule_now(tx: Tx, task_id: UUID, *, cause: str) -> int:
    """いまから、一覧の番号へかけてよい（日時はデータベースの時刻。取込で登録したタスクの最初の予定）。
    実際にかけるか・いつかけるかは門が決める（時間帯・間隔・停止など）。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=False,
                   target=Target(tasks.dial_number(tx, task_id)["contact_id"]))


# かける番号を直したときに戻す、終えた行の終わりの理由（決めるのは `reopens_on_fix` の1か所。2026-11-06 ユーザー：「番号のせいで
# 終えた行、決めた回数までかけて終えた行は、新しい番号へすぐかけてよい予定に戻します。お客様が断った行・業務を終えた行は戻しません。」・
# 「お客様と約束したかけ直しで終えた行は、「決めた回数（日数）までかけて終えた行」であれば、新しい番号で再度0回からかけ直します。」）
NUMBER_ENDS = frozenset({
    "number_unusable",               # 鳴らない電話が続いた番号
    "alternate_wrong_again",         # お客様が言った番号を伺い直しても、また番号違い
    "alternate_not_called",          # お客様が言った番号へは、業務の設定でかけない（かける番号が無い）
    "ask_number_again_unanswered",   # 番号の伺い直しの電話に出なかった（正しい番号が分からない）
    "other_client_promise_never",                                            # 門：その番号への契約者の設定（ほかの約束の電話）
    "apology_wrong_party", "apology_fax_line",   # お詫びの電話の番号が番号違い・FAX の回線だった（その番号だけ止めた＝番号のせい。0194）
})
# 「かけない」とした理由（target_policy.block_reason の名前）のうち、お客様の断りでなく番号そのもののせいのもの（番号が読めない・
# かけない種類の番号・ほかの契約者がかけた番号への契約者の設定）。発信禁止（dnc_client・dnc_contractor）は登録の理由で決まる
# （`_blocked_by_number`）。管理番号の発信禁止（dnc_client_ref）・同じ業務で断られたお客様（declined_in_business）は、お客様の断り
BLOCKED_BY_NUMBER = frozenset({"number_unknown", "number_kind_forbidden", "number_kind_not_allowed", "other_contractor_called"})
ALTERNATE_BLOCKED = "alternate_blocked:"                      # お客様が言った番号が、かけない種類・発信禁止など（記録だけ）＋理由
COUNT_ENDS = frozenset({"max_attempts", "callback_exact_unreached", "callback_unspecified_time",   # 決めた回数（日数）まで
                        # 言ってはいけないことのあとのお詫びの電話を、決めた回数（retry.apology_max）かけても、お客様に届かなかった
                        # （✅ 2026-11-06 ユーザー＝計画書 7-1 (14)：番号を直したら「戻す」。戻し方は番号のせいで終えたお詫びの電話と
                        # 同じ＝`_restart_apology`。お詫びの電話の「届かなかった」終わりは、これだけ＝`decide_after_apology`。0195）
                        "apology_unreached"})
# お客様が「あとで」を、時刻の決まっていない約束の方針の回数より多く言って終えた（0194。前は callback_unspecified_time に入れていたが、
# 決めた回数かけて話せずに終えた行と、番号を直して戻すかの決まりが違う＝計画書 7-1 (13)。終える所は `_unspecified_next` の1か所）
LATER_OVER = "callback_later_over"
COUNT_END_PREFIXES = ("policy_max:",)                        # 再発信の方針の回数まで


def reopens_on_fix(tx: Tx, task_id: UUID, end_reason: str, *, dial: bool = True, same_person: bool = False) -> bool:
    """その終わりの理由で終えた行を、行の直しで戻すか（1か所。dial＝かける番号の直し＝`after_dial_fix`・`_moved_on_fix`。偽＝お呼びする方の
    名前などを「担当の方は違う」で直した＝`after_person_fix`。0208・0210。same_person＝名前などを「同じ」で直した＝値は変わったが担当の方は同じ。
    dial は偽。0213）。番号の直しと名前などの直しで別々に書かない：
    - その方個人の答え（`_PERSONAL_ENDS`＝用件の断り・ほかの手段・在籍していない など）で終えた行は、どちらの直しでも同じ1か所
      （`answer_still_ends`）：そのあとお呼びする方が替わっていれば戻す（新しい担当の方へ引き継ぎを確かめる電話）。「もうかけないで」と会社としての
      答えは戻さない（計画書 7-1 (24)(26)）。
    - 決めた回数（日数）までかけて終えた行（`_count_end`）は、どちらの直しでも戻す：番号の直しでは新しい番号でまだかけていない（7-1 (2)(4)）、
      「担当の方は違う」の直しでは新しい担当の方にまだかけていない（7-1 (8)「担当の相手が…違うのであれば、最初から」）。お客様が「あとで」を
      方針の回数より多く言って終えた行（`LATER_OVER`）も入る（✅ 2026-11-08 ユーザー＝計画書 7-1 (27)「戻す」：営業拒否と読んだ「あとで」は断りとして
      別に終わる＝判定が do_not_call にする＝7-1 (16)・0198 ので、回数で終えた行の「あとで」は約束の一環とみなす。「違う」の直しでは、前の担当の方の
      「あとで」は新しい担当の方の言葉でもない。⛔ 0209 まで：判定の中身 callback.intent の書いていない「あとで」を「まだ分からない」として戻さなかった＝
      `_later_refused`。消した）。
    - 番号のせいで終えた行（`NUMBER_ENDS`・かけない種類の番号など・番号違いと FAX の回線の対象外）は、番号の直しだけ（名前などの直しでは番号が
      変わらない）。
    - 名前などを「同じ」で直したとき（same_person）は、お呼びする方も番号も替わらないので、戻すのは、その方個人の答えで終えた行が、いまは
      終わったままでないときだけ（受付の方に「お名前だけではどの方か分かりかねます」と言われて終え、依頼者に埋めていただく欄を埋めた行＝
      `answer_still_ends`。✅ 2026-11-08 ユーザー＝計画書 7-1 (29)「埋めたらかけ直す」。0213）。決めた回数で終えた行は戻さない（同じ方に、同じ番号で、
      もう決めた回数かけた）。
    お客様が断った・業務を終えた・苦情・言ってはいけないことなどは戻さない。
    ここが見るのは終わりの理由だけ。お詫びの電話が番号のせい・決めた回数で終わった行を、どう戻すか（同じ相手へお詫び・違う相手へ営業を
    続ける・業務を終えていれば戻さない）は、戻す形を決める `_restart` の1か所。"""
    from salescall.core import dnc   # 発信禁止の部品が次の動きを読むので、ここで読む（読み合いにしない）
    if end_reason in _PERSONAL_ENDS:
        # その方個人の答え（用件の断り・ほかの手段・在籍していない など）で終えた行：そのあとお呼びする方が替わっていれば戻す（新しい担当の方へ
        # 引き継ぎを確かめる電話＝`_restart` が `_onto_dial` を通る）。決めるのは、その答えで終えた行が、いまも終わったままかの1か所
        # （`answer_still_ends`。「もうかけないで」と会社としての答えは戻さない）。✅ 2026-11-07 ユーザーの新しい決定（計画書 7-1 (24) にそろえる）。
        # 番号の直しでも名前などの直しでも、ここを通る（0208）
        r = tx.one("SELECT attempt_id, business_state::text AS state, detail ->> 'policy_key' AS policy_key,"
                   "       detail ->> 'out_of_scope' AS out_of_scope FROM salescall.latest_task_results WHERE task_id = %s", (task_id,))
        return r is not None and r["attempt_id"] is not None and not answer_still_ends(tx, r)
    if same_person:
        return False   # 同じ方の直し：お呼びする方も番号も替わらない（決めた回数・番号のせいで終えた行は戻さない）
    if _count_end(tx, task_id, end_reason):
        return True   # 決めた回数で終えた：新しい番号・新しい担当の方には、まだかけていない（番号の直しと名前などの直しで同じ）
    if not dial:
        return False   # 名前などの直しでは番号は変わらない：番号のせいで終えた行は戻さない
    if end_reason in NUMBER_ENDS:
        return True
    if end_reason.startswith(ALTERNATE_BLOCKED):
        # お客様が言った番号が「かけない」で止まって終えた行：門がその理由で終えた行と同じ決まり（⛔ 2026-11-06 の見直し：前は理由を
        # 見ずに戻し、お客様が断った番号（発信禁止・同じ業務で断られたお客様）で止まった行にも、直した番号へ営業の電話をかけえた）
        return _blocked_by_number(tx, task_id, end_reason[len(ALTERNATE_BLOCKED):])
    if end_reason in BLOCKED_BY_NUMBER or end_reason in ("dnc_client", "dnc_contractor"):
        return _blocked_by_number(tx, task_id, end_reason)
    if end_reason == "out_of_scope":
        # 対象外のうち、番号違い・FAX の回線（その番号だけ止めた）：その番号だけの理由の組は dnc.NUMBER_ONLY の1か所（⛔ 2026-11-06 の
        # 見直し：前は同じ組をここにも持っていた）
        r = tx.one("SELECT detail ->> 'out_of_scope' AS why FROM salescall.latest_task_results WHERE task_id = %s", (task_id,))
        return r is not None and r["why"] in dnc.NUMBER_ONLY
    return False


def _count_end(tx: Tx, task_id: UUID, end_reason: str) -> bool:
    """決めた回数（日数）までかけて終えた終わりの理由か（`reopens_on_fix` だけが読む）：`COUNT_ENDS`・再発信の方針の回数（`COUNT_END_PREFIXES`）・
    時刻の決まっていない約束の回数・「あとで」の回数（`LATER_OVER`。計画書 7-1 (27)）。ただし、時刻の決まっていない約束を、回数でなく契約者の
    再発信の方針が「終える」ので終えた行は入れない（いまの版の方針で見る）。"""
    if end_reason in ("callback_unspecified_time", LATER_OVER):
        return _unspecified_policy(tx, task_id).action != "end"
    return end_reason in COUNT_ENDS or end_reason.startswith(COUNT_END_PREFIXES)


def _blocked_by_number(tx: Tx, task_id: UUID, reason: str) -> bool:
    """「かけない」とした理由（target_policy.block_reason の名前。門が終えた行の終わりの理由と、お客様が言った番号が止まった
    `ALTERNATE_BLOCKED` の後ろ）が、お客様の断りでなく番号のせいか（1か所。番号を直したときに戻すか＝`reopens_on_fix` が読む）。
    - 番号そのもののせい（`BLOCKED_BY_NUMBER`）⇒ 番号のせい。
    - 発信禁止（依頼者・契約者の共通）⇒ その行のどの番号の登録も、その番号だけの理由（番号違い・FAX の回線＝dnc.NUMBER_ONLY）なら
      番号のせい。お客様が断った・手で登録した登録が1つでもあれば、お客様の断り（原則14：そのお客様が断っていれば、どの番号でかけても
      同じお客様。⛔ 見直し (A) の8 (3) の前は発信禁止で終えた行をすべて戻さず、そのあとは門の番号の登録だけを見ていた）。
    - それ以外（管理番号の発信禁止・同じ業務で断られたお客様）⇒ お客様の断り（戻さない）。"""
    if reason in BLOCKED_BY_NUMBER:
        return True
    if reason not in ("dnc_client", "dnc_contractor"):
        return False
    from salescall.core import dnc   # 発信禁止の部品が次の動きを読むので、ここで読む（読み合いにしない）
    return tx.one(   # 依頼者の台帳と契約者の共通の台帳の、その行の番号の登録（取り消していないもの）
        "SELECT bool_and(x.reason = ANY(%s)) AS only FROM ("
        "  SELECT e.reason::text FROM salescall.dnc_entries e JOIN salescall.task_numbers n ON n.phone_e164 = e.phone_e164"
        "   WHERE n.task_id = %s AND e.revoked_at IS NULL"
        "  UNION ALL SELECT c.reason FROM salescall.contractor_dnc_entries c JOIN salescall.task_numbers n ON n.phone_e164 = c.phone_e164"
        "   WHERE n.task_id = %s AND c.revoked_at IS NULL) x",
        (list(dnc.NUMBER_ONLY), task_id, task_id))["only"] is True


def _laters(tx: Tx, task_id: UUID) -> list[str | None]:
    """数え始め以降にお客様が「あとで」と言った通話の、約束の一環か（判定の中身 callback.intent。promise か None＝まだ分からない）の並び
    （1か所。読むのは「あとで」と言われた回数＝`_unspecified_next`＝その数で行を終える）。「あとで」＝判定が時刻の決まっていない約束（再発信の方針
    callback_unspecified_time）にした通話（通話ごとに最新の判定の版＝見方 call_judgements）。範囲は数え始め以降の、どの番号への電話も
    （attempts.count_start。番号を直したあと前の番号へかけた電話で言われた「あとで」も入る：お客様の言葉は、どの番号で言われても同じお客様の言葉）。
    いまの担当の方の期の通話だけ（前の担当の方の「あとで」は、新しい担当の方の言葉ではない。その通話が、いまの期のものかはデータベースの1か所
    salescall.attempt_in_period。0203・0210）。
    ⛔ 0209 まで：「あとで」を回数より多く言って終えた行を番号の直しで戻すか（`_later_refused`）も、この並びの意味（intent）を読み、まだ分からない
    「あとで」があれば戻さなかった ⇒ 計画書 7-1 (27) で、回数で終えた行の「あとで」は約束の一環とみなす（`reopens_on_fix`）ので、読むのは回数だけ。"""
    rows = tx.all(
        "SELECT j.detail -> 'callback' ->> 'intent' AS intent FROM salescall.call_judgements j"
        " JOIN salescall.attempts a ON a.attempt_id = j.attempt_id"
        " WHERE j.task_id = %s AND j.detail ->> 'policy_key' = 'callback_unspecified_time'"
        f" AND {attempts.count_start('a')}"
        # いまの担当の方の期の通話だけ（前の担当の方の「あとで」は、新しい担当の方の言葉ではない。0203。⛔ 0202：数えた＝指摘7）
        " AND salescall.attempt_in_period(a.attempt_id, now())",
        (task_id,))
    return [r["intent"] for r in rows]


_PLAN_COLUMNS = ("n.seq, n.contact_id, n.kind, n.call_at, n.promised, n.purpose, n.wrong_contact_id, n.callee_at, n.window_until,"
                 " n.window_daily, n.window_days, n.window_from, n.window_repeat, n.window_round, n.window_missed, n.window_said,"
                 " n.window_day_said, n.window_recover_until, n.apology_reasons, n.cut_call, n.successor, n.number_again, n.handover,"
                 " n.decided_at, n.person_since, k.phone_e164")


def _last_call_plan(tx: Tx, task_id: UUID, before_seq: int | None = None) -> dict[str, Any] | None:
    """その行の、いちばん新しい「かけ直す」の予定（before_seq より前。その番号＝phone_e164 も。1か所＝終えた行を戻すとき・止めた行を
    戻すとき・続きの電話が守っていた予定を読むとき）。"""
    return tx.one(f"SELECT {_PLAN_COLUMNS} FROM salescall.task_next_actions n"
                  " JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
                  " WHERE n.task_id = %s AND n.kind = 'call'"
                  " AND (%s::int IS NULL OR n.seq < %s) ORDER BY n.seq DESC LIMIT 1", (task_id, before_seq, before_seq))


def _plan_target(row: dict[str, Any]) -> Target:
    return Target(row["contact_id"], None, row["purpose"], row["wrong_contact_id"], bool(row["successor"]), row["number_again"])


def _is_promise(tx: Tx, row: dict[str, Any] | None) -> bool:
    """その予定が、お客様と約束したかけ直しの電話か（決めるのはデータベースの salescall.promise_call の1か所＝目的 normal で約束の印。
    サブを作る通話＝salescall.call_makes_sub も同じ関数を読む。0193）。続きの電話（目的 resume）は約束の印が付いても約束の電話ではない
    （✅ 2026-11-06 ユーザー：サブは「約束の電話だけ」と伺ったとき、続きの電話は約束の外として伺った）。⛔ 2026-11-06 の見直し：前は
    ここが「約束の印で目的 normal か resume」、データベースが「目的 normal で約束の印」と、2つの決め方があった。"""
    return row is not None and bool(tx.one("SELECT salescall.promise_call(%s, %s) AS p", (row["purpose"], row["promised"]))["p"])


def _served_plan(tx: Tx, task_id: UUID, row: dict[str, Any] | None) -> dict[str, Any] | None:
    """続きの電話の予定（目的 resume）は、切れた電話が守っていた予定（その前の、続きでない「かけ直す」の予定）に読み替える（1か所。
    終えた行をもう一度かけ始めるとき＝`_restart`）。続きの電話そのものは約束の電話ではないが、約束の電話が切れたあとの続きなら、
    その約束を守っている（約束のまま戻す。ふつうの電話の続きなら、ふつうの電話で戻す）。"""
    while row is not None and row["purpose"] == "resume":
        row = _last_call_plan(tx, task_id, row["seq"])
    return row


def _promise_tried(tx: Tx, task_id: UUID) -> bool:
    """いまの約束の電話を、もうかけたか（約束の始まりのあとに、お客様のところで鳴った電話がある＝`attempts.rang` と同じデータベースの
    salescall.rang。決めるのは salescall.task_promise_tried の1か所＝0201）。"""
    return bool(tx.one("SELECT salescall.task_promise_tried(%s) AS t", (task_id,))["t"])


def promise_alive(tx: Tx, task_id: UUID) -> bool:
    """お客様と約束したかけ直しが残っているか（1か所＝データベースの salescall.task_promise_alive。0203）：いまの予定が、お客様と約束したかけ直しの
    電話（`_is_promise`。約束の日・幅がまだ残っている）。番号を直すとき「担当の方は違う」を選び、これが真なら、契約者に、前の担当の方との約束も
    守るか・次の担当の方にだけかけるかを選んでいただく（✅ 2026-11-07 ユーザー＝計画書 7-1 (21)。✅ 2026-11-07 ユーザーの新しい決定：まだ1回も
    かけていない約束だけでなく、1回かけたあとでも、約束の日・幅が残っていれば選ぶ。選びが要るかはデータベースの
    salescall.task_old_promise_choice_needed の1か所＝core/row_fixes.fix_dial_number と守り task_dial_fix_guard が読む）。⛔ 0201〜0203 の初めの形：
    「まだかけていない約束」（task_promise_open）で見て、1回かけたあとは選ばずに約束をやめた。"""
    return bool(tx.one("SELECT salescall.task_promise_alive(%s) AS a", (task_id,))["a"])


def _old_promise_dropped(tx: Tx, task_id: UUID) -> bool:
    """いまの約束に対する、いちばん新しい選びが「次の担当の方にだけかける」か（1か所＝データベースの salescall.task_promise_choice。選び待ち
    task_old_promise_waiting・まだかけていない約束 task_promise_tried と同じ数え方。見直し H の (B1)。⛔ 0203 の初めの形まで：ここは「約束の始まり
    より後のいちばん新しい選び」、データベースは「前の担当の方が新しい約束をした通話を締めたあとの選び」と別に数え、逆に判断しえた）。選んで
    いれば、前の担当の方との約束をやめて、新しい番号の担当の方へ、すぐかけてよい引き継ぎを確かめる電話にする（`_point` が前の番号に残さず、
    前の番号からいまかける番号へ移る1か所 `_onto_dial` が形を決める）。「守る」を選んだときは、いまの決まりのまま（`_point`）。"""
    last = tx.one("SELECT keep_old_promise FROM salescall.task_promise_choice(%s)", (task_id,))
    return last is not None and not last["keep_old_promise"]


# その番号のお客様へかける電話の目的（番号を直しても `current_dial` が、いまかける番号へ替えない＝番号の聞き直し：その番号を言われた
# 通話の番号へ・お詫び：その番号で電話を受けたお客様へ）。直したあとは、前の番号へ1回まで（`_old_number_moves`）
NUMBER_BOUND = tuple(p for p in PURPOSES if p not in SALES_PURPOSES)


def _old_number_moves(tx: Tx, task_id: UUID, t: Target | None, at: datetime, *, called: bool,
                      row: dict[str, Any] | None = None) -> bool:
    """前にかける番号だった番号への、その番号のお客様へかける電話（`NUMBER_BOUND`＝番号の聞き直し・お詫び）を、いまかける番号へ移すか
    （1か所。✅ 2026-11-06 ユーザー：伺い「直したあとのお詫び」に「約束の電話と同じ」＝計画書 7-1 (4)「まだ約束の電話をかけていない場合は、
    旧番号でかからなければ、新番号でかけます」・(17)。✅ 2026-11-07 ユーザー：伺い「聞き直し」に「お詫びと同じ」＝計画書 7-1 (18)。
    ⛔ 0195 の初めの作りは、前の番号への聞き直しを直したときから0回で数え直し、同じお客様へ決めた回数をもう一度かけた）：
    その電話の番号が、その時刻（at）に前にかける番号だった番号（`replaced_at`）で、その番号へ同じ目的の電話を、
    もうかけた：番号を直したとき（`after_dial_fix`）は、その番号へのお詫びの電話がもうお客様のところで鳴ったか（`attempts.rang`＝約束の
    電話の `_promise_tried` と同じ物差し）。お詫びの電話のあと（`_moved_on_fix`。called＝いまの電話を、前にかける番号だった番号へかけた）は、
    鳴っても鳴らなくても1本かけたので移す（約束の電話も、直したあと前の番号へ1本かければ、結果によらず次は新しい番号＝`current_dial`。
    ⛔ 2026-11-06 の見直し：前は鳴ったかだけを見て、鳴らなかったら前の番号へ決まった順でかけ続けた。回線のせいのかけ直しは、
    `_moved_on_fix` より前に `_line_fault_again` が決める＝かけなかったのと同じ）。まだかけていなければ、前の番号で1回かける。
    移し方は `_restart` の1か所（お詫び：同じ相手ならお詫び・違う相手ならお詫びなしで営業を続ける。番号の聞き直し：いまかける番号への
    ふつうの電話＝契約者が直した番号を正しい番号とみなす）。
    「前の番号へは1回」は、同じ担当の方の番号の直し（計画書 7-1 (17)(18)）だけ。「担当の方は違う」を選んで直したあとの、前の担当の方の期の
    予定（row の期が、いまの期と違う）の、前の担当の方へのお詫び・番号の聞き直しは、名前などの直しと同じく、決めた回数まで前の担当の方へかけ、
    使い切ったら新しい担当の方へ（`_after_former` の1か所。✅ 2026-11-08 本体の決定＝計画書 7-1 (32) にそろえる。⛔ 0201〜0215：番号の直しだけ
    前の番号へ1回で新しい担当の方へ移った。0216）。"""
    if t is None or t.purpose not in NUMBER_BOUND or t.contact_id is None or not replaced_at(tx, task_id, t.contact_id, at):
        return False
    if row is not None and row["person_since"] != person_now(tx, task_id):
        return False   # 前の担当の方の期の予定：決めた回数まで、その方へ（`_after_former`）
    return called or tx.one(f"SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = %s AND a.contact_id = %s"
                            f" AND a.purpose = %s AND {attempts.rang('a')}) AS e", (task_id, t.contact_id, t.purpose))["e"]


def _point(tx: Tx, task_id: UUID, row: dict[str, Any], numbers: Numbers) -> tuple[Target, Window | None]:
    """かけ直すの予定を、いまの番号へ向ける（1か所。番号を直したとき・止めた行を戻すとき・回線のせいでかけた予定のままかけ直すとき）。
    約束していない予定は `current_dial`。お客様と約束した電話は、まだかけていなければ前の番号のまま（2026-11-06 ユーザー：「まだ約束の
    電話をかけていない場合は、旧番号でかからなければ、新番号でかけます」）、もうかけていれば（⛔ 見直し (A) の8 (2)：前は、かけたあと
    でも前の番号のままだった）いまかける番号へ移し、約束の回を0から数え直す（「数え直す（お客様との約束は変更しない）」）。
    続きの電話（目的 resume）は約束の電話ではないので（`_is_promise`）、いまかける番号へ替える（設計書 3-4-1「続きの電話も、いまかける
    番号に替える」。⛔ 2026-11-06 の見直し：前は約束の印だけを見て、前の番号のままにした）。続きの電話が約束の幅を持っていれば（約束の
    電話が切れたあとの続き）、移るときに約束の回を0から数え直す（`recount_on_switch` の1か所）。
    契約者が番号・名前などを直して「次の担当の方にだけかける」を選んでいれば（`_old_promise_dropped`。計画書 7-1 (21)(25)）、まだかけていない
    約束でも1回かけた約束でも、約束の番号に残さず、いまかける番号へ向ける（約束をやめて引き継ぎを確かめる形にするのは `_onto_dial` の1か所。
    ⛔ 0205 まで：まだかけていない約束だけ見ていたので、1回かけた約束が前の担当の方の言った番号（前にかける番号でない番号）にあると、選んでも
    その番号のまま前の担当の方の期に残った。名前などの直しでは、前の担当の方の番号は前にかける番号にならないので、いつもそうなった）。"""
    t, window = _plan_target(row), window_of(row)
    was_replaced = t.contact_id in numbers.superseded
    if _is_promise(tx, row) and numbers.dial != t.contact_id and _old_promise_dropped(tx, task_id):
        # 契約者が「次の担当の方にだけかける」を選んだ：前の担当の方との約束をやめ、いまかける番号へ（形は `_onto_dial` が決める）
        return replace(t, contact_id=numbers.dial), None
    if _is_promise(tx, row) and was_replaced and (not _promise_tried(tx, task_id) or _kept_old_promise(tx, task_id, row)):
        # まだかけていない約束は、まず前の番号で（計画書 7-1 (4)）。契約者が「守る」を選んだ前の担当の方との約束は、かけたあとも、約束の決まり
        # いっぱいまで前の担当の方の番号で（計画書 7-1 (32)。`_kept_old_promise` の1か所。0214）
        return t, window
    moved = current_dial(t, numbers, called=False)
    return moved, recount_on_switch(window, was_replaced, moved, numbers)


def person_now(tx: Tx, task_id: UUID) -> datetime | None:
    """いまの担当の期の始まり（データベースの1か所 salescall.task_person_since。0203）：その行で、お呼びする方が替わった（「担当の方は違う」を
    選んだ番号の直し・お呼びする方の名前／フルネーム／役職／部署の直し＝計画書 7-1 (19)）いちばん新しい時刻。最初の担当の方の期は None。"""
    return tx.one("SELECT salescall.task_person_since(%s, now()) AS s", (task_id,))["s"]


def _carried(tx: Tx, task_id: UUID, since: datetime | None) -> bool:
    """前の期（いまの期 since より前の担当の方の期）で、行に引き継ぐことがあるか（1か所。新しい担当の方へ移るか＝`_onto_dial` と、引き継ぎを
    確かめる印＝`_handover_due` が読む）：前の期の通話でお客様と話した（会話の報告の1か所＝call_report.talked_in。受付の方と話した通話も入る）か、
    前の期の予定に、お客様と約束したかけ直しがある（お詫び＝言ってはいけないことのあとのお詫び・途中で切れたお詫びは、どれも前の期の話した通話から
    生まれるので、話した通話で足りる。条件に書かない）。
    ⛔ 0202 まで：いちばん新しい「話した通話」の時刻を使い（`_last_talked_at`）、直したあと前の番号で前の会社の受付の方と話すと、引き継ぐことが
    無いとみなした（第三者の見直しの指摘3）。最初の担当の方の期（since が None）でも、前の担当の方に戻した（計画書 7-1 (30)。0213）なら、ほかの期が
    ある（替わったことが無ければ、ほかの期の予定・通話は無い）。⛔ 0203〜0212：最初の期なら引き継ぐことは無いとしていた（戻すことが無かった）。"""
    if tx.one("SELECT EXISTS (SELECT 1 FROM salescall.task_next_actions n WHERE n.task_id = %s AND n.person_since IS DISTINCT FROM %s"
              "  AND salescall.promise_call(n.purpose, n.promised)) AS c", (task_id, since))["c"]:
        return True
    return any(call_report.talked_in(tx, r["attempt_id"]) for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND person_since IS DISTINCT FROM %s"
        " ORDER BY claimed_at DESC", (task_id, since)))


def _handover_due(tx: Tx, task_id: UUID, since: datetime | None) -> bool:
    """引き継ぎを確かめる印（予約証の handover。CL-1 が読む。計画書 7-1 (12)・0194）を、いまの期（since）の営業の電話の予定に付けるか（1か所。
    予定を書く `_append` だけが呼ぶ）：いまの期が最初の期でなく、前の期で行に引き継ぐことがあり（`_carried`）、いまの期の通話で目的のお客様と
    まだ話していない（締めの処理が試行に写した attempts.reached_target＝judge.JudgeInput.reached_target。受付の方とだけ話した通話は入らない）。
    ⛔ 0202 まで：かけた電話の予定の印を写すだけ（`_handover`）で、印を持てない番号の聞き直しをはさむと消えた（第三者の見直しの指摘4）。
    最初の担当の方の期（since が None）でも、前の担当の方に戻したときは同じ（`_carried`。0213）。"""
    if tx.one("SELECT EXISTS (SELECT 1 FROM salescall.attempts WHERE task_id = %s AND person_since IS NOT DISTINCT FROM %s"
              " AND reached_target) AS r", (task_id, since))["r"]:
        return False
    return _carried(tx, task_id, since)


def _plan_person(tx: Tx, task_id: UUID, prev: dict[str, Any] | None, target: Target | None,
                 after: UUID | None, *, former_promise: bool = False, promised: bool = False) -> tuple[datetime | None, datetime | None]:
    """書く予定の担当の期と、もとの期（1か所。予定を書く `_append` と、新しい担当の方への形にするか＝`_onto_dial` が読む。0203）。
    もとの期＝その予定が続く元：通話のあとの予定は、その通話（after）の期・お詫びの電話の予定は、言ってはいけないことを言った通話
    （`schedule_apology` が after に渡す）の期・ほかは、いまの予定（prev）の期（どれも無ければ、いまの期）。
    - もとの期が、いまの期なら、いまの期。
    - もとの期が前の担当の方の期（お呼びする方が替わる前）で、かける先が前の担当の方の番号（salescall.task_former_number）なら、もとの期のまま
      （前の担当の方との続き：守った約束・その方の新しい約束・その方へのお詫び・その方の通話で言われた番号への聞き直し・前の番号に残る動き）。
    - その番号のお客様へかける電話（`NUMBER_BOUND`＝お詫び・番号の聞き直し）で、もとの通話・予定と同じ番号へかけるものも、もとの期のまま
      （その番号で電話を受けた前の担当の方への電話。0206：名前などの直しでは番号が変わらず、いまかける番号が前の担当の方と話した番号でもある。
      ⛔ 0205 まで：いまかける番号はいまの担当の方の番号なので、前の担当の方へのお詫びのかけ直しが、新しい担当の方への期の予定になった）。
    - そのほか（いまかける番号・いまの担当の方の番号へ向いた予定）は、いまの期（前の期から移るときは `_onto_dial` が新しい担当の方への形にする）。
    - 終える・判定待ちの記録は、もとの期のまま（続きの元を残す）。
    - 前の担当の方への通話で、その方がした新しいかけ直しの約束（former_promise＝`after_call` の1か所が決める）も、もとの期のまま（契約者の選びを
      待つ＝計画書 7-1 (23)。salescall.task_old_promise_waiting）。かける先が、いまの担当の方の番号でも同じ（名前などを「違う」で直したあとは、
      前の担当の方と話した番号が、いまかける番号でもある。⛔ 0209 まで：通話の途中で名前などを「違う」で直し、前の担当の方が「あとで」と
      約束すると、その約束が契約者への知らせも無く新しい担当の方への形になった＝見直し J2）。
    - 契約者が「約束も守る」を選んだ、もとの期の約束（promised＝お客様と約束したかけ直し。いまの約束に対する、いちばん新しい選び＝データベースの
      salescall.task_promise_choice が「守る」）で、その番号で、もとの期の担当の方にお呼びできる（salescall.task_number_reaches＝「守る」を選べるかの
      1か所 task_promise_keepable と同じ物差し）なら、もとの期のまま（✅ 本体の決定 2026-11-08：名前などの直しでは、直す前からの会社の番号＝
      いまかける番号でも「守る」を選べる。会話は、その通話の期のお名前でお呼びする。0213）。
    ⛔ 0202 まで：期を持たず、通話の時刻と番号から推し量っていた（新しい担当の方が指定した前の番号への通話を、前の担当の方への通話とした＝指摘1）。"""
    now_since = person_now(tx, task_id)
    if after is not None:
        o = tx.one("SELECT person_since, contact_id FROM salescall.attempts WHERE attempt_id = %s", (after,))
    elif prev is not None:
        o = prev
    else:
        return now_since, now_since
    origin = o["person_since"]
    if origin == now_since:
        return now_since, origin
    if target is None or target.contact_id is None or former_promise:
        return origin, origin
    if target.purpose in NUMBER_BOUND and target.contact_id == o["contact_id"]:
        return origin, origin
    if _keeps_old_promise(tx, task_id, target, promised, origin):
        return origin, origin
    former = tx.one("SELECT salescall.task_former_number(%s, %s, now()) AS f", (task_id, target.contact_id))["f"]
    return (origin if former else now_since), origin


def _keeps_old_promise(tx: Tx, task_id: UUID, target: Target, promised: bool, origin: datetime | None) -> bool:
    """その予定が、契約者が「約束も守る」を選んだ、もとの期（origin＝前の担当の方の期）の約束の続きか（`_plan_person` だけが読む。0213）：お客様と
    約束したかけ直し（目的 normal で約束の印＝データベースの salescall.promise_call）で、いまの約束に対する、いちばん新しい選びが「守る」
    （salescall.task_promise_choice の1か所＝`_old_promise_dropped` と同じ数え方）で、その番号で、もとの期の担当の方にお呼びできる
    （salescall.task_number_reaches の1か所）。"""
    if not tx.one("SELECT salescall.promise_call(%s, %s) AS p", (target.purpose, promised))["p"]:
        return False
    last = tx.one("SELECT keep_old_promise FROM salescall.task_promise_choice(%s)", (task_id,))
    return last is not None and bool(last["keep_old_promise"]) and bool(tx.one(
        "SELECT salescall.task_number_reaches(%s, %s, %s::timestamptz) AS r", (task_id, target.contact_id, origin))["r"])


def _kept_old_promise(tx: Tx, task_id: UUID, row: dict[str, Any] | None) -> bool:
    """その予定（row）が、契約者が「守る」を選んだ前の担当の方との約束の電話か（1か所。0214）：前の担当の方の期（予定の期が、いまの期と違う）の、
    お客様と約束したかけ直しで、`_keeps_old_promise` が真（いちばん新しい選びが「守る」・その番号で前の担当の方にお呼びできる）。
    ✅ 2026-11-08 ユーザー＝計画書 7-1 (32)「約束の幅いっぱい → 鈴木様」：この約束は、約束の決まり（幅・回・日数）いっぱいまで前の担当の方の番号へ
    かける（通話のあとの番号の選び＝`after_call` の keep_former・予定の向け直し＝`_point`。番号の直しの「前の番号へは1回」＝計画書 7-1 (4)(17) は、
    同じ担当の方の決まりなので当てない）。使い切ったら新しい担当の方へ（`_after_former`）。番号の直しも名前などの直しも同じ。"""
    return row is not None and row.get("kind", "call") == "call" and row["person_since"] != person_now(tx, task_id) \
        and _is_promise(tx, row) and _keeps_old_promise(tx, task_id, _plan_target(row), True, row["person_since"])


def _onto_dial(tx: Tx, task_id: UUID, row: dict[str, Any] | None, d: Decision, *, now: datetime,
               after: UUID | None = None) -> Decision | None:
    """予定（d＝同じ相手のときの形）が、前の担当の方の期から、いまの担当の方の期の予定へ移るとき（`_plan_person` の1か所が、もとの期＝元の予定
    row・通話 after の期と違う、いまの期を返す＝かける先が、いまの担当の方の番号）、前の期で行に引き継ぐことがあれば（`_carried`）、前のお客様との
    ことを引き継がない（1か所。どの道もここを通る：決定を書く `_write` が必ず通し、ほかに番号を直したとき＝`after_dial_fix`・名前などを直したとき＝
    `after_person_fix`・終えた行を戻すとき＝`_restart`・前の番号でかからずに移るとき＝`_moved_on_fix`（通話のあと・お詫びの電話のあと・門のあと）・
    回線のせいでかけた予定のままかけ直すとき＝`_line_fault_again`・`_check_unrung`・止めた行を戻すとき＝`reopen_revoked` が、営業を続けるか（None）を
    見るために先に通す。予定を書く `_append` が、通っていない予定を書かない＝守り）。形は `_to_new_person`（✅ 2026-11-06 ユーザー＝計画書 7-1 (8)
    「担当の相手が電話番号で違うのであれば、最初から」・(12)「違う相手の場合は、お詫びなしで、どこまで引き継がれているのかを確認しつつ、続きからか、
    途中から営業を続けます」・(19)）。同じ相手（期が同じ）なら d のまま（約束のまま新しい番号で数え直す＝計画書 7-1 (4)(6)。数え直すのは
    `recount_on_switch`・`_restart`）。引き継ぐことが無ければ（一度も話していない・約束もお詫びも無い）d のまま（いまの期の予定になる）。
    もう新しい担当の方への形の予定は、そのまま（同じ形を二度作らない）。
    ⛔ 0205 まで：かける先が「いまかける番号」のときだけ見ていた。番号が使えないときの代わり（`usable`）などで、いまかける番号でない、いまの担当の方
    の番号へ移る予定は、ここを通らず、前の担当の方との約束の印が残りえた。予定がもう新しい番号にあるとき（前の担当の方が言った番号へ「違う」で
    直した など）も、期で見るので同じ（⛔ 0202 は、前の番号から移ったかと直しの種類で分けていた）。"""
    if d.kind != "call" or d.target is None or d.target.contact_id is None:
        return d
    since, origin = _plan_person(tx, task_id, row, d.target, after, former_promise=d.former_promise, promised=bool(d.promised))
    if since == origin or not _carried(tx, task_id, since) or _new_person_form(d):
        return d
    return _to_new_person(tx, task_id, row, d.rule, now=now, contact=d.target.contact_id)


def _new_person_form(d: Decision) -> bool:
    """その予定が、もう新しい担当の方への形か（`_to_new_person` の形：約束・時刻・幅・お詫びの印の無い、ふつうの営業の電話）。"""
    return not d.promised and d.target is not None and d.target.purpose == "normal" and d.apology is False and d.callee_at is None \
        and d.window is None


def _to_new_person(tx: Tx, task_id: UUID, row: dict[str, Any] | None, rule: str, *, now: datetime,
                   contact: UUID | None = None) -> Decision | None:
    """新しい担当の方への形（1か所。`_onto_dial`・前の担当の方個人の答えのあと＝`_after_former`・前の担当の方へのお詫びの電話のあと＝
    `after_apology`・前の担当の方の新しい約束で契約者が新しい担当の方を選んだとき＝`after_old_promise_choice`）：約束・お詫びを引き継がない、
    すぐかけてよいふつうの電話（いまの期の予定。引き継ぎを確かめる印は `_append` が `_handover_due` で付ける）。かける番号は contact（`_onto_dial`
    が決めた、いまの担当の方の番号）か、いまかける番号。ただし、前の予定（row）が言ってはいけないことのあとのお詫びの電話で、その言葉を言った
    通話の答えで行を終えた（`source_ends_row`）なら、営業を続けないので None（計画書 7-1 (2)）。"""
    from salescall.core import apology   # お詫びの部品が次の動きを読むので、ここで読む（読み合いにしない）
    if row is not None and row["purpose"] == "apology" and source_ends_row(tx, apology.source_call(tx, task_id)):
        return None
    to = contact if contact is not None else tasks.dial_number(tx, task_id)["contact_id"]
    return _call(now, False, rule + "（お呼びする方が替わった：約束・お詫びを引き継がず、引き継ぎを確かめつつ営業を続ける）",
                 Target(to), apology=False, cut_call=None)


# 前の担当の方への通話（tasks.former_person_call）の答えの分け方（1か所。✅ 2026-11-07 ユーザー＝計画書 7-1 (22)「鈴木様へはかける」・(23)・(24)
# 「新しい担当の方へは引き継ぎを確かめる電話。ただし『対象外』『目的を果たした』など会社としての答えは行を終える」）。判定の状態の名前を
# すべて当てはめる（results.BusinessState と同じ組かは試験が見張る）：
#   personal＝その方個人の答え ⇒ 行を終えず、新しい担当の方へ引き継ぎを確かめる電話（`_after_former`）
#   company＝会社としての答え ⇒ いままでどおり行を終える
#   number＝番号の答え（番号違い・FAX）⇒ その番号だけ止め、番号のせいの終わりと同じく新しい番号へ（`_moved_on_fix`。計画書 7-1 (2)(7)）
#   promise＝新しいかけ直しの約束 ⇒ 契約者が選ぶ（7-1 (23)。`after_call`）
#   continue＝まだ続く（出なかった・繋がらなかった・取れていない）⇒ 新しい担当の方へ引き継ぎ（`_onto_dial`）
#   undecided＝判定できない ⇒ 判定待ち
FORMER_ANSWERS: dict[str, str] = {
    "do_not_call": "personal",         # 「もうかけないで」（止めるのは前の担当の方の番号だけ＝core/dnc.refusal_reach。7-1 (22)）
    "declined": "personal",            # 用件・最終目的の断り（「うちは要らない」。発信禁止ではない。7-1 (24)）
    "completed": "company",            # 目的を果たした
    "concluded": "company",            # 結論として終えた（取れていない必須の質問の扱いが「結論として終える」・ご納得いただけず担当の者に申し伝える）
    "out_of_scope": "company",         # 範囲外の求め（対象外）。番号違い・FAX（判定の中身 out_of_scope）は番号の答え＝`FORMER_NUMBER_ANSWERS`
    "callback_scheduled": "promise",   # 新しいかけ直しの約束
    "open": "continue", "not_reached": "continue", "incomplete": "continue",
    "unknown": "undecided",
}
FORMER_NUMBER_ANSWERS = frozenset({"wrong_party", "fax_line"})
# その方個人の答えで行を終える決定の、終わりの理由（`FORMER_ANSWERS` から作る。言ってはいけないこと・苦情など、ほかの理由で終える決定は変えない）
_PERSONAL_ENDS = frozenset(s for s, a in FORMER_ANSWERS.items() if a == "personal") | business_rules.SYSTEM_ENDS


def former_answer(state: str, *, key: str | None = None, out_of_scope: str | None = None) -> str:
    """前の担当の方への通話の答えの種類（`FORMER_ANSWERS` の1か所）。key＝再発信の方針の鍵（判定の中身 policy_key）：繋がらなかった・取れて
    いない結果でも、その方の側の事情で電話ではかけ直さない結果（`business_rules.SYSTEM_ENDS`＝ほかの手段を望まれた・在籍していない・お名前
    だけではどの方か分からない）は、その方個人の答え。out_of_scope＝判定の中身の out_of_scope（番号違い・FAX は番号の答え）。"""
    if state == "out_of_scope" and out_of_scope in FORMER_NUMBER_ANSWERS:
        return "number"
    if state in ("not_reached", "incomplete") and key in business_rules.SYSTEM_ENDS:
        return "personal"
    return FORMER_ANSWERS[state]


def judged_answer(j: Judgement) -> str:
    """判定から、前の担当の方への通話の答えの種類（`former_answer` に判定の中身を渡す1か所。通話のあと・締めの結果の版の印が読む）。"""
    return former_answer(j.state, key=j.detail.get("policy_key"), out_of_scope=j.detail.get("out_of_scope"))


def answer_ends_row(tx: Tx, attempt_id: UUID, answer: str) -> bool:
    """その通話の答え（`former_answer` の種類）で行を終えるか（1か所。締めの処理 core/settle の `_ends_row`・`_after_former`・お詫びの元の
    通話の `source_ends_row` が読む）。その通話が前の担当の方への通話（tasks.former_person_call の1か所＝通話の担当の期が、締めたときの期と
    違う。0203）で、その方個人の答え（personal）なら終えない：新しい担当の方へは引き継ぎを確かめる電話をかける（✅ 2026-11-07 ユーザー＝
    計画書 7-1 (22)(24)。断りで止める番号は同じ1か所を読む core/dnc.refusal_reach）。ほかは、いままでどおり。"""
    return answer != "personal" or not tasks.former_person_call(tx, attempt_id)


def answer_still_ends(tx: Tx, call: dict[str, Any]) -> bool:
    """その通話（call＝attempt_id・判定の状態 state・方針の鍵 policy_key・対象外の中身 out_of_scope）の答えで終える行が、いまも終わったままか
    （1か所。番号を直して戻すか＝`reopens_on_fix`・お詫びの元の通話＝`source_ends_row` が読む。答えの種類は `former_answer` の1か所）：
    - 会社としての答え・番号の答えなど（personal でない）⇒ 終わったまま。
    - その方個人の答え（用件の断り・ほかの手段・在籍していない など）⇒ その通話の担当の期が、いまの期なら終わったまま。お呼びする方が替わった
      （その通話が前の担当の方の期）なら、終わったままにしない（新しい担当の方へ引き継ぎを確かめる。✅ 2026-11-07 ユーザーの新しい決定：直す前に
      断ったときも、直したあとに断ったとき＝計画書 7-1 (24)・`answer_ends_row` とそろえる。締めたときにもう前の担当の方への通話だったものも、
      その期は、いまの期より前なので同じ）。
    - ただし「もうかけないで」（do_not_call）は、終わったまま（止めた範囲は締めたときの決まりのまま。✅ 同 決定「迷えば戻さず」）。
    - 受付の方に「お名前だけではどの方か分かりかねます」と言われて終えた行（方針の鍵 business_rules.WHO_UNCLEAR）は、依頼者に埋めていただく欄
      （その通話を締めたときに空いていたフルネーム・部署＝business_settings.who_unclear_blanks の1か所）の、どれかが埋まっていれば、終わったままに
      しない（✅ 2026-11-08 ユーザー＝計画書 7-1 (29)「埋めたらかけ直す」。担当の方は「同じ」の直しでも＝`reopens_on_fix` の same_person。0213）。
      両方あってもまた分からないと言われた行（お呼びし直した＝recalled）は、埋める欄が無いので、終わったまま。"""
    answer = former_answer(call["state"], key=call["policy_key"], out_of_scope=call["out_of_scope"])
    if answer != "personal" or call["state"] == "do_not_call":
        return True
    if call["policy_key"] == business_rules.WHO_UNCLEAR and _blanks_filled(tx, call["attempt_id"]):
        return False
    return tx.one("SELECT salescall.attempt_in_period(%s, now()) AS same", (call["attempt_id"],))["same"]


def _blanks_filled(tx: Tx, attempt_id: UUID) -> bool:
    """その通話（お名前だけではどの方か分からなかった通話）を締めたときに、依頼者に埋めていただく欄だった欄（その時刻の値で空いていた欄＝
    business_settings.who_unclear_blanks の1か所）の、どれかが、いま埋まっているか（`answer_still_ends` だけが読む。0213）。締めた時刻は
    データベースの1か所 salescall.call_standing_at。"""
    a = tx.one("SELECT task_id, salescall.call_standing_at(attempt_id) AS at FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    then = business_settings.who_unclear_blanks(tx, a["task_id"], before=a["at"])
    return bool(set(then) - set(business_settings.who_unclear_blanks(tx, a["task_id"])))


def source_ends_row(tx: Tx, src: dict[str, Any] | None) -> bool:
    """言ってはいけないことを言った通話（core/apology.source_call）の答えで、行を終えたか（1か所。新しい担当の方への形＝`_to_new_person` が読む）：
    判定が無い・業務を終えた結果（`END_STATES`）で、その答えで終えた行が、いまも終わったまま（`answer_still_ends`＝前の担当の方の、その方個人の
    答え（用件の断り）は終わったままにしない。会社としての答え・ふつうの通話の答えは終わったまま）。⛔ 0202：END_STATES の名前だけで見て、前の
    担当の方が用件を断った（declined）通話で言ってはいけないことを言うと、お詫びのあと新しい担当の方へかけなかった（第三者の見直しの指摘5）。"""
    if src is None or src["state"] is None:
        return True
    if src["state"] not in END_STATES:
        return False
    return answer_still_ends(tx, src)


def _after_former(tx: Tx, task_id: UUID, d: Decision, row: dict[str, Any] | None, *, attempt_id: UUID | None = None,
                  answer: str | None = None, apology: bool = False, complaint: bool = False, kept: bool = False, reached: bool = False,
                  now: datetime) -> Decision:
    """前の担当の方への通話（tasks.former_person_call の1か所）のあと・前の担当の方の期の予定（row の期が、いまの期と違う）を門が止めたあとの決定（d＝同じ方の
    ときの決まりで決めたもの）を、行を終えずに新しい担当の方への形（`_to_new_person`）にするか（1か所。通話のあと＝`after_call`・お詫びの電話のあと＝
    `after_apology`・門のあと＝`after_denial`。番号の直しと名前などの直しで別々に書かない。0214）：
    - その方個人の答え（用件の断り・「もうかけないで」・ほかの手段・在籍していない など＝`FORMER_ANSWERS` の personal。行を終えるかは `answer_ends_row` の
      1か所）で行を終える決定 ⇒ 新しい担当の方へ（✅ 2026-11-07 ユーザー＝計画書 7-1 (22)(24)）。
    - その方への用を使い切って終える決定（約束・番号の聞き直し・お詫びの回数・日数を使い切った＝`_count_end`、その方の番号のせいで終えた＝`NUMBER_ENDS`）
      ⇒ 新しい担当の方へ（✅ 2026-11-08 ユーザー＝計画書 7-1 (32)「約束の幅いっぱい → 鈴木様」。行を終えずに引き継ぎを確かめる電話。
      ⛔ 0213 まで：番号の直しでは前の番号へ1回で新しい番号へ移り（`_moved_on_fix`）、名前などの直し・前の担当の方の携帯では、約束を使い切ると行が
      終わった。前の担当の方への番号の聞き直しを使い切ったときも、名前などの直しでは行が終わった）。
    - お詫びの電話（apology）：お詫びは前の担当の方個人のこと。届いても・届かなくても・断られても、行を終える決定なら新しい担当の方へ（2026-11-07
      本体の決定＝計画書 7-1 (12)(22)(24)）。
    - 契約者が「守る」を選んだ約束の電話（kept＝`_kept_old_promise`）で、前の担当の方と話し（reached）、新しい約束でも行を終える答えでもない（まだ続く＝
      `FORMER_ANSWERS` の continue）⇒ 新しい担当の方へ（約束でない答えになったので、約束はそこまで。⛔ 0213 まで：番号の直しでは新しい番号へ移って
      引き継ぎ、前の担当の方の携帯・名前などの直しの会社の番号では、前の担当の方への約束が続いた）。
    新しい担当の方へかけない（言ってはいけないことのあとのお詫びで、業務を終えた結果＝`_to_new_person` が None）なら d のまま。前の担当の方が怒っていた
    （complaint）ときも d のまま（苦情は契約者が対応する特例で、行を終える＝締めの `_ends_row` と同じ）。会社としての答え（目的を果たした・対象外）は
    d のまま（行を終える）。新しい約束（promise）は契約者の選び（`after_call` の former_promise）。"""
    if complaint or d.kind == "hold":
        return d
    if attempt_id is not None:
        if not tasks.former_person_call(tx, attempt_id):
            return d
    elif answer is not None or apology or row is None or row["kind"] != "call" or row["person_since"] == person_now(tx, task_id):
        return d   # 通話が無い（門のあとでない）・門のあと：止めた予定が、いまの担当の方の期の予定
    if d.kind == "end":
        own = apology or (answer is not None and d.end_reason in _PERSONAL_ENDS and not answer_ends_row(tx, attempt_id, answer))
        used = _count_end(tx, task_id, d.end_reason) or d.end_reason in NUMBER_ENDS
        if not (own or used):
            return d
        rule = "前の担当の方への通話のあと（その方個人の答え・その方への用を使い切った）：新しい担当の方へ"
    elif kept and reached and answer == "continue" and not d.former_promise:
        rule = "前の担当の方との「守る」約束の電話で、その方と話した（約束でない答え）：新しい担当の方へ"
    else:
        return d
    moved = _to_new_person(tx, task_id, row, rule, now=now)
    return d if moved is None else moved


def _restart(tx: Tx, task_id: UUID, row: dict[str, Any] | None, *, now: datetime, rule: str,
             marks: Decision | None = None) -> Decision | None:
    """番号のせい・決めた回数で終える行を、いまかける番号で、もう一度かけ始める予定（1か所。番号を直したときに終えた行を戻す＝
    `after_dial_fix`・直したあと前の番号へかけた電話がその理由で終わるとき＝`_moved_on_fix`）。row＝もとの「かけ直す」の予定。
    ✅ 2026-11-06 ユーザー（見直しの伺いへのお答え）：お客様と約束したかけ直しは「約束の時刻（毎日15時）のまま、新しい番号で1日目から
    数え直してかける。だが、担当の相手が電話番号で違うのであれば、最初から。」⇒ 約束の時刻・幅のまま、回を0から（いまから探す。
    `_window_opening`）。時刻の決まっていない約束は、その方針の間隔で（回数は直したときから数え直す＝0180）。約束していない予定は、
    すぐかけてよいふつうの電話。約束の電話かは `_is_promise` の1か所。続きの電話の予定は、切れた電話が守っていた予定に読み替える
    （`_served_plan`）。
    - **お詫びの電話（目的 apology）は `_restart_apology`**（✅ 2026-11-06 ユーザー＝計画書 7-1 (12)：「言ってはいけないことを言った同じ
      相手なら、新しい番号へお詫びの電話をかけます。」）。番号の聞き直し（目的 ask_number_again）は業務の途中の電話（お呼びする方へ
      つながる番号を探す）なので、設計書 3-4-1 (c)「番号の伺い直しに出なかった」のとおり、いまかける番号へのふつうの電話で戻す。
    - **相手が違う**（約束・お詫びのあとに、お呼びする方を直した・「担当の方は違う」を選んで番号を直した）ときの形（約束もお詫びも
      引き継がない・引き継ぎを確かめる印・業務を終えていれば営業の電話にしない＝None。⛔ 0193 の前は、言ってはいけないことで終えた行・
      断った行へ営業の電話をかけ直した）は、前の番号からいまかける番号へ移る1か所 `_onto_dial`（まだ終えていない行で番号を直したとき・
      前の番号でかからずに移るときと同じ）。戻さない（None）のは、お詫びの電話を戻せないとき（承認されたお詫びの案が無い）と、
      `_onto_dial` が営業の電話にしないとき。
    - **戻した電話には、お詫びの印を付けない**（前の予定の印も、その元の電話も引き継がない。計画書 7-1 (11)「行を終えたあとに番号を
      直して戻した電話には、途中で切れたお詫びをしない」。⛔ 2026-11-06 の見直し：前は `_append` が前の予定の印を引き継いだ）。
      直したあと前の番号でかからずに新しい番号へ移る電話（行を終えていない＝`_moved_on_fix`）は、その決定の印（marks）のまま（同じ相手
      のときだけ。違う相手なら `_onto_dial` が外す）。"""
    served = _served_plan(tx, task_id, row)
    again = _restart_apology(tx, task_id, served, now=now, rule=rule) if served is not None and served["purpose"] == "apology" else \
        _restart_plan(tx, task_id, served, now=now, rule=rule)
    if again is None:
        return None
    again = replace(again, apology=False, cut_call=None) if marks is None else \
        replace(again, apology=marks.apology, cut_call=marks.cut_call)
    return _onto_dial(tx, task_id, row, again, now=now)


def _restart_apology(tx: Tx, task_id: UUID, served: dict[str, Any], *, now: datetime, rule: str) -> Decision | None:
    """番号のせいでかけられずに終える、または決めた回数かけてもお客様に届かずに終える（apology_unreached）、言ってはいけないことのあとの
    お詫びの電話を、いまかける番号で、もう一度かけ始める予定（`_restart` だけが呼ぶ。✅ 2026-11-06 ユーザー＝計画書 7-1 (12)・(14)：
    届かずに終えた行も「戻す」＝戻し方は (12) と同じ）：新しい番号へ、お詫びの電話（目的 apology のまま。お詫びの電話は約束の印を
    持つ＝`schedule_apology` と同じ。お詫びの文・承認・録音はそのまま）。戻したお詫びの電話の回数は、新しい番号で0回から（計画書
    7-1 (15)＝gate.purpose_dials の counted の1か所）。承認されたお詫びの案が無い（お詫びの電話の予定は、承認からしか書かない＝
    core/apology.schedule_if_ready）ときは戻さない（None）。
    違う相手（言ってはいけないことを言った通話＝core/apology.source_call が、いまの担当の方の期の通話でない＝データベースの
    salescall.attempt_in_period）なら、お詫びをせず、新しい担当の方への形（`_to_new_person` の1か所＝引き継ぎを確かめる電話。言ってはいけないことを
    言った通話で業務を終えていれば戻さない＝None）。served＝お詫びの電話の予定（`_served_plan`）。⛔ 0209 まで：同じ相手かを見ずにお詫びの電話にしていたので、名前などを「違う」で
    直した行（番号が変わらない）では、お詫びがその番号のまま前の担当の方へ戻った（`_plan_person` の、同じ番号への `NUMBER_BOUND` の電話）。"""
    from salescall.core import apology   # お詫びの部品が次の動きを読むので、ここで読む（読み合いにしない）
    src = apology.source_call(tx, task_id)
    if src is None:
        return None
    dial = tasks.dial_number(tx, task_id)["contact_id"]
    if not tx.one("SELECT salescall.attempt_in_period(%s, now()) AS same", (src["attempt_id"],))["same"]:
        return _to_new_person(tx, task_id, served, rule + "（言ってはいけないことを言った相手と違う：お詫びなしで）", now=now)
    return _call(now, True, rule + "（言ってはいけないことを言った同じ相手：新しい番号へお詫びの電話）", Target(dial, purpose="apology"))


def _restart_plan(tx: Tx, task_id: UUID, row: dict[str, Any] | None, *, now: datetime, rule: str) -> Decision:
    """`_restart` の、もう一度かけ始める予定の時刻と約束（`_restart` だけが呼ぶ。同じ相手のときの形。相手が違えば `_onto_dial`）。"""
    dial = tasks.dial_number(tx, task_id)["contact_id"]
    if not _is_promise(tx, row):
        return _call(now, False, rule, Target(dial))
    t = replace(_plan_target(row), contact_id=dial)
    window = window_of(row)
    if window is None:
        return _unspecified_next(tx, task_id, t, now=now, base=now, tried=now)
    at, w2 = _window_opening(tx, task_id, now, replace(window, round=0, missed=0), now=now)
    if w2 is None:   # 次の回が無い（理由の分からない日だけの約束が過ぎた）：時刻の決まっていない約束と同じく
        return _unspecified_next(tx, task_id, t, now=now, base=now, tried=now)
    w2 = replace(w2, round=0, missed=0)
    # 時刻ちょうどの約束は、その回の約束の時刻（お客様が指定した時刻＝callee_at）。かける時刻は、回の始まり＝約束の時刻ちょうど（いまが
    # その回の中なら、いま）を `_window_opening` の1か所が返す（⛔ 2026-11-06 の見直し：ここで「約束の時刻ちょうどに」をもう一度決めて
    # いたが、`_window_opening` の答えと同じで、外しても何も変わらなかった＝壊し試しで見張れない重なり。外した）
    callee = _slot_of(w2)[0] if row["callee_at"] is not None else None
    if at is None:
        return _no_opening(now, no_opening_recheck(tx, task_id), True, t, callee, w2)
    return _call(at, True, rule + "（約束の時刻・幅のまま、1日目から数え直す）", t, callee, w2)


def _moved_on_fix(tx: Tx, task_id: UUID, d: Decision, row: dict[str, Any] | None, was_replaced: bool, *, now: datetime) -> Decision:
    """直したあと前の番号へかけた電話（まだかけていなかった約束の電話＝`_point`）が、番号のせい・決めた回数で終える決定になった ⇒
    終えずに、いまかける番号で（`_restart`。2026-11-06 ユーザー：「旧番号でかからなければ、新番号でかけます」。⛔ 見直し (A) の1：前は
    行が終わり、新しい番号へ移らなかった）。どの終わりを番号のせいとするかは `reopens_on_fix` の1か所（直したあとに終えた行を戻す
    のと同じ決まり）。
    直したあと前の番号へかけたお詫びの電話が届かず、また前の番号へお詫びをかける決定になったときも、いまかける番号で（前の番号への
    お詫び・番号の聞き直しは1回まで＝`_old_number_moves`。✅ 2026-11-06 ユーザー「約束の電話と同じ」＝計画書 7-1 (17)）。
    戻す形（業務を終えたあとのお詫びの電話は戻さない＝終える決定のまま）は `_restart` の1か所。行を終えていないので、お詫びの印は、
    この決定の印のまま（None＝いまの予定から引き継ぐ。同じ相手のときだけ）。
    ほかの決定で、かける先が前の番号からいまかける番号へ移った（`current_dial`＝前の番号で話せなかった約束の電話・前の番号で切れた
    続きの電話 など）ときも、相手が違えば約束・お詫びを引き継がない（`_onto_dial` の1か所。`_restart` も同じ所を通る）。"""
    moves = d.kind == "call" and _old_number_moves(tx, task_id, d.target, now, called=was_replaced, row=row)   # 前の番号へのお詫びは1回まで（7-1 (17)）
    if not moves and (d.kind != "end" or not was_replaced or not reopens_on_fix(tx, task_id, d.end_reason)):
        moved = _onto_dial(tx, task_id, row, d, now=now)
        return d if moved is None else moved
    again = _restart(tx, task_id, row, now=now, marks=d,
                     rule=f"直したあと前の番号でかからなかった（{d.end_reason or 'お詫びの電話'}）：いまかける番号で")
    return d if again is None else again


def after_dial_fix(tx: Tx, task_id: UUID, *, cause: str, back: bool = False) -> int | None:
    """かける番号を直したあとの、行の予定（1か所。core/row_fixes.fix_dial_number だけが呼ぶ）。
    - かけ直すの予定 ⇒ いまの番号へ向ける（`_point`：約束していない予定は付け替える・約束の電話はまだかけていなければ前の番号で、
      もうかけていれば新しい番号で数え直す）。
    - 終えた行で、終わりの理由が `reopens_on_fix` に当たる ⇒ いまかける番号で、もう一度かけ始める（`_restart`：約束は時刻・幅の
      まま1日目から・相手が違えば最初から・約束していなければすぐ）。回数は直したときから数え直す＝0180。
    書いたら、その予定の番号（seq）を返す。決め方は名前などの直しと同じ1か所（`_after_fix`）。back＝前の担当の方に戻す直し（計画書 7-1 (30)。0213）。"""
    return _after_fix(tx, task_id, cause=cause, dial=True, back=back)


def after_person_fix(tx: Tx, task_id: UUID, *, cause: str, same_person: bool = False, back: bool = False) -> int | None:
    """お呼びする方の名前・フルネーム・役職・部署を「担当の方は違う」で直した（担当の期が替わった＝計画書 7-1 (19)「違う相手」・(28)）あとの、行の予定（1か所。
    core/row_fixes.fix だけが呼ぶ。0206）。かけ直すの予定の決め直しは、番号を直したとき（`after_dial_fix`）と同じ1か所（`_repoint`）：
    前の担当の方との約束は、契約者の選び（row_fixes が受けて直しの記録に残す）のとおり。約束の番号がいまかける番号なら選べず、約束をやめて
    引き継ぎを確かめる電話（`_onto_dial`）・別の番号で「守る」なら約束の番号のまま前の担当の方の期・「次の担当の方にだけ」なら約束をやめて
    いまかける番号へ（`_point`）。終えた行を戻すかも番号の直しと同じ1か所（`reopens_on_fix`）：前の担当の方個人の答え（用件の断り・ほかの手段・
    在籍していない など）で終えた行は戻し、新しい担当の方へ引き継ぎを確かめる電話（計画書 7-1 (19)(26)。0208）。決めた回数（日数）・「あとで」の
    回数で終えた行も戻す（新しい担当の方には、まだかけていない＝計画書 7-1 (8)(27)。0210）。番号は変わらないので、番号のせいで終えた行は戻さない。
    呼ぶのは、値が本当に変わる直し（データベースの salescall.person_fix_differs）。same_person＝契約者が「同じ」を選んだ（お呼びする方は替わらない：
    予定も約束もそのまま。戻すのは、依頼者に埋めていただく欄を埋めた行だけ＝計画書 7-1 (29)。0213）。back＝前の担当の方に戻す直し（計画書 7-1 (30)）。
    ⛔ 0209 まで：決めた回数・「あとで」の回数で終えた行を、名前などの直しでは戻さなかった（番号を「違う」で直したときは戻る＝見直し J-(C)）。
    ⛔ 0205 まで：名前などを直しても予定を見直さず、前の担当の方との約束が選び無しに前の担当の方の期のまま残り、その電話は前の担当の方への
    通話になった。⛔ 0206 まで：前の担当の方個人の答えで終えた行を戻さなかった。書いたら、その予定の番号（seq）を返す。"""
    return _after_fix(tx, task_id, cause=cause, dial=False, same_person=same_person, back=back)


def _after_fix(tx: Tx, task_id: UUID, *, cause: str, dial: bool, same_person: bool = False, back: bool = False) -> int | None:
    """行の直し（dial＝かける番号の直し・偽＝お呼びする方の名前などの中身の直し）のあとの、行の予定（1か所。`after_dial_fix`・`after_person_fix`
    が通る。0208）：終えた行は、戻すか（`reopens_on_fix` の1か所）を見て、戻すなら、いまかける番号で、もう一度かけ始める（`_restart`。相手が
    違えば `_onto_dial` が新しい担当の方への形にする）。かけ直すの予定は決め直す（`_repoint`）。
    same_person＝名前などを「同じ」で直した（0213）：お呼びする方は替わらないので、予定も約束もそのまま（予約した通話も取り消さない）。終えた行だけ、
    戻すかを見る（依頼者に埋めていただく欄を埋めた行＝計画書 7-1 (29)）。
    back＝前の担当の方に戻す直し（✅ 2026-11-08 ユーザー＝計画書 7-1 (30)「戻すと前の担当の方のそのときの答え＝用件の断りなども戻る。断っていれば
    行は終わり、田中様へはかけない」。0213）：戻した期の答えで行を終えるか（`_period_ends`）を先に見る。"""
    if same_person:
        lock(tx, task_id)
        cur = current(tx, task_id)
        if cur is None or cur["kind"] != "end" or not reopens_on_fix(tx, task_id, cur["end_reason"], dial=False, same_person=True):
            return None
    cur, now = _fix_start(tx, task_id)
    if cur is None:
        return None
    ended = _period_ends(tx, task_id) if back and cur["kind"] != "hold" else None
    if ended is not None:   # 戻した担当の方の、そのときの答えで終える（もう終えている行は、終えたまま＝ほかの方の答えで戻さない）
        reason, attempt_id = ended
        return None if cur["kind"] == "end" else \
            _write(tx, task_id, _end(reason, f"{cause}（戻した担当の方の、そのときの答え {reason} で行を終える）"), after=attempt_id)
    if cur["kind"] == "end" and not reopens_on_fix(tx, task_id, cur["end_reason"], dial=dial, same_person=same_person):
        return None
    # 戻した担当の方との約束で、直すとき「次の担当の方にだけ」でやめた約束が、まだ残っていれば戻す（`_period_promise` の1か所。計画書 7-1 (31)）
    restored = _period_promise(tx, task_id, cur, cause=cause, now=now) if back and cur["kind"] != "hold" else None
    if restored is not None:
        return _write(tx, task_id, restored)
    if cur["kind"] == "end":
        again = _restart(tx, task_id, _last_call_plan(tx, task_id, cur["seq"]), now=now,
                         rule=f"{cause}（{cur['end_reason']} で終えた行を、いまかける番号で）")
        return None if again is None else _write(tx, task_id, again)   # 戻せないお詫びの電話は戻さない（`_restart`）
    return _repoint(tx, task_id, cur, cause=cause, now=now)


def _period_ends(tx: Tx, task_id: UUID) -> tuple[str, UUID] | None:
    """前の担当の方に戻したあと（`_after_fix` の back）、いまの期（戻した期）の答えで行を終えるか（1か所。0213）：いまの期の、いちばん新しい
    判定した通話（その時刻にいまの期の通話か＝データベースの salescall.attempt_in_period の1か所）の答えが、その方個人の答え（用件の断り・
    「もうかけないで」・ほかの手段・在籍していない・お名前だけでは分からない＝`FORMER_ANSWERS`・`former_answer` の1か所）で、その答えで終える行が
    いまも終わったまま（`answer_still_ends` の1か所。いまの期の通話なので、ふつうは終わったまま。依頼者に埋めていただく欄を埋めていれば
    終わったままにしない）なら、（終わりの理由, その通話）を返す。終わりの理由は、その答えで行を終えた決定と同じ（判定の状態か、方針の鍵）。
    会社としての答え（目的を果たした・対象外）は、もともと行を終えたまま（戻していない）。新しいかけ直しの約束・まだ続く答えは None
    （いまの予定を決め直す＝`_repoint`）。苦情で終えた行も、もともと戻していない。"""
    r = tx.one("SELECT j.attempt_id, j.business_state::text AS state, j.detail ->> 'policy_key' AS policy_key,"
               "       j.detail ->> 'out_of_scope' AS out_of_scope FROM salescall.call_judgements j"
               " JOIN salescall.attempts a ON a.attempt_id = j.attempt_id"
               " WHERE j.task_id = %s AND salescall.attempt_in_period(a.attempt_id, now())"
               " ORDER BY a.claimed_at DESC, a.attempt_id DESC LIMIT 1", (task_id,))
    if r is None or former_answer(r["state"], key=r["policy_key"], out_of_scope=r["out_of_scope"]) != "personal" \
            or not answer_still_ends(tx, r):
        return None
    return (r["state"] if FORMER_ANSWERS[r["state"]] == "personal" else r["policy_key"]), r["attempt_id"]


def _period_promise(tx: Tx, task_id: UUID, cur: dict[str, Any], *, cause: str, now: datetime) -> Decision | None:
    """前の担当の方に戻したあと（`_after_fix` の back。行を終えていない・戻す行）、戻した期の約束を戻すか・どの予定に戻すか（1か所。0214）。
    ✅ 2026-11-08 ユーザー＝計画書 7-1 (31)「約束を戻す」：担当を直すとき「次の担当の方にだけかける」を選んで前の担当の方との約束をやめたあと、その方に
    戻したら、約束の時刻にかける。約束の日・幅がもう過ぎていれば、ふつうの電話（None＝`_repoint`・`_restart` のいまの形）。
    - 戻す約束：戻した期（いまの期）の、いちばん最後の予定（その期を離れたときの予定）が、お客様と約束したかけ直し（`_is_promise`）で、その予定から次の
      予定を書くまでのあいだの選び（old_promise_choices＝離れる直しの選び・前の担当の方の新しい約束の選び）が「次の担当の方にだけ」（契約者がやめた約束）。
      「守る」を選んで約束が残っている・使い切った約束（新しい担当の方への形にしたのは選びでなく `_after_former`）は戻さない。
    - 約束の日・幅が残っているか：約束全体（すべての日・回）がまだ終わっていない（✅ 2026-11-08 本体の決定：「過ぎた」は約束全体が終わったとき）。
      いまの回の終わりがまだ来ていなければ、その予定のまま。過ぎていて、まだ日・回が残っていれば、次の回（次の日の約束の時刻。`_keep_promise`。
      取り戻す電話＝`_recover_today` はしない）。回が残っていない・約束の日数・回数を使い切っていれば、ふつうの電話。⛔ 0214：いまの回の終わり
      （window_until）が過ぎたら、ふつうの電話にしていた。時刻の決まっていない約束（「あとで」）と終わりの無い幅は、日・幅が無いので残っている。
    - 戻す予定：その約束の予定のまま（約束の印・お客様が指定した時刻・幅・回の数え＝何回目の回か・かけられなかった回・お詫びの印・かける番号）。
      かける時刻は、約束の時刻（もう過ぎていれば、いま＝その回の中）。その回にかけた電話の数えは、その約束の期の通話だけ（やめていたあいだの新しい担当の
      方への電話は数えない＝`attempts.in_promise_count`）。予定の期は戻した期（決定の resumes＝その約束の予定。`_write` が `_plan_person` のもとの期にする）。
    - いまの予定が、離れる方との「守る」約束（`_kept_old_promise`。戻すとき、離れる方の約束も守ると選んだ）なら、契約者が守ると選んだ約束を先にし、戻さない。
    ⛔ 0213：やめた約束は戻さず、戻すとふつうの電話にしていた（⏳ 本体に伺っていた。設計書 3-4-1【0213】の (c)）。"""
    if cur["kind"] == "call" and _kept_old_promise(tx, task_id, cur):
        return None
    r = tx.one(f"SELECT {_PLAN_COLUMNS} FROM salescall.task_next_actions n"
               " LEFT JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
               " WHERE n.task_id = %s AND n.person_since IS NOT DISTINCT FROM salescall.task_person_since(%s, now())"
               " ORDER BY n.seq DESC LIMIT 1", (task_id, task_id))
    if r is None or r["seq"] == cur["seq"] or not _is_promise(tx, r) or not tx.one(
            "SELECT EXISTS (SELECT 1 FROM salescall.old_promise_choices c WHERE c.task_id = %s AND NOT c.keep_old_promise"
            "   AND c.recorded_at >= %s AND c.recorded_at <= (SELECT min(m.decided_at) FROM salescall.task_next_actions m"
            "                                                 WHERE m.task_id = %s AND m.seq > %s)) AS d",
            (task_id, r["decided_at"], task_id, r["seq"]))["d"]:
        return None
    window = window_of(r)
    if window is not None and window.round_end is not None and window.round_end <= now:
        # いまの回は過ぎた：約束の日・回がまだ残っていれば、次の回へ（約束を守る1か所 `_keep_promise`。回の数え・約束の時刻・幅はそのまま。その回に
        # 一度もかけていなければ、約束の日にかけられなかったお詫びも足す）。約束全体（すべての日・回）が終わっていれば、ふつうの電話（None）。
        # ✅ 2026-11-08 本体の決定（計画書 7-1 (31) の「約束の日・幅がもう過ぎていれば、ふつうの電話」の読み方：「過ぎた」は、約束全体が終わったとき）。
        # ⛔ 0214：いまの回の終わりが過ぎたら、まだ日・回が残っていても、ふつうの電話にしていた。0216
        if _window_opening(tx, task_id, now, window, now=now)[1] is None:
            return None   # 約束の回がもう無い（理由の分からない日だけの約束が過ぎた など）：ふつうの電話
        exact = r["callee_at"] is not None
        d = _keep_promise(tx, task_id, now, window, _plan_target(r), now=now, exact=exact, recover=False)
        if d.kind != "call" or d.window is None or _promise_over(tx, task_id, d.window.round, exact=exact):
            return None   # 約束の日数・回数を使い切った・約束の日・回がもう過ぎた：ふつうの電話
        reasons = _apology_reasons(tuple(r["apology_reasons"]), d.apology)
        return replace(d, rule=f"{cause}（戻した担当の方との、やめていた約束を戻す：次の回へ）", apology=reasons,
                       cut_call=r["cut_call"] if "cut_off" in reasons else None, resumes=r["seq"])
    return replace(_call(max(r["call_at"], now), True, f"{cause}（戻した担当の方との、やめていた約束を戻す：約束の時刻・幅・回の数えのまま）",
                         _plan_target(r), r["callee_at"], window, apology=tuple(r["apology_reasons"]), cut_call=r["cut_call"]),
                   resumes=r["seq"])


def _promise_over(tx: Tx, task_id: UUID, round_: int, *, exact: bool) -> bool:
    """その約束の、何回目の回（round_。最初の回は0）が、約束全体（すべての日・回）を過ぎているか（`_period_promise` だけが読む）：時刻ちょうどの
    約束は設定 callback.exact_days の日数、幅の約束は時刻の決まっていない約束の方針の回数（`_keep_promise` の打ち止めと同じ数）。やめていたあいだに
    過ぎた回も数える（契約者の選びでかけなかった回は、こちらの止めでかけられなかった回＝打ち止めに数えない回ではない。✅ 2026-11-08 本体の決定：
    「過ぎた」は約束全体が終わったとき。0216）。"""
    if exact:
        return round_ >= int(settings.resolve(tx, "callback.exact_days", **tasks.scope(tx, task_id)))
    p = _unspecified_policy(tx, task_id)
    return p.action == "end" or round_ > p.max_times


def _fix_start(tx: Tx, task_id: UUID) -> tuple[dict[str, Any] | None, datetime]:
    """行の直し（番号・名前など）のあとの予定の決め直しの始め（`after_dial_fix`・`after_person_fix` の1か所）：直しは門の判定に効く変更なので、
    その行のまだ発信していない予約を取り消し（終える・判定待ちと同じ。DESIGN.md 5-2-1 の2。⛔ 2026-11-06 の見直し：前は残り、直したあとに前の
    番号へかかった。予約し直すのは門＝直したあとの予定で。名前などの直しでも、予約した通話は前の担当の方の期のまま＝0206）、いまの予定と
    いまの時刻を返す。"""
    lock(tx, task_id)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    return current(tx, task_id), tx.one("SELECT now() AS now")["now"]


def _repoint(tx: Tx, task_id: UUID, cur: dict[str, Any], *, cause: str, now: datetime) -> int | None:
    """行の直しのあと、かけ直すの予定（cur）を決め直す（1か所。番号の直し＝`after_dial_fix`・名前などの直し＝`after_person_fix`）：いまの番号へ
    向け（`_point`）、相手が違えば約束・お詫びを引き継がない（`_onto_dial`）。判定待ちの予定は、そのまま（判定し直しが決める）。"""
    if cur["kind"] != "call":
        return None
    if _old_number_moves(tx, task_id, _plan_target(cur), now, called=False, row=cur):
        # 前の番号へのお詫びの電話が、もう鳴っていた ⇒ いまかける番号で（約束の電話の `_point` と同じ。計画書 7-1 (17)）
        again = _restart(tx, task_id, cur, now=now, rule=f"{cause}（前の番号へのお詫びの電話は、もうかけた：いまかける番号で）")
        if again is not None:
            return _write(tx, task_id, again)
    t, window = _point(tx, task_id, cur, numbers_of(tx, task_id))
    if t.contact_id == cur["contact_id"] and _plan_person(tx, task_id, cur, t, None, promised=bool(cur["promised"]))[0] == cur["person_since"]:
        return None   # 番号も相手も、予定の担当の期も変わらない：予定はそのまま（前の担当の方の番号に残る約束など）
    # 相手が違えば、約束・お詫びを引き継がない（いまの担当の方の期の予定になる1か所＝`_onto_dial`。決定を書く `_write` が通す。予定がもう直した
    # 番号にあるときも通す＝0202。営業を続けない形＝お詫びの電話は、同じ番号のまま前の担当の方の期に残るので、ここへは来ない＝`_plan_person`）
    return _write(tx, task_id, _call(cur["call_at"], bool(cur["promised"]), cause, t, cur["callee_at"] if cur["promised"] else None,
                                     window))


def hold(tx: Tx, task_id: UUID, *, cause: str, apology: Apology = None, cut_call: UUID | None = None,
         after: UUID | None = None) -> int:
    """判定待ちにする（かけない・終えない）。同じ取引で、その行のまだ発信していない予約を取り消す。"""
    seq = _append(tx, task_id, call_at=None, end_reason=None, cause=cause, hold=True, apology=apology, cut_call=cut_call, after=after)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    return seq


def end(tx: Tx, task_id: UUID, reason: str, *, cause: str, apology: Apology = None, cut_call: UUID | None = None,
        after: UUID | None = None) -> int:
    """終える（理由）を記録する。同じ取引で、その行のまだ発信していない予約を取り消す（門の判定に効く変更。5-2-1 の2）。終えたら、確定の
    印の無い結果の版を、承認の部分が確かめ直す（core/approval.after_ended の1か所。前の作業担当の残した問い (8)）。"""
    seq = _append(tx, task_id, call_at=None, end_reason=reason, cause=cause, apology=apology, cut_call=cut_call, after=after)
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s AND state = 'claimed'", (task_id,))])
    approval.after_ended(tx, task_id)
    return seq

DNC_ENDS = ("dnc_client", "dnc_client_ref", "dnc_contractor")   # 門が発信禁止で終えた行の終わりの理由＝かけない理由の名前（target_policy）


def reopen_revoked(tx: Tx, *, cause: str) -> list[UUID]:
    """発信禁止で門が終えた行のうち、その行の番号・管理番号の登録（依頼者の台帳・契約者の共通の台帳）が取り消され、いまはもう発信禁止で
    止まっていないものを、終える前の予定に戻す（どの行を戻すかの1か所。2026-10-30 管理者のご決定 (7)）。戻す予定は、終える前のいちばん
    新しい「かけ直す」の予定（かける先・目的・約束の印・お客様の時刻・幅・お詫びの印のまま、時刻はいま）。まだ止まっているかは、門と同じ
    かけない理由の1か所（target_policy.block_reason）で、戻す予定の番号と行の管理番号を見る（⛔ 見直し24b の (C)：前は、取り消した時刻と
    終えた時刻を比べて自分で決め、まだほかの登録で止まっている行も「戻した」にした）。呼ぶのは、依頼者の台帳の取り消しの1か所（core/dnc の
    `_revoke`。その場で）と、見守り役の決まった間隔の仕事（依頼者ごと。契約者の共通の台帳は、その契約者のすべての依頼者の行に効くので、
    取り消しの取引からは行を読めない）。戻した行を返す。"""
    back = []
    for r in tx.all(
            "SELECT c.task_id, c.seq, t.client_ref FROM salescall.current_next_actions c JOIN salescall.tasks t ON t.task_id = c.task_id"
            " WHERE c.kind = 'end' AND c.end_reason = ANY(%s)"
            "   AND (EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.revoked_at IS NOT NULL"
            "                AND (e.client_ref = t.client_ref OR e.phone_e164 IN (SELECT n.phone_e164 FROM salescall.task_numbers n"
            "                                                                      WHERE n.task_id = t.task_id)))"
            "        OR EXISTS (SELECT 1 FROM salescall.contractor_dnc_entries e WHERE e.revoked_at IS NOT NULL"
            "                   AND e.phone_e164 IN (SELECT n.phone_e164 FROM salescall.task_numbers n WHERE n.task_id = t.task_id)))"
            " ORDER BY c.task_id", (list(DNC_ENDS),)):
        # 終える前のいちばん新しい「かけ直す」の予定（読むのは `_last_call_plan` の1か所。⛔ 2026-11-06 の見直し：前はここが自分で読んだ）。
        # 戻す予定は、その予定のまま（お詫びの印も。2026-10-30 管理者のご決定 (7)「終える前の予定に戻す」＝番号を直したときに終えた行を
        # もう一度かけ始める `_restart`＝お詫びの印を付けない、とは別のご決定）
        row = _last_call_plan(tx, r["task_id"], r["seq"])
        if row is None:
            continue
        blocked = target_policy.block_reason(tx, normalize(row["phone_e164"]), client_ref=r["client_ref"],
                                             **tasks.scope(tx, r["task_id"]))
        if blocked is not None and blocked[0] in DNC_ENDS:
            continue                           # まだ発信禁止で止まっている（ほかの登録）：戻さない
        # いまの番号へ向ける（番号を直したときと同じ1か所＝`_point`。⛔ 見直し (A) の7：前は約束の電話も、すぐ新しい番号へ替え、
        # 約束の回も数え直さなかった）
        target, window = _point(tx, r["task_id"], row, numbers_of(tx, r["task_id"]))
        now = tx.one("SELECT now() AS now")["now"]
        # お詫びの印も（その元の電話も）、終える前の予定のまま。前の番号の予定がいまかける番号へ移り、相手が違えば、約束・お詫びを
        # 引き継がない（`_onto_dial` の1か所。営業の電話にしないなら戻さない）
        d = _onto_dial(tx, r["task_id"], row, _call(now, bool(row["promised"]), cause, target,
                                                    row["callee_at"] if row["promised"] else None, window,
                                                    tuple(row["apology_reasons"]), row["cut_call"]), now=now)
        if d is None:
            continue
        _write(tx, r["task_id"], d)
        back.append(r["task_id"])
    return back


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
    check_of: UUID | None = None         # call のとき：切れ方の分からなかった通話（試行）の、確かめのかけ直し（`after_call` が記録する）
    cut_call: UUID | None = None         # この決定でお詫びの理由 cut_off を付けた元の電話（試行。0187。None＝前の予定から引き継ぐ）
    # call のとき：前の担当の方への通話で、その方がした新しいかけ直しの約束（契約者の選びを待つ＝計画書 7-1 (23)。その方の期のまま書く＝
    # `_plan_person`。付けるのは `after_call` の1か所。0210）
    former_promise: bool = False
    # call のとき：この決定が続ける元の予定（予定の番号 seq。前の担当の方に戻したとき、戻した期の、やめていた約束の予定に戻す＝`_period_promise`。
    # その予定の期を、書く予定のもとの期にする＝`_write`・`_plan_person`。0214）
    resumes: int | None = None

    @property
    def contact_id(self) -> UUID | None:
        return None if self.target is None else self.target.contact_id


def _call(at: datetime, promised: bool, rule: str, target: Target, callee_at: datetime | None = None,
          window: Window | None = None, apology: Apology = None, cut_call: UUID | None = None) -> Decision:
    return Decision("call", at, promised, None, rule, target, callee_at, window, apology, cut_call=cut_call)


def _end(reason: str, rule: str) -> Decision:
    return Decision("end", None, None, reason, rule)


def _hold(rule: str) -> Decision:
    return Decision("hold", None, None, None, rule)


def no_opening_recheck(tx: Tx, task_id: UUID) -> int:
    """これから先、かけてよい時間帯が見つからない行を、もう一度確かめるまでの秒（仮の設定 retry.no_opening_recheck。初め1日。読むのは
    この1か所。決める関数＝decide_after_call などへは、呼ぶ側が渡す＝初めの値の写しを持たない。⛔ 見直し23 の (9)：前はコードに直に
    書いていた＝NO_OPENING_RECHECK）。"""
    return int(settings.resolve(tx, "retry.no_opening_recheck", **tasks.scope(tx, task_id)))


def _no_opening(now: datetime, recheck: int, promised: bool, target: Target, callee_at: datetime | None = None,
                window: Window | None = None) -> Decision:
    """これから先（31日）、かけてよい時間帯が見つからない：行を終えず、recheck 秒（`no_opening_recheck`）のあとにもう一度確かめる
    （1か所）。時間帯が無いのは設定のその時の形（依頼者の時間帯を空にした・NG時間が時間帯を覆う・祝日だけの時間帯など）で、
    お客様の側の理由ではない。前は行を終えていて、設定を戻しても取り返せなかった（2026-10-16 の見直し G6。
    2026-10-16 ユーザー：「何でもかんでも止めるんじゃないよ」）。門が時間帯の外で止め、次の動きがまた確かめる。"""
    return _call(now + timedelta(seconds=recheck), promised, "これから先、かけてよい時間帯が見つからない：決めた長さのあとにもう一度確かめる"
                 "（retry.no_opening_recheck）", target, callee_at, window)


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
        return Target(None, f"{ALTERNATE_BLOCKED}{alt.blocked}")   # かけない種類・発信禁止・そろえられない：記録だけ
    return Target(alt.contact_id)


@dataclass(frozen=True)
class Numbers:
    """行の番号の材料（締めの処理の時点。`numbers_of` の1か所で集める）。"""
    unusable: frozenset[UUID] = frozenset()   # 回線の部品が続けて受け付けなかった番号（番号の欄が「使えない」＝core/health.number_dead）
    said: tuple[UUID, ...] = ()                # お客様が言った別の番号のうち、かけない理由の無いもの（新しい順）
    sources: tuple[tuple[UUID, UUID], ...] = ()   # お客様が言った番号 → その番号を言われた通話でかけた番号（伺い直す先）
    asked_again: bool = False                  # この行で、前にも番号を伺い直すと決めた（number_recheck.asked_again_before の1か所）
    dial: UUID | None = None                   # いまかける番号（ダッシュボードで直していれば直した番号＝tasks.dial_number の1か所。0179）
    sub: UUID | None = None                    # サブの番号（直したあと前の番号で話せた番号＝salescall.task_sub_contact の1か所。0182）
    superseded: frozenset[UUID] = frozenset()  # 前にかける番号だった番号（取り込んだ番号・前に直した番号。いまかける番号に替える）


def choose_number(j: Judgement, *, dialed: Target, alternate: Alternate | None, recheck: Recheck | None,
                  cut_off: bool = False, numbers: Numbers = Numbers(), connected: bool = True, keep_former: bool = False) -> Target:
    """次にかける先（番号と発信の目的。1か所。下の _choose）。後任の方を追う印（successor）は、この通話で目的のお客様が後任の方に
    替わった（判定の印）か、かけた電話が後任の方を追う電話だったら、行が終わるまで引き継ぐ（後任の方に繋がったあとも、これからの
    電話は後任のご担当者様をお呼びする。2026-10-22 ユーザー：「基本は後任へつないでもらう」）。決めた番号が、回線の部品に続けて受け付け
    られなかった番号なら `usable` で替える。connected＝いまの通話でお客様と話せたか（call_report.talked_in。メインとサブの選び＝`current_dial`）。
    keep_former＝前の担当の方が新しいかけ直しの約束をした（`after_call` が決める。計画書 7-1 (23)）：契約者が選ぶまで、前の担当の方の番号の
    まま（いまかける番号へ替えない。その番号が使えなければ、ふつうに `usable`）。"""
    chosen = _choose(j, dialed=dialed, alternate=alternate, recheck=recheck, cut_off=cut_off)
    t = chosen if keep_former and chosen.contact_id is not None and chosen.contact_id not in numbers.unusable else \
        usable(chosen, numbers, connected=connected)
    if t.contact_id is not None and (j.detail.get("successor") or dialed.successor):
        return replace(t, successor=True)
    return t


def usable(t: Target, numbers: Numbers, *, connected: bool = True) -> Target:
    """かける番号が、お客様のところで鳴らない電話が続いた番号（番号の欄が「使えない」＝core/health.number_dead：設定
    dial.failed_retry_days の並びの数だけ続いた）なら、その番号へはかけない（1か所。計画書 10-3「無限に再試行しない」。⛔ 見直し15：発信に失敗した電話は行の回数に
    数えないので、前は行が終わらず、同じ番号へかけ続けた）：お客様が言った別の番号（かけない理由の無いもの）があれば、そちらへ（ふつうの電話・続きの電話だけ。
    番号の聞き直し・お詫びは、その番号のお客様へかける電話なので替えない）。無ければ行を終える（number_unusable。番号は記録に
    残り、依頼者へ渡す）。メインの番号が使えなければ、まずサブの番号へ（0182）。"""
    t = current_dial(t, numbers, connected=connected)
    if t.contact_id is None or t.contact_id not in numbers.unusable:
        return t
    if t.purpose in SALES_PURPOSES:
        for c in ((numbers.sub,) if numbers.sub is not None else ()) + numbers.said:
            if c not in numbers.unusable and c != t.contact_id:
                return replace(t, contact_id=c)
        # お客様が言った番号に繋がらなかった：その番号を言われた通話の番号へ、番号を伺い直す（同じ行で1回だけ。番号違いの聞き直しと
        # 同じ数え方＝numbers.asked_again。2026-10-24 ユーザー：「別の方のお電話よりも、繋がらない番号のパターンも必要じゃないですか？」）
        source = dict(numbers.sources).get(t.contact_id)
        if source is not None and source not in numbers.unusable and not numbers.asked_again:
            return Target(source, None, "ask_number_again", t.contact_id, number_again="unreachable")
    return Target(None, "number_unusable")


def recount_on_switch(window: Window | None, was_replaced: bool, target: Target, numbers: Numbers) -> Window | None:
    """約束の電話が、前にかける番号だった番号から、いまかける番号へ移るとき、約束の回（日数・回数）を0から数え直す（1か所。2026-11-06
    ユーザー：「数え直す（お客様との約束は変更しない）」＝幅・時刻・回の理由はそのまま、何回目か・かけられなかった回の数だけ0に）。
    was_replaced＝かけた番号が、かけたときに前にかける番号だった番号か（`replaced_at`。⛔ 2026-11-06 の見直し (A) の2：前は「いま」の
    立場で見て、前の番号で話せてサブになったときだけ数え直さなかった）。"""
    if window is None or not was_replaced or target.contact_id != numbers.dial:
        return window
    return replace(window, round=0, missed=0)


def replaced_at(tx: Tx, task_id: UUID, contact_id: UUID | None, at: datetime) -> bool:
    """その番号が、その時刻に、前にかける番号だった番号か（データベースの1か所 salescall.task_number_replaced_at。0188）。"""
    return contact_id is not None and bool(tx.one("SELECT salescall.task_number_replaced_at(%s, %s, %s) AS r",
                                                  (task_id, contact_id, at))["r"])


def current_dial(t: Target, numbers: Numbers, *, connected: bool = True, called: bool = True) -> Target:
    """かける先が、前にかける番号だった番号（取り込んだ番号・前に直した番号）なら、いまかける番号へ替える（1か所。2026-11-06 ユーザー：
    「新番号を登録すると、旧電話番号はグレーアウトして、新番号がくっきり表示されます」＝直したあとは旧番号へかけない）。替えるのは
    ふつうの電話・続きの電話だけ（`usable` と同じ）。番号の聞き直し（その番号を言われた通話の番号へかける）とお詫び（その番号で電話を
    受けたお客様へかける）は、その番号のお客様へかける電話なので替えない。
    **メインとサブ**（0182。2026-11-06 ユーザー：「話せたら、新番号がメイン、旧番号はサブ」・「かけ直しの約束をしていて、旧番号でつながった
    場合、新しい番号がつながらないときに元の番号へもかける」）：サブの番号＝直したあと前の番号でお客様と話せた番号。メイン（いまかける
    番号）にかけて話せなかった（connected が偽）ら、次はサブへ。サブへかけたあとは、話せても話せなくても、次はメインへ。
    called＝通話のあとの選び（偽＝予定を向け直すだけ＝`_point`：まだかけていないサブへの予定はサブのまま。⛔ 2026-11-06 の見直し：前は
    回線のせいでかけた予定のままかけ直すときも、サブへの予定をメインへ替え、つながらなかったメインへまたかけた）。
    **お客様が指定した番号**（0188。2026-11-06 ユーザー：「お客様の言った番号へ」）：直したあとの電話でお客様が「この番号へ」と言った番号
    は、前の番号でも替えない（お客様の指定が、契約者の直しより後なので先）。決めるのはデータベースの1か所（その番号は、前にかける番号
    だった番号＝superseded に入らない＝salescall.task_number_replaced_at）。"""
    if t.contact_id is None or numbers.dial is None or t.purpose not in SALES_PURPOSES:
        return t
    if t.contact_id in numbers.superseded or (called and t.contact_id == numbers.sub):
        return replace(t, contact_id=numbers.dial)
    if not connected and numbers.sub is not None and t.contact_id == numbers.dial:
        return replace(t, contact_id=numbers.sub)
    return t


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
    tries: int                   # この行で、その目的でその番号へかけた回数（お客様に届いた発信＝gate.purpose_dials の counted。今回を含む。
    #                              かける番号を直したら、新しい番号で0回から＝計画書 7-1 (15)）
    limit: int                   # 設定 retry.ask_number_again_max／retry.apology_max
    at: datetime | None          # まだ試していない時間帯の、次にかけられる時刻（無ければ None）


def decide_after_call(j: Judgement, *, cut_off: bool = False, normal: gate.Pacing, promised: gate.Pacing, rules: Rules,
                      times_with_key: int, now: datetime, target: Target, retry_at: datetime | None,
                      ask: AskAgain | None = None, forbidden: bool = False, kept: Decision | None = None,
                      resume_window: Window | None = None, resume_callee_at: datetime | None = None,
                      resume: bool | None = None, failed_at: datetime | None = None, recheck: int,
                      check_of: UUID | None = None, waiting: str | None = None, call: UUID | None = None) -> Decision:
    """通話のあとの次の動き（下の `_decide`）。recheck＝かけてよい時間帯が見つからないとき、もう一度確かめるまでの秒
    （`no_opening_recheck`。呼ぶ側が必ず渡す）。途中で切れた通話（cut_off）のあとは、かけ直す・判定待ちのどれでも、次にお客様に
    つながったときにお詫びから始める（印 cut_off を付けるのは、ここの1か所。すぐにかけ直すか＝resume とは別に決める。⛔ 見直し15：
    前は続けて3回切れると、すぐのかけ直しをやめるのと一緒にお詫びの印も外れ、次の電話でお詫びしなかった）。
    resume＝すぐにかけ直してよいか（続けて切れた数で after_call が決める。None＝cut_off と同じ）。
    failed_at＝かける番号へ、鳴らない電話が続いているときの次の時刻（`_failed_retry_at`。続いていなければ None）。
    check_of＝この通話の切れ方が決められず、確かめのかけ直しをするとき、その通話（試行。するかは `after_call` が決める。しないときは
    None）。確かめのかけ直しも、お客様が出たらお詫びから始める（印 cut_off）。
    waiting＝この通話の取り消せない決定が、まだ決まらない保留の種類（core/approval.decision_wait。決まっていれば None）。
    call＝印 cut_off を付けるときの、どの電話についてのお詫びか（`Decision.cut_call`。0187。どの電話にするかは `after_call` の1か所が決める）。"""
    d = _decide(j, cut_off=cut_off, normal=normal, promised=promised, rules=rules, times_with_key=times_with_key, now=now,
                target=target, retry_at=retry_at, ask=ask, forbidden=forbidden, kept=kept, resume_window=resume_window,
                resume_callee_at=resume_callee_at, resume=cut_off if resume is None else resume, failed_at=failed_at,
                recheck=recheck, check_of=check_of, waiting=waiting)
    if (cut_off or d.check_of) and d.kind in ("call", "hold"):
        return replace(d, apology=_plus(d.apology, "cut_off"), cut_call=call)
    return d


def _decide(j: Judgement, *, cut_off: bool, normal: gate.Pacing, promised: gate.Pacing, rules: Rules,
            times_with_key: int, now: datetime, target: Target, retry_at: datetime | None,
            ask: AskAgain | None, forbidden: bool, kept: Decision | None,
            resume_window: Window | None, resume_callee_at: datetime | None, resume: bool,
            failed_at: datetime | None = None, recheck: int, check_of: UUID | None = None,
            waiting: str | None = None) -> Decision:
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
    if waiting is not None:
        # 取り消せない決定（断り・番号違い・怒っていた）が、まだ決まらない：行を終えず、かけずに判定待ち（2026-10-30 管理者のご決定 (6)。
        # 言ってはいけないことを言った通話も、決まってから決める＝締め直しで、この順でもう一度決まる）
        return _hold({"approval": "取り消せない決定：契約者が録音とやり取りを確かめて承認するまで保留（かけない）",
                      "recording": "取り消せない決定：2つ目の判定とそろわず、録音の確かめで決まるまで保留（かけない）"}[waiting])
    if forbidden:   # 2026-10-15 ユーザー：「その行はひとまず終える。依頼主でなく、管理者へ報告。場合によっては、お詫びを入れる」
        # この通話でお客様がかけ直しを約束していた（苦情・目的を果たした・断りでない）：約束を守り、お詫びは約束の時刻の電話の頭で伝え、そのまま
        # 約束の用件を続ける（お詫びの理由 forbidden_speech。契約者がお詫びの案を承認するまで、門がその電話を止める＝gate.apology_unapproved。
        # ✅ 2026-11-08 ユーザー＝計画書 7-3 (2)「約束の時刻にお詫び」。⛔ 0215 まで：ここで先に行を終え、お詫びの電話だけかけて apology_done で
        # 終わり、約束の電話がかからなかった。0216）。「もうかけないで」と言われた通話は、いままでどおり終える（判定が do_not_call）
        if j.state == "callback_scheduled" and not j.complaint and kept is not None and kept.kind == "call" \
                and target.contact_id is not None:
            return replace(kept, apology=_plus(kept.apology, "forbidden_speech"),
                           rule=kept.rule + "（言ってはいけないことを言った：お詫びは約束の時刻の電話の頭で伝え、約束の用件を続ける）")
        return _end("forbidden_speech", "言ってはいけないことを言った：その行はひとまず終える（契約者と管理者へ知らせ、CL-1 へ修正依頼を出した）")
    # 切れ方が決められない（営業拒否の言葉も、声が届かなかった証拠も無い）：確かめのかけ直しで決まるまで、行を終えない（目的を果たした・
    # 断りの答えが取れた結果でも。断ったかもしれないお客様を、発信禁止に入れずに終えない）。すぐ確かめのかけ直し（2026-10-30 管理者の
    # 決定。するかは `after_call` が決めて check_of を渡す）。苦情があった・お客様と約束したかけ直しの通話は、ふつうの決まりで
    if check_of is not None and not j.complaint and target.purpose in ("normal", "resume", "ask_number_again") \
            and j.state != "callback_scheduled":
        again = target if target.purpose == "ask_number_again" else replace(target, purpose="resume")
        return replace(_call(promised.earliest or now, True, "切れ方が分からない：すぐ確かめのかけ直し（鳴っても出なければ営業拒否）",
                             again, resume_callee_at if resume_window else None, resume_window), check_of=check_of)
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
            return _no_opening(now, recheck, True, target)
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
        return _no_opening(now, recheck, False, target)
    return _call(max(retry_at, normal.earliest or now), False, f"再発信の方針：{key}", target)


def decide_after_denial(d: gate.Denied, *, current_promised: bool, current: Target | None, pacing: gate.Pacing,
                        now: datetime, opening: datetime | None, retry_wait: int, recheck: int,
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
    if d.reason == "number_failed_wait":   # 番号ごとの決まった順は、通話のあとと同じ1か所（`_failed_retry_at`）が after_denial で決める
        raise ValueError("番号ごとの決まった順（number_failed_wait）の次の時刻は、after_denial の _failed_retry_at が決めます")
    if d.reason in ("awaiting_decision", "not_due", "no_schedule", "task_ended", "judge_pending"):
        # 締めの処理・いまの予定に任せる（先に書き直さない）。予定の無い行・終えた行に、門の答えから書かない
        return None
    if current is None or current.contact_id is None:
        raise ValueError("いまの予定に番号がありません（かけ直す予定は必ず番号を持つ）")
    if d.kind == "permanent":
        return _end(d.reason, f"門：待っても変わらない理由（{d.reason}）")
    if d.reason == "outside_hours":
        if opening is None:
            return _no_opening(now, recheck, current_promised, current, current_callee_at, current_window)
        return _call(max(opening, pacing.earliest or opening), current_promised, "門：時間帯の外 ⇒ 次の時間帯の始まり",
                     current, current_callee_at, current_window)
    # 間隔の待ち（pacing が門と同じ時刻を答える）。門のあとで変わって、もう待たなくてよい・時刻が分からない（まれ）ときだけ、
    # 決まった間のあと（その時刻に門がもう一度確かめる。時刻の分からない待ちは候補に出さない＝gate.waits_unknown）
    at = pacing.earliest if pacing.earliest is not None and pacing.earliest > now else now + timedelta(seconds=retry_wait)
    return _call(at, current_promised, f"門：待てば変わる理由（{d.reason}）", current, current_callee_at, current_window)


def _write(tx: Tx, task_id: UUID, d: Decision, *, after: UUID | None = None) -> int:
    """決めた予定を書く（after＝その予定がどの通話のあとか＝`_append`）。かけ直す予定は、どの道で決めたものも、前の担当の方の期からいまの期へ
    移るなら新しい担当の方への形にする1か所（`_onto_dial`）を通す（道ごとの呼び忘れが起きない。0206。営業を続けないとき＝None は、その道が
    先に `_onto_dial` を通して決める。決めていなければ `_append` の守りが書かない）。"""
    # もとの予定：いまの予定か、この決定が続ける元の予定（前の担当の方に戻したときの、やめていた約束の予定＝resumes。0214）
    base = current(tx, task_id) if d.resumes is None else \
        tx.one(f"SELECT {_PLAN_COLUMNS} FROM salescall.task_next_actions n JOIN salescall.task_numbers k"
               " ON k.task_id = n.task_id AND k.contact_id = n.contact_id WHERE n.task_id = %s AND n.seq = %s", (task_id, d.resumes))
    if d.kind == "call":
        d = _onto_dial(tx, task_id, base, d, now=tx.one("SELECT now() AS now")["now"], after=after) or d
    if d.check_of is not None:   # 確かめのかけ直し：同じ行で1回だけ（判定し直しで、同じ通話のために書き直すときは、そのまま）
        tx.execute("INSERT INTO salescall.unclear_checks (tenant_id, client_id, task_id, attempt_id)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s) ON CONFLICT (task_id) DO NOTHING",
                   (task_id, d.check_of))
    if d.kind == "end":
        return end(tx, task_id, d.end_reason, cause=d.rule, apology=d.apology, cut_call=d.cut_call, after=after)
    if d.kind == "hold":
        return hold(tx, task_id, cause=d.rule, apology=d.apology, cut_call=d.cut_call, after=after)
    return _append(tx, task_id, call_at=d.at, end_reason=None, cause=d.rule, promised=d.promised, target=d.target,
                   callee_at=d.callee_at, window=d.window, apology=d.apology, cut_call=d.cut_call, after=after,
                   former_promise=d.former_promise, origin=base if d.resumes is not None else None)


# 目的のお客様に繋がらなかった理由のうち、同じ日にまたかけてよいかを聞く（判定の again）もの（1か所）：不在・取り次げない・在籍して
# いないで後任の方を追う（ほかの手段・在籍していないで終えるは、かけ直さない＝システムの方針。番号違いは番号の確かめの道）
UNREACHED_REASONS = ("absent", "not_transferred", "departed")


def _line_fault_again(tx: Tx, task_id: UUID, dialed: Target, dialed_at: datetime, now: datetime,
                      blame: str | None) -> Decision | None:
    """お客様のところで鳴らなかった発信のうち、番号のせいでないと分かったもの（`attempts.REDO_AS_PLANNED`＝回線のせい line・
    こちらのせい ours）のあとの次の動き（1か所。ふつうの電話・約束の電話・お詫びの電話のどれも）：かけなかったのと同じに、かけた予定を
    そのまま（かける先・約束・時刻・幅・お詫びの印）。回数・番号の続き・約束の回の本数のどれにも数えない。
    - line（テスト番号が鳴らなかった・回線を使わなくした＝回線のせいと分かった。0143）：いまから。回線は回線の欄が休みになる
      （DESIGN.md 17章。ほかの回線は、すぐかけられる）。2026-10-24 ユーザー：「回線の問題が明らかな場合は行は取りやめでなく一時中止
      だけ」。まだ分からないあいだは締めないので（settle.settle）、決まったときに、ここで1回だけ決める。
    - ours（こちらのプログラムが固まった・甚大な問題で終えた・通話を進める部品が落ちた＝試行の trouble。0136）：いまから。ただし門と
      候補を出す処理が、復旧するまで待たせる（行の欄の「復旧待ち」＝DESIGN.md 17章の R-5・R-6。お客様が出たあとに終えた電話のすぐの
      かけ直しと同じ。復旧＝同じ契約者で、こちらの不具合なしにお客様と話し終えた電話がある・その不具合を直したと記録され、そのあと
      プログラムが替わった）。
      2026-10-17 管理者：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」。⛔ 0141 の前は、復旧を見ずに同じお客様への
      最短の間隔だけをあけた＝お客様が出たあとの電話とは別の決め方だった）。
    それ以外（番号のせい・まだ分からない・鳴った）・かけた予定が読めなければ None（ふつうの決め方で決める）。"""
    if blame not in attempts.REDO_AS_PLANNED:
        return None
    row = _plan_at(tx, task_id, dialed_at)
    if row is None or row["kind"] != "call":
        return None
    target, window = _point(tx, task_id, dict(row, contact_id=dialed.contact_id, purpose=dialed.purpose,
                                              wrong_contact_id=dialed.wrong_contact_id), numbers_of(tx, task_id))
    target = _dialed_marks(target, row)   # いまの番号へ向ける（番号を直したあとは前の番号へかけない＝`_point`。⛔ 見直し (A) の5）
    why = ("回線のせいで鳴らなかった（テスト番号も鳴らなかった・回線を使わなくした）：回線の一時中止だけで、かけた予定のまま（どの数にも数えない）"
           if blame == "line" else
           "こちらのせいで鳴らなかった（こちらのプログラムの不具合）：かけた予定のまま、復旧次第（どの数にも数えない）")
    # 前の番号の予定がいまかける番号へ移り、相手が違えば、約束・お詫びを引き継がない（`_onto_dial` の1か所。営業の電話にしないなら、
    # ふつうの決め方で）
    return _onto_dial(tx, task_id, row, _call(now, bool(row["promised"]), why, target, row["callee_at"] if row["promised"] else None,
                                              window), now=now)


@dataclass(frozen=True)
class Check:
    """切れ方の分からなかった通話のあとの、確かめのかけ直しの答え（`check_outcome` の1か所が決める）。"""
    of: UUID                     # 切れ方の分からなかった通話（試行）
    outcome: str                 # rang＝お客様の電話まで鳴った／unrung＝鳴らなかった（状態の表の E2）／ours＝こちらの不具合で終えた（E3）
    ended_at: datetime           # 確かめのかけ直しが終わった時刻


def check_outcome(tx: Tx, attempt_id: UUID) -> Check | None:
    """その試行が、切れ方の分からなかった通話のあとの確かめのかけ直しなら、その答え（1か所。締めの処理が、判定の入力＝check_rang と
    `after_call` に渡す）。確かめのかけ直しは、確かめると決めた（表 salescall.unclear_checks）あと、その行で最初に発信まで進んだ、
    同じ番号への続き・番号の聞き直しの電話（こちらの不具合で終えた電話＝かけた予定のままかけ直す＝は数えない）。鳴ったか・鳴らなかったかは、
    お客様の電話まで鳴ったか（こちらの不具合で終えた＝状態の表の出来事 E3・鳴らなかった＝E2・電源オフや圏外のお知らせ・話し中でなければ
    鳴った）。
    確かめのかけ直しでなければ None。"""
    row = tx.one(
        "SELECT u.attempt_id AS of, c.ended_at, c.notice, c.state::text AS state FROM salescall.attempts c"
        " JOIN salescall.unclear_checks u ON u.task_id = c.task_id"
        " JOIN salescall.attempts x ON x.attempt_id = u.attempt_id"
        " WHERE c.attempt_id = %s AND c.attempt_id <> u.attempt_id AND c.claimed_at > u.decided_at AND c.contact_id = x.contact_id"
        "   AND c.purpose IN ('resume', 'ask_number_again')"
        f"  AND NOT EXISTS (SELECT 1 FROM salescall.attempts b WHERE b.task_id = c.task_id AND {attempts.dialed('b')}"
        "                    AND b.claimed_at > u.decided_at AND b.claimed_at < c.claimed_at"
        "                    AND NOT salescall.trouble_ours(b.trouble))",
        (attempt_id,))
    if row is None:
        return None
    code = health.call_event(tx, attempt_id)
    if code is None:
        return None
    if code == "E3":
        return Check(row["of"], "ours", row["ended_at"])
    # 鳴ったか＝お客様の電話まで鳴ったか（2026-10-30 管理者のご決定 (3)「鳴れば回線は元気なので、お客様が自分で切った＝営業拒否とみなします。
    # 鳴らなければ回線のせいなので、時間をおいてお客様へかけ直します（お詫びから）」）。回線が元気か（状態の表の出来事）とは別の問い：
    # 電源オフ・圏外のお知らせ（こちらの回線は元気＝0151）・話し中は、お客様の電話は鳴っていない（⛔ 見直し24c の A1：前は状態の表の
    # 出来事で決め、圏外・電源オフ・話し中で終わった確かめのかけ直しを「鳴った」として営業拒否にした）
    rang = code != "E2" and row["notice"] not in ("power_off", "out_of_range") and row["state"] != "busy"
    return Check(row["of"], "rang" if rang else "unrung", row["ended_at"])


def _unclear_check_of(tx: Tx, task_id: UUID, attempt_id: UUID | None, j: Judgement, dialed: Target, target: Target) -> UUID | None:
    """この通話のあと、確かめのかけ直しをするか（1か所）。するなら、この通話（試行）を返す：切れ方が決められなかった（judge の切れ方
    unclear）・行のお客様の通話（番号違いの方・FAX・範囲外の求めでない）・次にかける先が同じ番号・この行でまだ確かめて
    いない（同じ行で1回だけ。判定し直しで、同じ通話のために決め直すときは、する）。確かめたあとにまた決められない切れ方になったときは、
    しない（ふつうの決まりで）。"""
    if attempt_id is None or j.detail.get("cut") != "unclear" or target.contact_id != dialed.contact_id:
        return None
    if j.dnc_reason is not None or j.state == "out_of_scope" or j.detail.get("not_reached") == "wrong_alternate":
        # 電話口が番号違いの方・FAX・範囲外の求めで終えた通話：行のお客様の断りかを確かめる電話ではない（その番号はもう止めた・行は終える）
        return None
    done = tx.one("SELECT attempt_id FROM salescall.unclear_checks WHERE task_id = %s", (task_id,))
    return attempt_id if done is None or done["attempt_id"] == attempt_id else None


def _check_unrung(tx: Tx, task_id: UUID, dialed: Target, dialed_at: datetime, check: Check, now: datetime) -> Decision | None:
    """確かめのかけ直しが、お客様の電話まで鳴らなかった（1か所）：回線のせいとして（回線は状態の表が E2 で確かめる）、テスト番号を
    あける長さの並びの1つ目（初め15分。新しい設定は作らない＝core/health.first_gap）おいて、お詫びからかけ直す（2026-10-30 管理者の
    決定）。そのかけ直しは確かめのかけ直しではなく、途中で切れた電話のふつうの決まり（目的「続き」・約束の印・お詫びの印 cut_off）。
    かけた予定が読めなければ None（ふつうの決め方で）。"""
    row = _plan_at(tx, task_id, dialed_at)
    if row is None or row["kind"] != "call":
        return None
    target, window = _point(tx, task_id, dict(row, contact_id=dialed.contact_id, purpose=dialed.purpose,
                                              wrong_contact_id=dialed.wrong_contact_id), numbers_of(tx, task_id))
    target = _dialed_marks(target, row)   # いまの番号へ向ける（`_point`。⛔ 見直し (A) の5）
    at = max(now, check.ended_at + health.first_gap(tx))
    return _onto_dial(tx, task_id, row, _call(at, True, "確かめのかけ直しが鳴らなかった：回線のせいとして、決めた長さ（テスト番号を"
                                              "あける長さの1つ目）のあと、お詫びからかけ直す", target,
                                              row["callee_at"] if row["promised"] else None, window, apology="cut_off"), now=now)
    # お詫びの元の電話（cut_call）は、切れ方の分からなかった通話のまま（その通話の締めが付けた予定から引き継ぐ＝_append）


def after_call(tx: Tx, task_id: UUID, j: Judgement, *, rules: Rules, dialed: Target,
               dialed_at: datetime, alternate: Alternate | None, recheck: Recheck | None, forbidden: bool,
               cut_off: bool = False, reached_target: bool = False, blame: str | None = None,
               attempt_id: UUID | None = None, check: Check | None = None, waiting: str | None = None) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。判定の結果から次の動きを決めて記録する（同じ取引）。
    cut_off＝お客様が出たあと、お客様の意思と関係なく途中で切れた（決めるのは締めの処理の1か所）。
    dialed＝この通話でかけた先（番号・目的）、dialed_at＝かけた時刻。alternate＝お客様が言った別の番号の材料（言われたときだけ）。
    recheck＝言われた番号が番号違いだったときの番号の確かめ（そのときだけ）。reached_target＝目的のお客様と話せた（約束の日に
    かけられなかったお詫びは、ここで済んだものとして外す＝会話の処理が、予約証の印を見て最初にお詫びしている）。
    blame＝鳴らなかった発信のせい（試行のせいの欄＝core/health.blame_of。回線のせい・こちらのせいなら `_line_fault_again` の1か所で、
    かけた予定のまま）。attempt_id＝この通話（試行。確かめのかけ直しを記録する）。check＝この通話が確かめのかけ直しなら、その答え
    （`check_outcome`。鳴らなかったときは `_check_unrung` の1か所で、回線のせいとして決めた長さのあとお詫びから。こちらのせいで鳴らなかった
    ときも、回線のせいでも番号のせいでもなく、こちらで決める＝2026-10-30 管理者：「鳴らなかったら回線のせい」）。
    waiting＝取り消せない決定の保留の種類（core/approval.decision_wait。保留なら判定待ち＝`decide_after_call`）。"""
    now = tx.one("SELECT now() AS now")["now"]
    if check is not None and check.outcome == "unrung" and waiting is None and not forbidden and not j.complaint and j.state not in END_STATES:
        d = _check_unrung(tx, task_id, dialed, dialed_at, check, now)
        if d is not None:
            _write(tx, task_id, d, after=attempt_id)
            return d
    again = _line_fault_again(tx, task_id, dialed, dialed_at, now, blame)
    if again is not None:
        _write(tx, task_id, again, after=attempt_id)
        return again
    # かけた予定（発信のときに効いていた次の予定）が持っていた、お客様が言った時間の幅。いまの予定ではなく、発信の時刻までに
    # 決めた予定から読む（判定し直しのときは、いまの予定が判定待ちになっていて、幅を失うため）
    dialed_row = _plan_at(tx, task_id, dialed_at)
    dialed_window = window_of(dialed_row)
    dialed_exact = dialed_window is not None and dialed_row["callee_at"] is not None   # 時刻ちょうどの約束の回
    dialed = _dialed_marks(dialed, dialed_row)
    key = j.detail.get("policy_key")
    connected = attempt_id is None or call_report.talked_in(tx, attempt_id)   # お客様と話せたか（会話の報告の1か所）
    was_replaced = replaced_at(tx, task_id, dialed.contact_id, dialed_at)   # かけたときに前にかける番号だった番号か（0188）
    if attempt_id is not None and connected:
        tasks.record_sub_number(tx, task_id, attempt_id)   # 直したあと前の番号で話せた ⇒ その番号はサブ（0182）
    numbers = numbers_of(tx, task_id)
    # お客様が言った時刻・幅・時刻の決まっていない約束（約束の申し出のある通話が途中で切れて判定できなかったときも）
    cb = j.detail.get("callback") if j.state == "callback_scheduled" else \
        j.detail.get("promise_offer") if j.state == "unknown" and cut_off else None
    # 「担当の方は違う」を選んだ直しのあとの、前の担当の方への通話（tasks.former_person_call の1か所）で、前の担当の方が新しい約束をした：
    # その約束は前の担当の方の番号のまま書き、契約者に知らせて選んでいただく（選ぶまで門が止める。✅ 2026-11-07 ユーザー＝計画書 7-1 (23)）
    former_promise = cb is not None and attempt_id is not None and tasks.former_person_call(tx, attempt_id)
    # 契約者が「守る」を選んだ前の担当の方との約束の電話（`_kept_old_promise` の1か所）で、前の担当の方とまだ話せていない ⇒ 約束の決まりいっぱいまで、
    # その方の番号のまま（いまかける番号へ替えない。✅ 2026-11-08 ユーザー＝計画書 7-1 (32)。⛔ 0213 まで：番号の直しだけ、前の番号へ1回で移った）
    kept_old = attempt_id is not None and _kept_old_promise(tx, task_id, dialed_row)
    target = choose_number(j, dialed=dialed, alternate=alternate, recheck=recheck, cut_off=cut_off, numbers=numbers,
                           connected=connected, keep_former=former_promise or (kept_old and not reached_target))
    dialed_window = recount_on_switch(dialed_window, was_replaced, target, numbers)   # 新しい番号へ移る約束は0から数え直す
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
                              number=target.contact_id, not_today=no_today, now=now)
    failed_at = _failed_retry_at(tx, task_id, target.contact_id, now, dialed_at)
    policy = rules.retry.get(key) if key is not None else None
    retry_at = None if policy is None or policy.action != "call" else \
        _policy_at(tx, task_id, policy.interval, now, dialed_at, not_today=no_today)
    unreached = j.state == "not_reached" and j.detail.get("not_reached") in UNREACHED_REASONS
    # お客様が言った時刻・時間の幅の約束（「明日の15時」「夕方」「金曜なら」「13日なら」など）を守る次の動き（`_keep_promise` の1か所）。
    # 約束の申し出のある通話が途中で切れて判定できなかったとき（judge の promise_offer）も、同じ約束として守る（見直し15）
    kept = None
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
        # （`_exact_next_try`）。幅の約束は、この結果の再発信の方針の間隔で、その日にかけた数が1日の回数の中で、回の終わりより
        # 前だけ（`_window_round_next` の1か所。⛔ 2026-10-22 の見直し14：前は、約束の回に出なかった分まで数え、別の結果の回数と混ぜていた。
        # ⛔ 0215 まで：その回にかけた数を再発信の方針の回数と比べ、1日4回・夜21時半まで鳴らしえた）
        if no_today:   # 幅が当日を越えて続くときも、当日の分はやめる
            after = min(dialed_window.round_end or _next_day(now), _next_day(now))
        elif dialed_exact:
            after = _exact_next_try(tx, task_id, dialed_window, now, dialed_row["person_since"])
        else:
            # 目的のお客様に繋がらなかったが、あとでまたかけてよいと言われた ⇒ 目的のお客様がいま出られない＝「不在」の方針の間隔で
            # （「取り次げなかった」の方針＝日を改めて1回は、約束の電話には使わない＝約束の時刻が先）
            after = _window_round_next(tx, task_id, dialed_window, rules.retry["not_reached_absent"] if unreached else policy,
                                       now=now, dialed_at=dialed_at, period=dialed_row["person_since"])
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
    # お詫びの理由「途中で切れた」の元の電話（どの電話についてのお詫びか＝予約証の apology_when。0187）：お詫びを聞くのは目的のお客様
    # なので、目的のお客様と話していた電話が切れたら、その電話。話せずに（受付の方などと）切れた電話は、前に切れた電話があれば、
    # その電話のまま（⛔ 2026-11-06 の見直し (C) の3：前は、昨日目的のお客様に申し伝えたあと切れ、今日受付の方と話している途中で
    # 切れると、今日の電話で「先ほどは、お話の最後に…」と言った）。決めるのは、ここの1か所
    prev_cut = (current(tx, task_id) or {}).get("cut_call")
    cut_call = attempt_id if reached_target or prev_cut is None else prev_cut
    d = decide_after_call(j, cut_off=cut_off, normal=normal,
                          promised=promised, rules=rules, times_with_key=times, now=now, target=target,
                          retry_at=retry_at, ask=ask, forbidden=forbidden, kept=kept, resume_window=dialed_window,
                          resume_callee_at=dialed_row["callee_at"] if dialed_row else None, resume=resume,
                          failed_at=failed_at, recheck=no_opening_recheck(tx, task_id),
                          check_of=_unclear_check_of(tx, task_id, attempt_id, j, dialed, target), waiting=waiting,
                          call=cut_call)
    d = _apologised(d, reached_target)
    if former_promise and d.kind == "call" and d.promised:
        # 前の担当の方の新しい約束は、その方の期のまま書く（かける先が、いまの担当の方の番号でも＝名前などを「違う」で直したあと。`_plan_person`）
        d = replace(d, former_promise=True)
    # 前の担当の方への通話のあと：その方個人の答え・その方への用を使い切った・「守る」約束でその方と話した ⇒ 行を終えず、新しい担当の方へ
    # （`_after_former` の1か所。計画書 7-1 (22)(24)(32)。番号の直しの「前の番号へは1回」＝`_moved_on_fix` より先に決める）
    d = _after_former(tx, task_id, d, dialed_row, attempt_id=attempt_id, answer=judged_answer(j), complaint=j.complaint,
                      kept=kept_old, reached=reached_target, now=now)
    d = _moved_on_fix(tx, task_id, d, dialed_row, was_replaced, now=now)   # 前の番号でかからなかった ⇒ いまかける番号で
    _write(tx, task_id, d, after=attempt_id)
    if former_promise and tasks.old_promise_waiting(tx, task_id) == attempt_id:
        _notice_old_promise(tx, task_id, attempt_id)
    if reached_target and attempt_id is not None and dialed_row is not None and "forbidden_speech" in (dialed_row["apology_reasons"] or ()):
        # 約束の時刻の電話の頭で、言ってはいけないことのお詫びを伝えた（目的のお客様と話せた＝会話の処理が予約証の印を見て最初にお詫び
        # している。印を外すのは `_apologised`）：契約者に知らせる（お詫びの電話を伝えたときと同じ知らせの種類。計画書 7-3 (2)。0216）
        notify.emit(tx, "apology", key=f"apology:{attempt_id}", summary="お客様と約束した電話の頭で、お詫びを伝えました（そのまま約束の用件を続けました）。",
                    refs={"attempt_id": attempt_id, "task_id": task_id})
    return d


def _notice_old_promise(tx: Tx, task_id: UUID, attempt_id: UUID) -> None:
    """前の担当の方が新しいかけ直しの約束をした（契約者の選びを待っている＝tasks.old_promise_waiting）ことを、契約者に知らせる（その通話ごとに
    1回。✅ 2026-11-07 ユーザー＝計画書 7-1 (23)「契約者に知らせて選んでもらう」）。"""
    notify.emit(tx, "old_promise_choice", key=f"old_promise_choice:{attempt_id}", refs={"attempt_id": attempt_id, "task_id": task_id},
                summary="番号を直す前の担当の方が、新しいかけ直しの約束をしました。ダッシュボードで、前の担当の方との約束を守るか、新しい担当の方へ"
                        "かけるかを選んでください（選ぶまで、この行にはかけません）。")


def after_old_promise_choice(tx: Tx, task_id: UUID, *, keep_old_promise: bool, cause: str) -> int | None:
    """前の担当の方が新しい約束をしたあとの、契約者の選びのあとの予定（1か所。core/row_fixes.choose_old_promise だけが呼ぶ。計画書 7-1 (23)）。
    守る ⇒ いまの予定（前の担当の方の番号への、新しい約束どおりの電話）のまま（選んだので門が通す。そのあとは同じ決まり＝新しい担当の方へ
    引き継ぎ・また約束があれば選び）。新しい担当の方 ⇒ 約束をやめ、新しい番号へ引き継ぎを確かめる電話（`_to_new_person`）。書いたら、その
    予定の番号（seq）を返す。"""
    lock(tx, task_id)
    if keep_old_promise:
        return None
    now = tx.one("SELECT now() AS now")["now"]
    d = _to_new_person(tx, task_id, current(tx, task_id), cause, now=now)
    return None if d is None else _write(tx, task_id, d)


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
    （かけない理由は target_policy.block_reason の1か所）。「担当の方は違う」を選んだ直しのあとの前の担当の方の番号は、お客様が言った別の番号
    （番号が使えないときの代わり＝`usable`）に入れない（0202）。"""
    rows = tx.all("SELECT n.contact_id, n.phone_e164, n.source, t.client_ref, s.contact_id AS source_contact,"
                  "       salescall.task_former_number(n.task_id, n.contact_id, now()) AS former"
                  " FROM salescall.task_numbers n JOIN salescall.tasks t ON t.task_id = n.task_id"
                  " LEFT JOIN salescall.attempts s ON s.attempt_id = n.source_attempt_id"
                  " WHERE n.task_id = %s ORDER BY n.added_at DESC, n.phone_e164", (task_id,))
    scope = tasks.scope(tx, task_id)
    unusable = frozenset(r["contact_id"] for r in rows if health.number_dead(tx, r["contact_id"]))
    # 「担当の方は違う」を選んだ直しのあとは、前の担当の方の番号（tasks.former_numbers と同じ1か所＝salescall.task_former_number）を、新しい
    # 担当の方への代わりの番号にしない（0202。本体の指示 (2)）
    said = tuple(r["contact_id"] for r in rows if r["source"] == "callee" and not r["former"] and r["contact_id"] not in unusable
                 and target_policy.block_reason(tx, normalize(r["phone_e164"]), client_ref=r["client_ref"], **scope) is None)
    sources = tuple((r["contact_id"], r["source_contact"]) for r in rows
                    if r["source_contact"] is not None and r["source_contact"] != r["contact_id"])
    dial = tasks.dial_number(tx, task_id)["contact_id"]
    superseded = frozenset(r["contact_id"] for r in tx.all(   # 前にかける番号だった番号（データベースの1か所＝0181）
        "SELECT contact_id FROM salescall.task_numbers WHERE task_id = %s AND salescall.task_number_replaced(task_id, contact_id)",
        (task_id,)))
    sub = tx.one("SELECT salescall.task_sub_contact(%s) AS c", (task_id,))["c"]
    return Numbers(unusable, said, sources, number_recheck.asked_again_before(tx, task_id), dial=dial, sub=sub, superseded=superseded)


def _failed_retry_at(tx: Tx, task_id: UUID, contact_id: UUID | None, now: datetime, dialed_at: datetime) -> datetime | None:
    """その番号へ、お客様のところで鳴らない電話（回線の部品が受け付けなかった・お客様が出る前に終わった）が
    続いているとき、次にかける時刻（1か所）。
    続いていない・番号のせいと分かった発信で並びを使い切った（その番号へはかけない＝`usable`）ときは None。並び（設定 dial.failed_retry_days。1＝最初に失敗
    した日）の次の日（前の失敗の日から並びの差より前にはしない）が今日（か、もう過ぎた）なら、最後の失敗から同じお客様への最短の間隔をあけて。先の日なら、その日の、前と別の
    時間帯で（`_retry_at`）。2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回受け付けられ
    なかったら、その番号へはかけない」。"""
    if contact_id is None:
        return None
    # 次にかけてよい日と時刻の下限は、番号の欄（17-1-3 の「続きあり」の中身＝N-1。core/health.number_wait。門も同じものを読む＝同じ番号の
    # ほかの行も、この順を越えない。見直し22 の 9）。数えるのは番号のせいと決まった数だけ（⛔ 0129〜0141：まだ分からない電話も数えて次の日へ
    # 送り、壊れた回線の2時間で行が次の日へ送られた＝見直し21a の場面 a6）
    wait = health.number_wait(tx, contact_id)
    if wait is None:
        return None
    today = calling_hours.local(now).date()
    if wait.day <= today:
        return max(now, wait.not_before)
    return _retry_at(tx, task_id, ("days", (wait.day - today).days), now, dialed_at)


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


def _round_calls(tx: Tx, task_id: UUID, window: Window, fallback: int, period: datetime | None, *,
                 on: datetime | None = None) -> int:
    """約束の回 [since, 回の終わり) に、この行でかけた電話のうち、お客様に届いた1回の数（今回を含む。1か所）。比べる相手の再発信の方針の
    回数（`_times`）と同じ数え方＝数え方の種類 dial_kind の 'reached'（`attempts.reached`。約束の電話なので約束かどうかは見ない。
    ⛔ 見直し16b：前は「鳴った・鳴りえた電話」で数え、方針の回数と数え方が混ざっていた）。回の始まりの分からない幅は fallback
    （その結果の方針の回数）。period＝その約束の担当の期（数える範囲は `attempts.in_promise_count` の1か所。0214）。
    on＝その時刻の日（日本時間）にかけた電話だけ（幅の約束の1日の回数＝`_window_round_next`。0216）。数えるのは、その回の予定を初めて決めた
    あと（約束をした通話そのものは、お客様と話した通話なので数えない＝`_missed` と同じ。⛔ 0215 まで：回の中で約束をした通話も数えた）。"""
    if window.since is None:
        return fallback
    end = window.round_end
    return tx.one(f"SELECT count(*) AS n FROM salescall.attempts a, (SELECT %s::timestamptz AS since) p"
                  f" WHERE a.task_id = %s AND {attempts.reached('a')}"
                  f" AND {attempts.in_promise_count('a', 'p.since')}"   # その約束の期の通話（いまの期なら、番号を直したら新しい番号で数え直す）
                  " AND a.claimed_at >= greatest(%s::timestamptz, (SELECT min(n.decided_at) FROM salescall.task_next_actions n"
                  "                                               WHERE n.task_id = %s AND n.window_from = %s AND n.window_until = %s))"
                  " AND (%s::timestamptz IS NULL OR a.claimed_at < %s)"
                  " AND (%s::timestamptz IS NULL OR salescall.jst(a.claimed_at)::date = salescall.jst(%s::timestamptz)::date)",
                  (period, task_id, window.since, task_id, window.since, window.until, end, end, on, on))["n"]


def _window_round_next(tx: Tx, task_id: UUID, window: Window, policy: business_rules.Policy | None, *, now: datetime,
                       dialed_at: datetime, period: datetime | None) -> datetime:
    """お客様が言った幅の約束の電話で話せなかったあと、その回の中で次にかける時刻（回の中の本数を決める1か所。時刻ちょうどの約束は
    `_exact_next_try`）：この結果の再発信の方針の間隔（`_retry_at`）のあと。ただし、その日にかけた数（`_round_calls` の on）が1日の回数
    （業務ごとの設定 callback.window_daily_max。初め3）に届いていれば、次の日から。回の終わりを過ぎれば、回の終わり（次の回は `_keep_promise`）。
    方針がかけ直さない（終える）なら、回の終わり。✅ 2026-11-08 ユーザー＝計画書 7-3 (3)「1日3回・営業時間内」。⛔ 0215 まで：その回にかけた数を
    再発信の方針の回数と比べ、回の中で1日4回まで鳴らしえた。0216。"""
    end = window.round_end
    at = _retry_at(tx, task_id, policy.interval, now, dialed_at) if policy is not None and policy.action == "call" else None
    if at is None:
        return end or now
    cap = int(settings.resolve(tx, "callback.window_daily_max", **tasks.scope(tx, task_id)))
    if _round_calls(tx, task_id, window, 0, period, on=at) >= cap:
        at = max(at, _next_day(at))   # その日は使い切った：次の日の始まりから（その日の幅の中の次の始まりは `_keep_promise` が探す）
    return at if end is None or at < end else end


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
        f" WHERE j.task_id = %s AND j.detail ->> 'policy_key' = %s AND {attempts.counts_as_try('a')} AND {attempts.in_row_count('a')}",
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
    # いまの回の終わり（取り戻す電話の回は、その期限＝`Window.round_end`。次の回は、もとの回の [since, until) から数える）
    now_end = to_jst(window.round_end)

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
        if upcoming is not None and upcoming < now_end:
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
                  exact: bool = False, recover: bool = True) -> Decision:
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
    (5) 過ぎた回に、一度もかけられなかった ⇒ 次にお客様につながったときに、お詫びから始める（印 apology）。
    (6) 時刻ちょうどの約束で、今日の回の時間が、こちらの止め（緊急停止・回線の不具合・門の止まり）で一度もかけられないまま過ぎた ⇒ 今日のうちに
        かけられるなら、すぐお詫び（missed_promise）つきでかける（お客様指定の歯止め時間の中で。`_recover_today` の1か所）。今日かけられなければ、
        次の日の約束の時刻（(1)）。✅ 2026-11-08 ユーザー＝計画書 7-3 (4)「復旧しだい今日」。⛔ 0215 まで：次の日の同じ時刻まで待った。0216。
        recover＝偽：取り戻さない（契約者が「次の担当の方にだけ」でやめていた約束を、前の担当の方に戻したとき＝`_period_promise`。かけなかったのは
        こちらの止めでなく契約者の選びなので、次の回＝次の日の約束の時刻へ。計画書 7-1 (31) の本体の決定）。"""
    at, w2 = _window_opening(tx, task_id, after, window, now=now)
    moved = w2 is None or w2.round > window.round
    if recover and exact and moved and window.recover_until is None:
        recovered = _recover_today(tx, task_id, window, target, now=now)
        if recovered is not None:
            return recovered
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
        return replace(_no_opening(now, no_opening_recheck(tx, task_id), True, target, callee, w2), apology=apology)
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


def _recover_today(tx: Tx, task_id: UUID, window: Window, target: Target, *, now: datetime) -> Decision | None:
    """時刻ちょうどの約束の、今日の回の時間が、こちらの止め（緊急停止・回線の不具合・門の止まり）で一度もかけられないまま過ぎたとき、今日のうちに
    取り戻す電話（1か所。`_keep_promise` だけが呼ぶ）：今日の回（いまの回が今日でなければ、今日の回を `_window_opening` で探す）に、お客様のところで
    鳴った電話が1本も無い（`_missed`＝かけて出なかった回は、ふつうの決まりで次の日）・約束の日数（callback.exact_days）の中・今日のお客様指定の
    歯止め時間（callee.designated_limit）の終わりまでに予約から予約証の期限までが入る ⇒ いまから（歯止め時間の始まりより前なら、その始まり）、
    お詫び（missed_promise）つきで、その回のまま（回の数え・約束の時刻は変えない）。回の期限は、今日の歯止め時間の終わり（Window.recover_until。
    門はその期限まで、歯止め時間の中でかける＝calling_hours.in_window・callee_hours）。その1本で話せなければ、次の日の約束の時刻（`_exact_next_try`）。
    取り戻せなければ None（`_keep_promise` のふつうの決まり＝次の日の約束の時刻に、お詫びつき）。✅ 2026-11-08 ユーザー＝計画書 7-3 (4)
    「復旧しだい今日」（その日のうちにかけられるようになったら、すぐお詫びつきでかける。お客様指定の歯止めの中で。今日かけられなければ、次の日の
    約束の時刻）。0216。"""
    today = calling_hours.local(now).date()
    w = window
    if w.since is None or w.until is None:
        return None
    if calling_hours.local(w.since).date() != today:   # いまの回が今日でない（止めが日をまたいだ・見守り役が止まっていた）：今日の回を探す
        day0 = calling_hours.from_local(datetime.combine(today, datetime.min.time()))
        _, found = _window_opening(tx, task_id, day0, window, now=day0)
        if found is None or found.since is None or calling_hours.local(found.since).date() != today:
            return None   # 今日は約束の回の日でない（依頼者の休みの日など）
        w = replace(found, missed=window.missed + int(_missed(tx, task_id, window)) + max(0, found.round - window.round - 1))
    if w.until > now or not _missed(tx, task_id, w):
        return None   # 今日の回の時間がまだ終わっていない・今日の回にもうかけた（出なかった＝ふつうの決まりで次の日）
    scope = tasks.scope(tx, task_id)
    if w.round - w.missed >= int(settings.resolve(tx, "callback.exact_days", **scope)):
        return None   # 約束の日数を使い切った（ふつうの決まりで終える）
    limit = gate.callee_settings(tx, **scope).designated_limit
    midnight = datetime.combine(today, datetime.min.time())
    start = max(calling_hours.local(now), midnight + timedelta(seconds=limit[0]))
    end = midnight + timedelta(seconds=limit[1])
    if start + timedelta(seconds=int(settings.resolve(tx, "gate.claim_lifetime", **scope))) > end:
        return None   # 今日の歯止め時間の中に、もう時間が無い：次の日の約束の時刻
    rec = replace(w, recover_until=calling_hours.from_local(end))
    at = calling_hours.from_local(start)
    earliest = gate.pacing(tx, task_id, contact_id=target.contact_id, promised=True).earliest
    return _call(max(at, earliest or at), True, "お客様と約束したかけ直し（こちらの止めで約束の時刻にかけられなかった：復旧しだい今日、お詫びつきで）",
                 target, _slot_of(w)[0], rec, "missed_promise")


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
    got = calling_hours.callee_range((start, end), (True, False), limit, end - start if beyond_ok else None, made=True,
                                     business=callee.business_hours)
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


def _exact_next_try(tx: Tx, task_id: UUID, window: Window, now: datetime, period: datetime | None) -> datetime:
    """時刻ちょうどの約束の電話で話せなかった：その回の時間の中の、次にかける時刻（1か所）。かける時刻の並びは `_exact_plan`。
    本数は、その回にかけた電話のうち、お客様に届いた1回（`attempts.reached`＝ふつうの電話の方針の回数と同じ数え方 salescall.dial_kind の
    'reached'。発信に失敗・お客様が出る前の回線の失敗・すぐのお詫びのかけ直しの内の途中の切れは数えない。⛔ 見直し17c：前は「鳴りえた
    電話」で数え、お客様が出る前の回線の失敗が続くだけで、その回を使い切った）で数え、それが並びの数に届いた・回の時間が過ぎた ⇒
    回の終わり（次の回へ）。前の電話（発信まで進んだ電話すべての、いちばん新しいもの）が遅れたときも、そこから並びの最初の間（5分）は
    あける（⛔ 2026-10-22 の見直し14：前は鳴った電話だけから数え、回線の側でかけられなかった電話のあとは、すぐかけ直し続けた）。
    ⛔ 2026-10-23：前は前の電話からの間だけで決め（15:00・15:05・15:15・15:35）、4本目が30分の外に出た。
    period＝その約束の担当の期（本数に数える範囲は `attempts.in_promise_count` の1か所。ほかの期の方への電話は、その回の本数に入れない。0214。
    前の電話からあける間は、どの電話からも＝同じ会社へ続けてかけない）。
    取り戻す電話の回（window.recover_until。こちらの止めで約束の時刻にかけられなかった回を、その日のうちに＝`_recover_today`）は、その1本で回の
    終わり（話せなければ、次の日の約束の時刻。✅ 2026-11-08 ユーザー＝計画書 7-3 (4)「今日かけられなければ、次の日の約束の時刻」。0216）。"""
    if window.recover_until is not None:
        return window.recover_until
    start, end = _slot_of(window)
    gaps = _exact_gaps(tx, task_id)
    final = start + timedelta(seconds=gate.callee_settings(tx, **tasks.scope(tx, task_id)).exact_slot)   # 1回分の終わり
    plan = _exact_plan(start, final, gaps, window.round)
    dialed = tx.all(f"SELECT a.claimed_at, ({attempts.reached('a')}) AND {attempts.in_promise_count('a', 'p.since')} AS rang"
                    f" FROM salescall.attempts a, (SELECT %s::timestamptz AS since) p WHERE a.task_id = %s"
                    f" AND {attempts.dialed('a')}"
                    " AND a.claimed_at >= %s AND a.claimed_at < %s ORDER BY a.claimed_at",
                    (period, task_id, start, end))
    rang = sum(1 for d in dialed if d["rang"])
    if rang >= len(plan):
        return window.until
    nxt = plan[rang]
    if dialed:
        nxt = max(nxt, dialed[-1]["claimed_at"] + timedelta(seconds=gaps[0]))
    nxt = max(nxt, now)
    return window.until if nxt >= end else nxt


def _missed(tx: Tx, task_id: UUID, window: Window) -> bool:
    """その回（[since, 回の終わり)）に、一度もかけられなかったか：その回の予定を初めて決めたあと（約束をした通話そのものは数えない。
    まだその回の予定が無い＝約束をした通話の締めでは、回の始まりから：その回の中でお客様と話したので、お詫びは要らない）、
    お客様のところで鳴った電話（`attempts.rang`＝数え方の種類 dial_kind が「届いた1回」か「途中で切れた」。発信に失敗・お客様が出る前の
    回線の失敗は、かけられなかったのと同じ＝お詫びの印が付く。⛔ 見直し17c：前は「鳴りえた電話」で見て、回線の失敗だけの回を
    「かけた」とし、お詫びが付かなかった）が1本も無い。回の始まりの分からない幅は数えない（False）。
    数えるのは、その約束の担当の期（その回の予定を初めて決めたときの期。まだ無ければ、いまの期）の電話だけ（試行の person_since の比べ。前の
    担当の方に戻した約束の回に、やめていたあいだの新しい担当の方への電話を入れない＝計画書 7-1 (31)。0214。鳴ったかを見るだけなので、番号を
    直したあとの数え直し＝in_row_count は当てない）。回の終わりは `Window.round_end`（取り戻す電話の回は、その期限まで。0216）。"""
    if window.since is None or window.until is None:
        return False
    return tx.one(
        "WITH w AS (SELECT n.decided_at, n.person_since FROM salescall.task_next_actions n"
        "           WHERE n.task_id = %s AND n.window_from = %s AND n.window_until = %s ORDER BY n.seq LIMIT 1),"
        "     p AS (SELECT (SELECT decided_at FROM w) AS at, CASE WHEN EXISTS (SELECT 1 FROM w) THEN (SELECT person_since FROM w)"
        "                  ELSE salescall.task_person_since(%s, now()) END AS since)"
        f" SELECT NOT EXISTS (SELECT 1 FROM salescall.attempts a, p WHERE a.task_id = %s AND {attempts.rang('a')}"
        "   AND a.person_since IS NOT DISTINCT FROM p.since"
        "   AND a.claimed_at < %s AND a.claimed_at >= greatest(%s::timestamptz, p.at)) AS m",
        (task_id, window.since, window.until, task_id, task_id, window.round_end, window.since))["m"]


def _unspecified_policy(tx: Tx, task_id: UUID) -> business_rules.Policy:
    version = tx.one("SELECT salescall.run_current_version(t.run_id) AS v FROM salescall.tasks t WHERE t.task_id = %s",
                     (task_id,))["v"]
    return business_rules.load(tx, version).retry["callback_unspecified_time"]


# 時間帯を変えてかけ直す電話（番号の聞き直し・お詫びの電話）の回数の上限の設定（かけ直しの決まりは `_new_band_retry` の1か所。お詫びの電話も
# 同じ＝2026-10-30 管理者：「案A（3回）　出ない場合、連続してかけないようにすること」。お詫びの電話用の別の決まりは作らない）
_BAND_RETRY_LIMITS = {"ask_number_again": "retry.ask_number_again_max", "apology": "retry.apology_max"}


def _new_band_retry(tx: Tx, task_id: UUID, after: datetime, *, purpose: str, number: UUID | None, not_today: bool = False,
                    now: datetime | None = None) -> AskAgain:
    """別の時間帯にかけ直す電話（番号の聞き直し・お詫び）で出なかったときの材料：その目的でその番号（number＝これからかける番号）へ
    かけた回数と試した時間帯（門の数え方。かける番号を直したら、新しい番号で0回から＝gate.purpose_dials の counted。計画書 7-1 (15)）・
    まだ試していない時間帯の次にかけられる時刻。not_today＝同じ日にはかけ直さない（`not_today` の1か所。次の日から探す）。"""
    scope = tasks.scope(tx, task_id)
    get = lambda key: settings.resolve(tx, key, **scope)   # noqa: E731
    boundaries = list(get("retry.time_band_boundaries"))
    dials = gate.purpose_dials(tx, task_id, purpose=purpose, number=number)
    counted = [d for d in dials if d.counted]   # 回数・試した時間帯（最小の間隔は、どの発信からも＝同じお客様なので数え直さない）
    # 回数・試した時間帯：お客様に届いた発信だけ（gate.purpose_dials＝ふつうの電話の回数と同じ決まり salescall.dial_kind の1か所。
    # 回線の都合で繋がらなかった時間帯は、まだ試していない）。鳴らない電話が続く番号は、ここで数えず、番号ごとの歯止めで止める
    # （`usable`。⛔ 見直し15：前は発信まで進んだもの全部を数え、ふつうの電話と決まりが違った）。
    # 最小の間隔：お客様と約束したかけ直しは門の最小の間隔の対象外なので、この電話は、前の同じ目的の発信から間を空ける（あけた時刻は
    # 門の数え方の1か所＝gate.same_contact_free_at。⛔ 見直し23 の (8)：前はここで同じ設定を別に読んでいた）
    local = calling_hours.local(after)
    if not_today:
        local = max(local, calling_hours.local(_next_day(now or after)))
    if dials:
        local = max(local, gate.same_contact_free_at(tx, scope, max(d.ended for d in dials)))
    hours, length = gate.calling_hours_for(tx, **scope), timedelta(seconds=int(get("gate.claim_lifetime")))
    try:
        found = calling_hours.next_opening_in_new_band(hours, holiday_checker(tx), local, length, boundaries,
                                                       {calling_hours.band_of(d.at, boundaries) for d in counted})
        if found is None:   # 試していない時間帯が無い（夜だけ開くお店など）：直後にかけ直さず、翌日以降の同じ頃から
            found = calling_hours.next_opening(hours, holiday_checker(tx), local + timedelta(days=1), length)
        at = None if found is None else calling_hours.from_local(found)
    except CalendarOutOfRange:   # 暦の無い日に当たる：決めつけず、待ってから門にもう一度確かめさせる
        at = after + timedelta(seconds=int(get("gate.denied_retry_wait")))
    return AskAgain(len(counted), int(get(_BAND_RETRY_LIMITS[purpose])), at)


def schedule_apology(tx: Tx, task_id: UUID, contact_id: UUID, *, cause: str, source: UUID | None) -> int:
    """お詫びの電話の予定を書く（core/apology.py の `schedule_if_ready` だけが呼ぶ）。いまから、その番号へ。お客様と約束したかけ直しと同じく、
    回数の上限・1日の回数・最小の間隔では止めない（お詫びは営業の電話ではない）。時間帯・NG時間・発信禁止は門が止める。source＝言ってはいけない
    ことを言った通話（お詫びの電話は、その通話の担当の方への電話＝予定の担当の期の元。`_plan_person`）。"""
    return _append(tx, task_id, call_at=None, end_reason=None, cause=cause, call_now=True, promised=True,
                   target=Target(contact_id, purpose="apology"), after=source)


def decide_after_apology(*, delivered: bool, refused: bool, retry: AskAgain, promised: gate.Pacing,
                         target: Target, now: datetime, stop_reason: str | None = None, cut_off: bool = False,
                         failed_at: datetime | None = None, recheck: int) -> Decision:
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
        return _no_opening(now, recheck, True, target)
    return _call(max(retry.at, promised.earliest or retry.at), True,
                 f"お詫びの電話：届かなかった ⇒ 別の時間帯に（{retry.tries + 1} 回目）", target)


def after_apology(tx: Tx, task_id: UUID, *, delivered: bool, refused: bool, dialed: Target,
                  stop_reason: str | None = None, reached_target: bool = False,
                  not_reached: str | None = None, again: bool = False, dialed_at: datetime | None = None,
                  blame: str | None = None, attempt_id: UUID | None = None) -> Decision:
    """締めの処理（core/settle.py）だけが呼ぶ。お詫びの電話のあとの次の動きを決めて記録する（同じ取引）。
    reached_target＝目的のお客様と話せた（約束の日にかけられなかったお詫びの印を外す＝`_apologised`）。途中で切れて、すぐかけ直して
    よいかは、締めの処理が試行に残した「切れたか」を、データベースの resume_allowed の1か所が読む。not_reached・again＝目的のお客様に繋がらなかった理由と、同じ日にまた
    かけてよいと言われたか（会話の報告。同じ日にかけ直さないかは `not_today` の1か所＝ふつうの電話と同じ）。
    blame＝鳴らなかった発信のせい（dialed_at＝かけた時刻。ふつうの電話と同じ `_line_fault_again` の1か所）。attempt_id＝このお詫びの電話。
    **前の担当の方へのお詫びの電話**（その通話の担当の期が、締めたときの期と違う＝tasks.former_person_call の1か所。0203）で行を終える決定
    （お詫びを伝えた・届かなかった・断られた・その番号へかけない）になったら、行を終えず、新しい担当の方への形（`_to_new_person`。言ってはいけない
    ことを言った通話の答えで行を終えていれば終える＝`source_ends_row`：会社としての答えなど）。✅ 2026-11-07 本体の決定（計画書 7-1 (12)(22)(24)
    にそろえる）：前の担当の方へのお詫びは、その方個人のこと。届いても、届かなかったときと同じく、新しい担当の方へ引き継ぎを確かめる営業を続ける。
    ⛔ 0202：お詫びが届いたときだけ行がお詫び済みで終わり、新しい担当の方へかからなかった（第三者の見直しの指摘6）。"""
    now = tx.one("SELECT now() AS now")["now"]
    redo = _line_fault_again(tx, task_id, dialed, dialed_at, now, blame) if dialed_at is not None else None
    if redo is not None:
        _write(tx, task_id, redo, after=attempt_id)
        return redo
    promised = gate.pacing(tx, task_id, contact_id=dialed.contact_id, promised=True)
    retry = _new_band_retry(tx, task_id, max(now, promised.earliest or now), purpose="apology", number=dialed.contact_id,
                            not_today=not_today(None if reached_target else not_reached, again), now=now)
    target = usable(dialed, numbers_of(tx, task_id))
    d = decide_after_apology(delivered=delivered, refused=refused, retry=retry,
                             promised=promised, target=target, now=now, stop_reason=stop_reason,
                             cut_off=attempts.resume_allowed(tx, task_id),
                             failed_at=_failed_retry_at(tx, task_id, target.contact_id, now, now),
                             recheck=no_opening_recheck(tx, task_id))
    d = _apologised(d, reached_target)
    # 直したあと前の番号へかけたお詫びの電話が、番号のせいで終える決定になった ⇒ 終えずに、いまかける番号で（同じ相手ならお詫び・違う
    # 相手なら営業を続ける＝`_restart` の1か所。通話のあと・門のあとと同じ `_moved_on_fix`。⛔ 0194 の前は、お詫びの電話のあとだけ通らず、
    # 行が終わった）
    at = dialed_at or now
    row = _plan_at(tx, task_id, at)
    # 前の担当の方へのお詫びの電話のあと、行を終える決定 ⇒ 新しい担当の方へ（通話のあとと同じ `_after_former` の1か所。0214）
    d = _after_former(tx, task_id, d, row, attempt_id=attempt_id, apology=True, now=now)
    d = _moved_on_fix(tx, task_id, d, row, replaced_at(tx, task_id, dialed.contact_id, at), now=now)
    _write(tx, task_id, d, after=attempt_id)
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
    now_target = None if cur is None or cur["kind"] != "call" else _plan_target(cur)
    # いまの予定の番号が、前にかける番号だった番号か（データベースの1か所 salescall.task_number_replaced_at を `replaced_at` で読む。下の
    # 2か所＝新しい番号へ移る約束の数え直しと、前の番号でかからなかったときの移り先が、同じ答えを使う。⛔ 2026-11-06 の見直し：前は
    # 片方が Python の集まり numbers.superseded、片方がデータベースの関数で見ていた）
    was_replaced = now_target is not None and replaced_at(tx, task_id, now_target.contact_id, now)
    if denied.reason == "number_unusable" and now_target is not None:
        # その番号は、鳴らない電話が決まった数だけ続いた（番号の欄が「使えない」。門と同じ）：お客様が言った別の番号へ替えるか、行を
        # 終える（通話のあとと同じ `usable` の1か所）
        numbers = numbers_of(tx, task_id)
        t = usable(now_target, numbers)
        window = recount_on_switch(window, was_replaced, t, numbers)   # 新しい番号へ移る約束は0から
        if t.contact_id is None:
            d = _end(t.end_reason, "門：その番号は、鳴らない電話が続いた（もうかけない）")
        elif t.purpose == "ask_number_again":   # お客様が言った番号に繋がらない：言われた通話の番号へ、番号を伺い直す（すぐ。
            # 番号の聞き直しは約束の印を必ず持つ＝データベースの salescall.needs_promise_mark の1か所）
            d = _call(now, True, "門：お客様が言った番号は、鳴らない電話が続いた ⇒ 言われた通話の番号へ、番号を伺い直す", t)
        else:
            d = _call(now, bool(cur["promised"]), "門：その番号は、鳴らない電話が続いた ⇒ お客様が言った別の番号へ", t,
                      cur["callee_at"], window)
        # 直したあと前の番号へかける電話（まだかけていなかった約束の電話・お詫びの電話）を、門がその番号のせいで止めた ⇒ いまかける番号で
        # （ほかの門の理由・通話のあと・お詫びの電話のあとと同じ1か所。⛔ 0194 の前は、ここだけ通らず、行が終わった）。前の担当の方の期の予定なら、
        # 先に、新しい担当の方へ移すか（`_after_former` の1か所。0214）
        d = _moved_on_fix(tx, task_id, _after_former(tx, task_id, d, cur, now=now), cur, was_replaced=was_replaced, now=now)
        _write(tx, task_id, d)
        return d
    if denied.reason == "number_failed_wait" and now_target is not None:
        # 鳴らない電話が続く番号の、番号ごとの決まった順の前（番号の欄の中身。門と同じ core/health.number_wait の1か所）：通話のあとと同じ
        # 1か所（`_failed_retry_at`・`_not_before_failed`）で、いまの予定（かける先・約束・時刻・幅・お詫びの印）を、その順より前にしない
        wait = health.number_wait(tx, now_target.contact_id)
        failed_at = _failed_retry_at(tx, task_id, now_target.contact_id, now, now if wait is None else wait.last_end)
        planned = _call(cur["call_at"], bool(cur["promised"]), "門：鳴らない電話が続く番号（番号ごとの決まった順）", now_target,
                        cur["callee_at"], window)
        d = _not_before_failed(tx, task_id, planned, failed_at, now=now) or planned
        _write(tx, task_id, d)
        return d
    if denied.reason in ("outside_hours", "callback_window") and window is not None and now_target is not None:
        # お客様が言った時間の幅の約束：幅の中の次の始まり・話せなかった回のあとは理由に合わせた次の回（`_keep_promise` の1か所）
        d = _keep_promise(tx, task_id, now, window, now_target, now=now, exact=cur["callee_at"] is not None)
        d = _after_former(tx, task_id, d, cur, now=now)   # 前の担当の方との「守る」約束を使い切った ⇒ 新しい担当の方へ（7-1 (32)。0214）
        _write(tx, task_id, d)
        return d
    if denied.reason == "outside_hours":   # 次の時間帯の始まり（門の1日の回数の待ちと同じ gate.next_opening の1か所。暦の無い
        # 日に当たれば、決めつけず次の日の始まり＝その日に門がもう一度確かめる。⛔ 見直し17a：前は決まった間のあとへ書き直し続けた）
        opening = gate.next_opening(tx, now, tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    d = decide_after_denial(denied, current_promised=bool(cur and cur["promised"]), current=now_target,
                            pacing=gate.pacing(tx, task_id), now=now, opening=opening, retry_wait=retry_wait,
                            recheck=no_opening_recheck(tx, task_id),
                            current_callee_at=cur["callee_at"] if cur else None, current_window=window)
    if d is not None:
        # 直したあと前の番号へかける約束の電話を、門がその番号のせいで止めた ⇒ いまかける番号で（通話のあとと同じ1か所）
        d = _moved_on_fix(tx, task_id, _after_former(tx, task_id, d, cur, now=now), cur, was_replaced, now=now)
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
    said = len(_laters(tx, task_id))   # 「あとで」と言われた回数（言った回数なので、数える発信かに関係なく数える。戻すかで見る並びと同じ）
    calls = _unspecified_calls(tx, task_id)
    if p.action != "end" and said > p.max_times:
        # お客様が「あとで」を方針の回数より多く言った：番号を直したとき、その「あとで」が約束の一環か営業拒否かで戻すかが決まる
        # （`reopens_on_fix`。計画書 7-1 (13)）ので、決めた回数かけて終えた行と終わりの理由を分ける（0194）
        return replace(_end(LATER_OVER, f"時刻の決まっていない約束：お客様が「あとで」を再発信の方針の回数（{p.max_times} 回）より多く"
                                         "言った"), apology=apology)
    if p.action == "end" or calls >= p.max_times:
        return replace(_end("callback_unspecified_time",
                            f"時刻の決まっていない約束：再発信の方針（{p.max_times} 回まで）で終える"), apology=apology)
    at = _policy_at(tx, task_id, p.interval, base, tried, not_today=not_today)
    if at is None:
        return replace(_no_opening(now, no_opening_recheck(tx, task_id), True, target), apology=apology)
    return _call(max(at, gate.pacing(tx, task_id, contact_id=target.contact_id, promised=True).earliest or at), True,
                 "お客様と約束したかけ直し（時刻の決まっていない約束：再発信の方針 callback_unspecified_time の間隔で）",
                 target, None, None, apology)


def _plan_at(tx: Tx, task_id: UUID, at: datetime) -> dict[str, Any] | None:
    """その時刻に効いていた次の予定（その時刻までに決めた、いちばん新しい予定。1か所）。かけた電話が守っていた予定を読むのに使う
    （いまの予定ではない：判定し直しのときは、いまの予定が判定待ちになっていて、幅を失うため）。"""
    return tx.one(
        "SELECT seq, kind, window_until, window_daily, window_days, window_from, window_repeat, window_round, window_missed,"
        "       window_said, window_day_said, window_recover_until, callee_at, promised, successor, handover, contact_id, purpose,"
        "       wrong_contact_id, cut_call, person_since, apology_reasons,"
        "       salescall.number_again_of(purpose, number_again) AS number_again"   # 前の予定の読み替えは1か所（0136）
        " FROM salescall.task_next_actions WHERE task_id = %s AND decided_at <= %s ORDER BY seq DESC LIMIT 1",
        (task_id, at))


def _dialed_marks(dialed: Target, row: dict[str, Any] | None) -> Target:
    """かけた電話の印（後任の方を追う・番号を伺い直す理由）を、かけたときに効いていた予定から写す（1か所。試行には持たない印。引き継ぎを
    確かめる印は写さない：予定を書くたびに `_handover_due` が求める＝0203）。
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
        f" AND {attempts.in_promise_count('a')}"   # 前の担当の方との「守る」約束の電話は、その方の番号で数える（0214）
        " AND a.claimed_at > coalesce((SELECT max(c.claimed_at) FROM salescall.attempts c"
        "   JOIN salescall.call_judgements r ON r.attempt_id = c.attempt_id"
        "   WHERE c.task_id = %s AND r.business_state = 'callback_scheduled'), '-infinity')",
        (task_id, task_id))
    return sum(_served_unspecified(tx, task_id, r["claimed_at"], r["purpose"]) for r in rows)
