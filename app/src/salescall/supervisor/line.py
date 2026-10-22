"""回線ごとの処理（DESIGN.md 5-4・5-5）。持ち主 CL-2。

中央の見守り役（サーバー）が、PC の部品がつないでいる回線1本ごとに、別の流れ（スレッド）で `loop` を動かす。電話の操作と通話の
声は、その回線の電話の部品（`links.Links.driver`＝PC の部品へ頼む形）を通る。1本の通話の処理の誤りは、その通話だけを結果不明にして
次へ進む（ほかの回線を巻き込まない＝CL-1 の 2026-10-13 の (c)。スマホのアプリ・音声の機器を扱う部分は PC の部品の側で回線ごとに分ける）。
1回線（スマホ1台）で同時に1本なので、この処理は1本ずつ順にかける（Intent.md 8-3）。

1回分（`one_round`）：
1. その回線でかけてよい依頼者を並べ（scheduler.clients_for_line）、依頼者ごとに、いまかけてよさそうな行を門に渡す順に出す
   （scheduler.due_tasks。判定はしない）。**お客様と約束したかけ直しを、すべての依頼者について先に出し、それが済んでから
   ふつうの電話を始める**（2026-10-19 ユーザー：「その日の約束の電話には目印が付き、それを処理してから、通常の順序の電話を
   始めた方がいい」。約束の時刻・幅が来ていない約束は、来るまで出ない＝その時間の中で最優先になる）。
2. 門（gate.claim）に渡す。通れば予約証が出る。
   - 行のせいの不許可は、次の動き（next_action.after_denial）へ渡す（次にいつ候補に出すかを決めるのは次の動きだけ＝Q4）。
     ただし、すぐに変わる行の理由（gate.TASK_REASONS：同じ行・同じお客様・同じ番号の発信が進行中）は、この回だけその行を飛ばし、
     予定は書き直さない（書き直さない理由のすべては gate.PLAN_KEPT の1か所）。
   - 回線の理由（gate.LINE_REASONS）なら、行の予定は書き直さず、この回線はしばらく休む（ほかの回線は、その行をかけられる）。
   - 依頼者の理由（gate.CLIENT_REASONS）なら、その依頼者の行は、この回だけ飛ばす。業務の理由（gate.BUSINESS_REASONS：その業務の
     今日の上限・時刻を指定された約束が、その業務の平均の長さのうちに来る）なら、その業務の行は、この回だけ飛ばす（同じ依頼者の
     ほかの業務の行はかける）。
3. 予約証で発信の出口（dial_exit.dial）がかけ、通話を進める部品（CL-1）が最後まで進める。外へ出た誤りは、その試行を結果不明に
   して「見守り役の誤り」として記録する（止めない）。回線がまだ前の通話の途中・回線の部品がかけられなかったときは、その回線を
   休ませる（かけられなかったことは、回線の異常として数える＝続けば、エラーの処置の表のとおり、その回線を止める）。
4. **回線が「通話中」のまま（電話の部品が「通話中」と答え続ける・答えられない）で、かけられない状態が `STUCK_AFTER` 続いたら、
   回線の異常として数える**（`Stuck` の1か所。数える入口は、かけられなかったときと同じ `_line_fault`＝続けば、処置の表のとおり、
   その回線を止めて知らせる。⛔ 2026-10-22 の見直し15：前は、通話中を理由に休み続けるだけで、だれにも知らされなかった）。
締め（判定・次の動き）は、見守り役の決まった間隔の仕事がする（jobs の settle）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import attempts, dial_exit, gate, next_action, scheduler, settings, watch
from salescall.core.access import Access, Actor
from salescall.core.errors import ErrorReport
from salescall.supervisor import ACTOR, CALL_WAIT, plugins, report, updates
from salescall.supervisor.links import CallDriver

CANDIDATES_PER_CLIENT = 20   # 1回に門へ渡す行の数の上限（依頼者ごと）。通れば1本かけて、また初めから並べ直す
IDLE_SECONDS = 5             # かける行が無いとき・版が合わないときの待ち
# 回線が「通話中」のまま、かけられない状態がこれだけ続いたら、回線の異常として数える（秒。通話の最長から決める＝supervisor の1か所。
# 最長の通話より長く「通話中」なら、通話ではなく、電話の部品か回線が動いていない）
STUCK_AFTER = CALL_WAIT


@dataclass(frozen=True)
class Round:
    """1回分の結果。called＝かけた試行、pause＝回線の理由（その回線をしばらく休む）。free＝電話の部品が「通話中ではない」と
    答えた（発信の出口が、通話中かを聞いて先へ進んだ）。"""
    called: UUID | None = None
    pause: str | None = None
    free: bool = False


@dataclass
class Stuck:
    """回線が「通話中」のまま、かけられない状態の見張り（判断はこの1か所）。since＝続けて「通話中」と分かった始まり。"""
    since: float | None = None

    def seen(self, got: Round, now: float) -> bool:
        """1回分の結果を見る。「通話中」が STUCK_AFTER 続いたら True（数えたら、また初めから数える＝続けば、また数える）。
        「通話中ではない」と分かったら数え直す。どちらとも分からない回（かける行が無い・門が断った）は、そのまま。"""
        if got.pause == dial_exit.Outcome.LINE_IN_CALL.value:
            if self.since is None:
                self.since = now
            elif now - self.since >= STUCK_AFTER:
                self.since = now
                return True
        elif got.free:
            self.since = None
        return False


def _line_fault(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, what: str) -> None:
    """回線の異常として数える（入口はこの1か所。続けば、エラーの処置の表のとおり、その回線を止めて知らせる）。"""
    watch.after_error(access, actor, ErrorReport("line", None, what), tenant_id=tenant_id, line_id=line_id)


def _claim(tx: Any, task: dict[str, Any], line_id: UUID, holder: str, tenant_id: UUID, client_id: UUID) -> Any:
    got = gate.claim(tx, task_id=task["task_id"], line_id=line_id, lease_holder=holder)
    if isinstance(got, gate.Denied) and got.reason not in gate.PLAN_KEPT:   # 行の予定を書き直さない理由は gate の1か所
        next_action.after_denial(tx, task["task_id"], got, tenant_id=tenant_id, client_id=client_id,
                                 business_id=task["business_id"])
    return got


def _end_unknown(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, attempt_id: UUID) -> bool:
    """通話を進める部品から誤りが外へ出た・通話を終えずに戻った：まだ進行中なら結果不明にする（お客様が出ていれば、締めがお詫び
    してかけ直す）。結果不明にしたら True。"""
    def work(tx: Any) -> bool:
        if tx.one("SELECT salescall.is_active(state) AND salescall.is_dialed(state) AS live FROM salescall.attempts"
                  " WHERE attempt_id = %s", (attempt_id,))["live"]:   # 発信まで進み、まだ進行中
            attempts.move(tx, attempt_id, "technical_error")
            return True
        return False
    return access.in_client(actor, tenant_id, client_id, work)


def one_round(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, holder: str, driver: CallDriver,
              runner: plugins.CallRunner) -> Round:
    clients = access.in_tenant(actor, tenant_id, lambda tx: scheduler.clients_for_line(tx, line_id))
    held: list[str] = []   # 依頼者の理由（同時の上限・無効）で断られた
    for promised_only in (True, False):   # 約束したかけ直しを、すべての依頼者について先に（目印で分けるのは due_tasks の1か所）
        for client_id in clients:
            got = _client_round(access, actor, tenant_id, client_id, line_id, holder, driver, runner, promised_only, held)
            if got is not None:
                return got
    # どこもかけられず、依頼者の理由で断られていた：回線の理由と同じく休む（⛔ 2026-10-22 の見直し：休まずに数秒ごとに門へ渡し
    # 直し、同時の上限のあいだ、断られた記録を増やし続けた）
    return Round(pause=held[0]) if held else Round()


def _client_round(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, line_id: UUID, holder: str,
                  driver: CallDriver, runner: plugins.CallRunner, promised_only: bool = False,
                  held: list[str] | None = None) -> Round | None:
    """1つの依頼者の行を、門に渡す。かけたか回線を休むなら Round、この依頼者でかけられる行が無ければ None。
    業務の理由で断られた業務は、この回だけ飛ばし、その業務を除いて候補を読み直す（前は読み直さなかったので、上限に達した業務の
    行が候補の先頭を埋めると、同じ依頼者のほかの業務の行が1日じゅう候補に入らなかった＝2026-10-18 の見直し）。"""
    capped: set[UUID] = set()
    held = [] if held is None else held
    while True:
        tasks = access.in_client(actor, tenant_id, client_id,
                                 lambda tx: scheduler.due_tasks(tx, CANDIDATES_PER_CLIENT, skip_businesses=capped,
                                                                promised_only=promised_only))
        newly = False
        for task in tasks:
            if task["business_id"] in capped:
                continue
            got = access.in_client(actor, tenant_id, client_id,
                                   lambda tx, task=task: _claim(tx, task, line_id, holder, tenant_id, client_id))
            if isinstance(got, gate.Claim):
                driver.prepare_call(got.voice_type)   # この通話の声の型を電話の部品へ（おわびの録音も同じ声で）
                done = dial_exit.dial(access, actor, tenant_id, client_id, got.attempt_id, driver)
                if done is dial_exit.Outcome.DIALED:
                    try:
                        runner.run(access, actor, got, tenant_id=tenant_id, client_id=client_id)
                    except Exception as e:  # noqa: BLE001 その通話だけを結果不明にして、次へ進む（止めない）
                        dial_exit.abandon(driver)   # 通話が残っていれば、おわびして切る（お客様を無言で放置しない）
                        _end_unknown(access, actor, tenant_id, client_id, got.attempt_id)
                        report(access, tenant_id, "通話を進める部品", e, line_id=line_id)
                    else:
                        # 通話を終えずに戻った（試行がまだ進行中）：誤りのときと同じに扱う（⛔ 2026-10-22 の見直し：電話はつながった
                        # まま無言になり、回線は「通話中」で止まり続け、だれにも知らされなかった）
                        if _end_unknown(access, actor, tenant_id, client_id, got.attempt_id):
                            dial_exit.abandon(driver)
                            report(access, tenant_id, "通話を進める部品",
                                   RuntimeError("通話を終えないまま戻りました"), line_id=line_id)
                    return Round(called=got.attempt_id, free=True)
                if done is dial_exit.Outcome.NOT_PLACED:   # 回線の異常として数える（続けば、その回線を止める）
                    _line_fault(access, actor, tenant_id, line_id, "回線の部品が電話をかけられなかった")
                if done in (dial_exit.Outcome.NOT_PLACED, dial_exit.Outcome.LINE_IN_CALL, dial_exit.Outcome.WITHHELD):
                    # PC がかけなかった（緊急停止・つながっていない）も休む（⛔ 2026-10-22 の見直し：休まずに数秒ごとに次の行を
                    # 予約し、どの行も「発信に失敗」にしていった）
                    # かけられなかった・まだ前の通話の途中：その回線を休ませる（休まず次の行へ進むと、行の回数と回線の
                    # 1日の上限を数分で使い切りうる＝2026-10-18 の見直し）
                    return Round(pause=done.value, free=done is not dial_exit.Outcome.LINE_IN_CALL)
                return Round(free=True)   # 予約証が使えなかった（緊急停止など）：次の回で、門から考え直す
            if got.reason in gate.LINE_REASONS:
                return Round(pause=got.reason)
            if got.reason in gate.CLIENT_REASONS:
                held.append(got.reason)   # この依頼者は、いまかけられない（ほかの依頼者を見る。どこもかけなければ休む）
                return None
            if got.reason in gate.BUSINESS_REASONS:
                capped.add(task["business_id"])
                newly = True
        if not newly:
            return None


def loop(access: Access, tenant_id: UUID, line: UUID, holder: str, driver: CallDriver, runner: plugins.CallRunner,
         stop: Any) -> None:
    """回線ごとの処理（中央の見守り役が、別の流れで動かす）。stop が立つまで繰り返す（いまの通話は最後まで進めてから止まる）。"""
    pause = int(access.in_tenant(ACTOR, tenant_id, lambda tx: settings.system_value(tx, "gate.denied_retry_wait")))
    stuck = Stuck()
    while not stop.is_set():
        try:
            # プログラムとデータベースの版が合わなければ、新しい電話をかけない（中央の見守り役が立ち上げたあとでデータベースが
            # 新しくなった、でも、かける処理が自分の版で確かめる。supervisor.updates）
            if access.in_tenant(ACTOR, tenant_id, updates.current) != updates.SAME:
                stop.wait(IDLE_SECONDS)
                continue
            got = one_round(access, ACTOR, tenant_id, line, holder, driver, runner)
            if stuck.seen(got, time.monotonic()):
                _line_fault(access, ACTOR, tenant_id, line,
                            f"回線が「通話中」のまま、かけられません（{int(STUCK_AFTER // 60)}分以上。電話の部品が答えない場合も）")
            if got.called is None:
                stop.wait(pause if got.pause else IDLE_SECONDS)
        except Exception as e:  # noqa: BLE001 記録して、少し待ってから続ける（止めない）
            report(access, tenant_id, "回線ごとの処理", e, line_id=line)
            stop.wait(IDLE_SECONDS)
