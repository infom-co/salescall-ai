"""契約者・管理者への知らせの送信（2章 Q23 の送る側。持ち主 CL-3。docs/DASHBOARD.md 5-4）。

出来事を台帳に入れるのは core の `notify.emit`（CL-2）。ここは、台帳の出来事を送り、送り方ごとの記録（notification_deliveries）を書くだけ。
ダッシュボードの「知らせ」の画面は、台帳と記録をそのまま読む（画面に出すこと自体は送信ではない）。

決まり（1か所ずつ）：
- **送る順**：`ORDER`。✅ 2026-11-18 管理者「Rakuten Link が最優先、それが使えなければ、チャットワーク」。
- **1つの出来事は1回届けば終わり**（どれかの送り方で sent になったら、もう送らない）。決めるのは `next_step` の1か所。
- **取引の外で送る**：読み書きの層は、ぶつかると取引を最初からやり直すので、取引の中で送ると同じ知らせが2通届きうる。⇒ (1) 取引で次の
  一手を決める (2) 取引の外で1回だけ送る (3) 別の取引で結果を記録する。送る処理は同時に1つだけ（`Access.exclusive`）。
- **文は短く**：日本語のショートメールは1通70文字まで（超えると何通分にも分かれ、その分の料金がかかる）。`text_for` の1か所で作る。
  お客様の電話番号・名前・発話の中身は入れない（台帳の要約は、もともと入れない作り＝CL-2 4-46 の5）。
- **送れなくても止めない**：決めた回数（呼ぶ側が設定から渡す）を使い切ったら gave_up を記録し、画面の「知らせ」に「送れていません」と出す。
  システムの動き（止める・解く・発信）は変わらない。

⏳ まだ無いもの（CL-2・電話接続の担当へのお願い。docs/team/CL-3.md 4章）：送り方「chatwork」の記録の欄（いまの表は sms・dashboard
だけ）／送り先（契約者の番号・チャットワークの部屋）の設定／回数の設定／Rakuten Link でショートメールを送る、PC の部品への頼み方／
見守り役の仕事への組み込み。送り方の部品（`Transport`）は、それぞれができたときに作る。

置き場の決まり：この部品はサーバー（中央の見守り役）で動かす。契約者の PC には電話をつなぐ部分だけを置く（PC で動かすとプログラムの中身を
盗まれる恐れがあるため＝2026-10-21 管理者）。Rakuten Link のショートメールの `Transport` は、作り終えた文を PC の部品に渡して1回送らせる
だけにし、送る順・文・送り直し・記録はここから動かさない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from salescall.core import calling_hours
from salescall.core.access import Access, Actor, Tx
from salescall.output import words

ORDER: tuple[str, ...] = ("sms", "chatwork")   # 送る順（sms＝Rakuten Link のショートメール）
SMS_LIMIT = 70                                   # 日本語のショートメール1通の文字数
HEAD = "【SalesCall】"
TAIL = "…詳しくは画面で"


class SendFailed(Exception):
    """送れなかった（理由は記録に残す。キーの値・お客様の情報を入れない）。"""


class Transport(Protocol):
    """送り方の部品：文を1回送る。送れなければ SendFailed。"""
    def send(self, text: str) -> None: ...


@dataclass(frozen=True)
class Step:
    channel: str
    action: str          # "send"＝送る／"give_up"＝回数を使い切ったと記録する
    attempt_no: int


def text_for(kind: str, summary: str) -> str:
    """知らせの文（1か所）。70文字に収まらなければ、切って「…詳しくは画面で」を付ける。"""
    body = f"{HEAD}{words.notice_kind(kind)}：{' '.join(summary.split())}"
    if len(body) <= SMS_LIMIT:
        return body
    return body[:SMS_LIMIT - len(TAIL)] + TAIL


def next_step(history: Sequence[tuple[str, str]], ready: Sequence[str], max_tries: int) -> Step | None:
    """次の一手（1か所）。history＝これまでの記録 [(送り方, 状態)]、ready＝いま使える送り方、max_tries＝送り方ごとの回数。

    - どれかで sent なら、もう何もしない（1回届けば終わり）。
    - まだ使い切っていない送り方のうち、失敗のいちばん少ないもの（同じなら ORDER の順）で送る。⇒ Rakuten Link が先。失敗したら、
      次はチャットワーク。両方失敗したら、また Rakuten Link から（回数の内で）。
    - 使える送り方がすべて回数を使い切ったら、まだ gave_up を書いていない送り方に gave_up を書く。全部書いたら、何もしない。"""
    if any(status == "sent" for _, status in history):
        return None
    if max_tries < 1:
        raise ValueError("回数は1以上")
    failed = {ch: sum(1 for c, st in history if c == ch and st == "failed") for ch in ORDER}
    gave_up = {ch for ch, st in history if st == "gave_up"}
    usable = [ch for ch in ORDER if ch in ready and ch not in gave_up]
    trying = [ch for ch in usable if failed[ch] < max_tries]
    if trying:
        ch = min(trying, key=lambda c: (failed[c], ORDER.index(c)))
        return Step(ch, "send", _attempt_no(history, ch))
    for ch in usable:
        return Step(ch, "give_up", _attempt_no(history, ch))
    return None


STATE_WORDS = {"sent": "送りました", "trying": "送り直しています", "gave_up": "送れていません", "none": "まだ"}


def delivery_state(statuses: Sequence[str]) -> str:
    """その知らせが届いたか（画面の「届いたか」の1か所）：どれかで sent＝送りました／まだ届かず、使い切った（gave_up）＝送れていません／
    失敗があり、まだ送り直す＝送り直しています／記録が無い＝まだ。⛔ 2026-11-18：画面で「失敗が1つでもあれば送れていません」としていた
    （Rakuten Link で失敗してチャットワークで届いた知らせまで「送れていません」になる）。"""
    if "sent" in statuses:
        return "sent"
    if "gave_up" in statuses:
        return "gave_up"
    return "trying" if "failed" in statuses else "none"


# ── いつ送るか（✅ 2026-11-19 管理者「(1) (2)はすぐ　そのほかはそれでいい。ダッシュボードには全てすぐに報告」）──────────────
# CL-3 の案（同じ日に伺った）：(1) 停止・再開と緊急のものは、いつでもすぐ (2) 承認待ち（契約者が決めること）はすぐ（管理者のご決定で「すぐ」）
# (3) 電話の中で起きた問題 (4) 仕組み・機械の不具合 (5) お金 は、送ってよい時間ならすぐ。外（夜中・休みの日）に起きた分は、次に時間が開いたときに
# 1通にまとめて送る (6) そのほかの報告は、毎朝（その日の最初に時間が開いたとき）の1通にまとめる。ダッシュボードの「知らせ」には、どれも起きたときに
# すぐ出す（画面に出すのは送信ではない＝台帳をそのまま読む）。送ってよい時間は契約者ごとの設定 `SEND_HOURS`（曜日ごと・祝日ごとの時間の幅＝
# かける時間帯と同じ形。休みの日もこれで表す。⏳ 設定の定義は CL-2 へのお願い 52。無い間は、どれもすぐ送る＝知らせを止めて取りこぼさない側）。
NOW, DAYTIME, MORNING = "now", "daytime", "morning"
TIMING: dict[str, str] = {
    # (1) 停止・再開
    "stopped": NOW, "resumed": NOW, "emergency_route": NOW,
    # (2) 契約者に決めていただくこと（承認待ち・選ぶこと。言ってはいけないことの知らせは、お詫びの電話の案の承認を求める）
    "forbidden_speech": NOW, "old_promise_choice": NOW, "more_calls": NOW, "correction_call": NOW, "month_ahead_approval": NOW,
    "month_ahead_unanswered": NOW, "solution_held": NOW, "solution_earlier": NOW, "solution_unconfirmed": NOW, "decision_approval": NOW,
    "recording_needed": NOW, "recording_list_needed": NOW,
    # (3) 電話の中で起きた問題
    "complaint": DAYTIME, "judge_unknown": DAYTIME, "declared_after_settle": DAYTIME, "decision_assumed": DAYTIME,
    # (4) 仕組み・機械の不具合
    "error_notice": DAYTIME, "calendar_sync_failed": DAYTIME, "calendar_next_year_missing": DAYTIME, "program_update": DAYTIME,
    "backup_failed": DAYTIME, "pc_down": DAYTIME, "line_undecided": DAYTIME, "task_resting": DAYTIME, "line_none_to_switch": DAYTIME,
    "pc_line_conflict": DAYTIME, "ai_version_missing": DAYTIME, "ai_version_unchecked": DAYTIME, "version_not_derived": DAYTIME,
    "setting_missing": DAYTIME,
    # (5) お金
    "budget": DAYTIME, "deficit_warning": DAYTIME,
    # (6) そのほかの報告
    "quality_stage": MORNING, "answer_rate_drop": MORNING, "sample_ready": MORNING, "ai_newer_version": MORNING, "apology": MORNING,
}
SEND_HOURS = "notice.send_hours"   # 知らせを送ってよい時間（契約者ごと。値の形は calling.hours と同じ weekly_hours）


@dataclass(frozen=True)
class SendTime:
    at: datetime        # 送る時刻（日本時間・時差なし）
    bundled: bool       # まとめて1通で送る（`deliver_bundle`）か、1件ずつ送る（`deliver_event`）か


def send_time(kind: str, urgent: bool, occurred: datetime, hours: dict | None, is_holiday: Callable[[date], bool]) -> SendTime:
    """その知らせをいつ・どう送るか（1か所。送る処理と、知らせの画面の「送る予定」が同じものを読む）。occurred＝起きた時刻（日本時間・時差なし）。
    hours＝送ってよい時間（設定 `SEND_HOURS`。無ければ None＝すぐ送る）。"""
    timing = TIMING.get(kind, DAYTIME)
    if urgent or timing == NOW or hours is None:
        return SendTime(occurred, False)
    if timing == DAYTIME:
        if calling_hours.covers(hours, is_holiday, occurred, occurred):
            return SendTime(occurred, False)
        opens = calling_hours.next_opening(hours, is_holiday, occurred, timedelta(0))
        return SendTime(opens, True) if opens is not None else SendTime(occurred, False)
    morning = _next_morning(hours, is_holiday, occurred)
    return SendTime(morning, True) if morning is not None else SendTime(occurred, False)


def _next_morning(hours: dict, is_holiday: Callable[[date], bool], after: datetime, horizon_days: int = 31) -> datetime | None:
    """after より後の、その日の最初に送ってよい時間が開く時刻（毎朝の1通の時刻）。"""
    for offset in range(horizon_days + 1):
        d = after.date() + timedelta(days=offset)
        starts = sorted(calling_hours.clock_seconds(a) for a, _ in hours[calling_hours.day_key(d, is_holiday)])
        if starts:
            first = datetime.combine(d, time()) + timedelta(seconds=starts[0])
            if first > after:
                return first
    return None


def schedule(tx: Tx, tenant_id: Any | None) -> Callable[[dict[str, Any]], SendTime]:
    """取引の中で、知らせ（kind・urgent・occurred_at を持つ行）の `send_time` を答える関数を作る（送る処理と知らせの画面が使う1か所）。
    送ってよい時間は、契約者あて＝その契約者の値・管理者あて（tenant_id＝None）＝システムの値。祝日の暦が無い年は、曜日で決める
    （暦が無いことで知らせを止めない。暦が無いことは別の知らせ calendar_next_year_missing が伝える）。"""
    from salescall.core import calendar, settings
    try:
        hours = settings.resolve(tx, SEND_HOURS, tenant_id=tenant_id, client_id=None, business_id=None)
    except settings.SettingMissing:   # ⏳ 設定の定義がまだ無い（CL-2 へのお願い 52）＝すぐ送る
        hours = None
    check = calendar.holiday_checker(tx)

    def is_holiday(d: date) -> bool:
        try:
            return check(d)
        except calendar.CalendarOutOfRange:
            return False
    return lambda e: send_time(e["kind"], bool(e["urgent"]), calling_hours.local(e["occurred_at"]), hours, is_holiday)


def bundle_text(kinds: Sequence[str]) -> str:
    """まとめて送る1通の文（1か所。70文字に収める）：何件か・種類ごとの件数。詳しくは画面で。"""
    counts: dict[str, int] = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    parts = "・".join(f"{words.notice_kind(k)}{'×' + str(n) if n > 1 else ''}" for k, n in counts.items())
    body = f"{HEAD}知らせ{len(kinds)}件：{parts}"
    return body if len(body) <= SMS_LIMIT else body[:SMS_LIMIT - len(TAIL)] + TAIL


def _attempt_no(history: Sequence[tuple[str, str]], channel: str) -> int:
    return sum(1 for c, _ in history if c == channel) + 1


def _history(tx: Tx, event_id: int) -> list[tuple[str, str]]:
    return [(r["channel"], r["status"]) for r in tx.all(
        "SELECT channel, status FROM salescall.notification_deliveries WHERE event_id = %s ORDER BY delivery_id", (event_id,))]


def _record(tx: Tx, event_id: int, step: Step, status: str, error: str | None) -> None:
    tx.execute("INSERT INTO salescall.notification_deliveries (event_id, channel, status, attempt_no, error)"
               " VALUES (%s, %s, %s, %s, %s)", (event_id, step.channel, status, step.attempt_no, error))


def deliver_event(access: Access, actor: Actor, tenant_id: Any | None, event_id: int,
                  transports: Mapping[str, Transport], max_tries: int) -> str:
    """出来事を1つ送る（1回の一手だけ。呼ぶ側が、送り終わるまで繰り返す）。答え：sent／failed／gave_up／done（もう済み）／bundled（まとめて
    送る知らせ＝`deliver_bundle` が送る）。
    契約者あて（tenant_id あり）は契約者の取引で、管理者あて（for_admin。tenant_id＝None）は全体の取引で読み書きする（管理者あては
    全体の取引でしか読めない決まり＝0084）。送る順・文・送り直し・記録は、どちらもこの1か所。
    送る処理は同時に1つだけ（`Access.exclusive`。取れなければ busy＝ほかの処理が送っている）。"""
    def in_tx(work: Any) -> Any:
        return access.in_system(actor, work) if tenant_id is None else access.in_tenant(actor, tenant_id, work)

    with access.exclusive(f"notice:{event_id}") as mine:
        if not mine:
            return "busy"

        def plan(tx: Tx) -> tuple[Step | None, dict[str, Any] | None, bool]:
            e = tx.one("SELECT kind, summary, urgent, occurred_at FROM salescall.notification_events WHERE event_id = %s", (event_id,))
            if e is None:
                raise LookupError(f"その知らせは無い（または範囲の外）：{event_id}")
            return next_step(_history(tx, event_id), list(transports), max_tries), e, schedule(tx, tenant_id)(e).bundled

        step, event, bundled = in_tx(plan)
        if step is None:
            return "done"
        if bundled:   # 送ってよい時間の外の (3)〜(5)・毎朝の (6)：1件ずつは送らず、まとめた1通で送る（`deliver_bundle`）
            return "bundled"
        if step.action == "give_up":
            in_tx(lambda tx: _record(
                tx, event_id, step, "gave_up", f"{max_tries}回送っても届きませんでした"))
            return "gave_up"
        try:
            transports[step.channel].send(text_for(event["kind"], event["summary"]))   # 取引の外で1回だけ
        except SendFailed as e:
            reason = str(e).strip() or "理由が返っていません"
            in_tx(lambda tx: _record(tx, event_id, step, "failed", reason))
            return "failed"
        in_tx(lambda tx: _record(tx, event_id, step, "sent", None))
        return "sent"


def deliver_bundle(access: Access, actor: Actor, tenant_id: Any | None, transports: Mapping[str, Transport], max_tries: int,
                   *, days: int = 7) -> dict[str, int]:
    """まとめて送る知らせ（`send_time` の bundled）のうち、送る時刻が来たものを、1通にまとめて送る（1回の一手。呼ぶ側が決まった間隔で呼ぶ）。
    送る順・送り直し・回数・記録は1件ずつと同じ（知らせごとに `next_step`。同じ一手になった知らせを1通にまとめ、送れたら・送れなかったら、
    含めた知らせのそれぞれに記録する）。契約者あて（tenant_id）と管理者あて（None）は別々に呼ぶ。答え：一手ごとの件数。"""
    def in_tx(work: Any) -> Any:
        return access.in_system(actor, work) if tenant_id is None else access.in_tenant(actor, tenant_id, work)

    with access.exclusive(f"notice-bundle:{tenant_id or 'admin'}") as mine:
        if not mine:
            return {"busy": 1}

        def plan(tx: Tx) -> dict[tuple[str, str], list[tuple[int, str, Step]]]:
            when = schedule(tx, tenant_id)
            now = calling_hours.local(tx.one("SELECT now() AS t")["t"])
            groups: dict[tuple[str, str], list[tuple[int, str, Step]]] = {}
            for e in tx.all("SELECT event_id, kind, urgent, occurred_at FROM salescall.notification_events"
                            " WHERE for_admin = %s AND occurred_at > now() - make_interval(days => %s) ORDER BY occurred_at, event_id",
                            (tenant_id is None, days)):
                st = when(e)
                if not st.bundled or st.at > now:
                    continue
                step = next_step(_history(tx, e["event_id"]), list(transports), max_tries)
                if step is not None:
                    groups.setdefault((step.channel, step.action), []).append((e["event_id"], e["kind"], step))
            return groups

        done: dict[str, int] = {}
        for (channel, action), items in in_tx(plan).items():
            if action == "give_up":
                in_tx(lambda tx: [_record(tx, eid, st, "gave_up", f"{max_tries}回送っても届きませんでした") for eid, _, st in items])
                done["gave_up"] = done.get("gave_up", 0) + len(items)
                continue
            try:
                transports[channel].send(bundle_text([k for _, k, _ in items]))   # 取引の外で1回だけ
            except SendFailed as err:
                reason = str(err).strip() or "理由が返っていません"
                in_tx(lambda tx: [_record(tx, eid, st, "failed", reason) for eid, _, st in items])
                done["failed"] = done.get("failed", 0) + len(items)
                continue
            in_tx(lambda tx: [_record(tx, eid, st, "sent", None) for eid, _, st in items])
            done["sent"] = done.get("sent", 0) + len(items)
        return done
