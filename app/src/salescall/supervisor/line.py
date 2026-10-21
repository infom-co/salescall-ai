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
   - 回線の理由（gate.LINE_REASONS）なら、行の予定は書き直さず、この回線はしばらく休む（ほかの回線は、その行をかけられる）。
   - 依頼者の理由（gate.CLIENT_REASONS）なら、その依頼者の行は、この回だけ飛ばす。業務の理由（gate.BUSINESS_REASONS：その業務の
     今日の上限・時刻を指定された約束が、その業務の平均の長さのうちに来る）なら、その業務の行は、この回だけ飛ばす（同じ依頼者の
     ほかの業務の行はかける）。
3. 予約証で発信の出口（dial_exit.dial）がかけ、通話を進める部品（CL-1）が最後まで進める。外へ出た誤りは、その試行を結果不明に
   して「見守り役の誤り」として記録する（止めない）。回線がまだ前の通話の途中・回線の部品がかけられなかったときは、その回線を
   休ませる（かけられなかったことは、回線の異常として数える＝続けば、エラーの処置の表のとおり、その回線を止める）。
締め（判定・次の動き）は、見守り役の決まった間隔の仕事がする（jobs の settle）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import attempts, dial_exit, gate, next_action, scheduler, settings, watch
from salescall.core.access import Access, Actor
from salescall.core.errors import ErrorReport
from salescall.supervisor import ACTOR, plugins, report, updates

CANDIDATES_PER_CLIENT = 20   # 1回に門へ渡す行の数の上限（依頼者ごと）。通れば1本かけて、また初めから並べ直す
IDLE_SECONDS = 5             # かける行が無いとき・版が合わないときの待ち


@dataclass(frozen=True)
class Round:
    """1回分の結果。called＝かけた試行、pause＝回線の理由（その回線をしばらく休む）。"""
    called: UUID | None = None
    pause: str | None = None


def _claim(tx: Any, task: dict[str, Any], line_id: UUID, holder: str, tenant_id: UUID, client_id: UUID) -> Any:
    got = gate.claim(tx, task_id=task["task_id"], line_id=line_id, lease_holder=holder)
    if isinstance(got, gate.Denied) and got.reason not in gate.LINE_REASONS | gate.CLIENT_REASONS | gate.BUSINESS_REASONS:
        next_action.after_denial(tx, task["task_id"], got, tenant_id=tenant_id, client_id=client_id,
                                 business_id=task["business_id"])
    return got


def _end_unknown(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, attempt_id: UUID) -> None:
    """通話を進める部品から誤りが外へ出た：まだ進行中なら結果不明にする（お客様が出ていれば、締めがお詫びしてかけ直す）。"""
    def work(tx: Any) -> None:
        if tx.one("SELECT salescall.is_active(state) AND salescall.is_dialed(state) AS live FROM salescall.attempts"
                  " WHERE attempt_id = %s", (attempt_id,))["live"]:   # 発信まで進み、まだ進行中
            attempts.move(tx, attempt_id, "technical_error")
    access.in_client(actor, tenant_id, client_id, work)


def one_round(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, holder: str, driver: dial_exit.LineDriver,
              runner: plugins.CallRunner) -> Round:
    clients = access.in_tenant(actor, tenant_id, lambda tx: scheduler.clients_for_line(tx, line_id))
    for promised_only in (True, False):   # 約束したかけ直しを、すべての依頼者について先に（目印で分けるのは due_tasks の1か所）
        for client_id in clients:
            got = _client_round(access, actor, tenant_id, client_id, line_id, holder, driver, runner, promised_only)
            if got is not None:
                return got
    return Round()


def _client_round(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, line_id: UUID, holder: str,
                  driver: dial_exit.LineDriver, runner: plugins.CallRunner, promised_only: bool = False) -> Round | None:
    """1つの依頼者の行を、門に渡す。かけたか回線を休むなら Round、この依頼者でかけられる行が無ければ None。
    業務の理由で断られた業務は、この回だけ飛ばし、その業務を除いて候補を読み直す（前は読み直さなかったので、上限に達した業務の
    行が候補の先頭を埋めると、同じ依頼者のほかの業務の行が1日じゅう候補に入らなかった＝2026-10-18 の見直し）。"""
    capped: set[UUID] = set()
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
                done = dial_exit.dial(access, actor, tenant_id, client_id, got.attempt_id, driver)
                if done is dial_exit.Outcome.DIALED:
                    try:
                        runner.run(access, actor, got, tenant_id=tenant_id, client_id=client_id)
                    except Exception as e:  # noqa: BLE001 その通話だけを結果不明にして、次へ進む（止めない）
                        _end_unknown(access, actor, tenant_id, client_id, got.attempt_id)
                        report(access, tenant_id, "通話を進める部品", e, line_id=line_id)
                    return Round(called=got.attempt_id)
                if done is dial_exit.Outcome.NOT_PLACED:   # 回線の異常として数える（続けば、その回線を止める）
                    watch.after_error(access, actor, ErrorReport("line", None, "回線の部品が電話をかけられなかった"),
                                      tenant_id=tenant_id, line_id=line_id)
                if done in (dial_exit.Outcome.NOT_PLACED, dial_exit.Outcome.LINE_IN_CALL):
                    # かけられなかった・まだ前の通話の途中：その回線を休ませる（休まず次の行へ進むと、行の回数と回線の
                    # 1日の上限を数分で使い切りうる＝2026-10-18 の見直し）
                    return Round(pause=done.value)
                return Round()   # 予約証が使えなかった（緊急停止など）：次の回で、門から考え直す
            if got.reason in gate.LINE_REASONS:
                return Round(pause=got.reason)
            if got.reason in gate.CLIENT_REASONS:
                return None
            if got.reason in gate.BUSINESS_REASONS:
                capped.add(task["business_id"])
                newly = True
        if not newly:
            return None


def loop(access: Access, tenant_id: UUID, line: UUID, holder: str, driver: dial_exit.LineDriver, runner: plugins.CallRunner,
         stop: Any) -> None:
    """回線ごとの処理（中央の見守り役が、別の流れで動かす）。stop が立つまで繰り返す（いまの通話は最後まで進めてから止まる）。"""
    pause = int(access.in_tenant(ACTOR, tenant_id, lambda tx: settings.system_value(tx, "gate.denied_retry_wait")))
    while not stop.is_set():
        try:
            # プログラムとデータベースの版が合わなければ、新しい電話をかけない（中央の見守り役が立ち上げたあとでデータベースが
            # 新しくなった、でも、かける処理が自分の版で確かめる。supervisor.updates）
            if access.in_tenant(ACTOR, tenant_id, updates.current) != updates.SAME:
                stop.wait(IDLE_SECONDS)
                continue
            got = one_round(access, ACTOR, tenant_id, line, holder, driver, runner)
            if got.called is None:
                stop.wait(pause if got.pause else IDLE_SECONDS)
        except Exception as e:  # noqa: BLE001 記録して、少し待ってから続ける（止めない）
            report(access, tenant_id, "回線ごとの処理", e, line_id=line)
            stop.wait(IDLE_SECONDS)
