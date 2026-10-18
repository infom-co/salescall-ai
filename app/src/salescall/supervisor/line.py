"""回線ごとの処理（DESIGN.md 5-4）。持ち主 CL-2。

見守り役の本体が、回線1本ごとに別の処理（プロセス）で `run` を動かす。1本の通話の処理が落ちても、ほかの回線を巻き込まない
（CL-1 の 2026-10-13 の (c)）。1回線（スマホ1台）で同時に1本なので、この処理は1本ずつ順にかける（Intent.md 8-3）。

1回分（`one_round`）：
1. その回線でかけてよい依頼者を並べ（scheduler.clients_for_line）、依頼者ごとに、いまかけてよさそうな行を門に渡す順に出す
   （scheduler.due_tasks。判定はしない）。
2. 門（gate.claim）に渡す。通れば予約証が出る。
   - 行のせいの不許可は、次の動き（next_action.after_denial）へ渡す（次にいつ候補に出すかを決めるのは次の動きだけ＝Q4）。
   - 回線の理由（gate.LINE_REASONS）なら、行の予定は書き直さず、この回線はしばらく休む（ほかの回線は、その行をかけられる）。
   - 依頼者の理由（gate.CLIENT_REASONS）なら、その依頼者の行は、この回だけ飛ばす。業務の理由（gate.BUSINESS_REASONS：その業務の
     今日の上限）なら、その業務の行は、この回だけ飛ばす（同じ依頼者のほかの業務の行はかける）。
3. 予約証で発信の出口（dial_exit.dial）がかけ、通話を進める部品（CL-1）が最後まで進める。外へ出た誤りは、その試行を結果不明に
   して「見守り役の誤り」として記録する（止めない）。
締め（判定・次の動き）は、見守り役の決まった間隔の仕事がする（jobs の settle）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import attempts, dial_exit, emergency, gate, next_action, scheduler, settings
from salescall.core.access import Access, Actor
from salescall.supervisor import ACTOR, config, plugins, report
from salescall.supervisor.locks import SingleInstance, line_lock

CANDIDATES_PER_CLIENT = 20   # 1回に門へ渡す行の数の上限（依頼者ごと）。通れば1本かけて、また初めから並べ直す
IDLE_SECONDS = 5             # かける行が無いとき・緊急停止の印があるときの待ち


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
        state = tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))["s"]
        if state in attempts.ACTIVE_STATES and state != "claimed":
            attempts.move(tx, attempt_id, "technical_error")
    access.in_client(actor, tenant_id, client_id, work)


def one_round(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, holder: str, driver: dial_exit.LineDriver,
              runner: plugins.CallRunner) -> Round:
    for client_id in access.in_tenant(actor, tenant_id, lambda tx: scheduler.clients_for_line(tx, line_id)):
        tasks = access.in_client(actor, tenant_id, client_id, lambda tx: scheduler.due_tasks(tx, CANDIDATES_PER_CLIENT))
        capped: set[UUID] = set()
        for task in tasks:
            if task["business_id"] in capped:
                continue
            got = access.in_client(actor, tenant_id, client_id,
                                   lambda tx, task=task: _claim(tx, task, line_id, holder, tenant_id, client_id))
            if isinstance(got, gate.Claim):
                if dial_exit.dial(access, actor, tenant_id, client_id, got.attempt_id, driver):
                    try:
                        runner.run(access, actor, got, tenant_id=tenant_id, client_id=client_id)
                    except Exception as e:  # noqa: BLE001 その通話だけを結果不明にして、次へ進む（止めない）
                        _end_unknown(access, actor, tenant_id, client_id, got.attempt_id)
                        report(access, tenant_id, "通話を進める部品", e, line_id=line_id)
                return Round(called=got.attempt_id)
            if got.reason in gate.LINE_REASONS:
                return Round(pause=got.reason)
            if got.reason in gate.CLIENT_REASONS:
                break
            if got.reason in gate.BUSINESS_REASONS:
                capped.add(task["business_id"])
    return Round()


def run(environ: dict[str, str], line_id: str, holder: str, stop: Any) -> None:
    """回線ごとの処理の入口（別の処理として動く）。stop が立つまで繰り返す（いまの通話は最後まで進めてから止まる）。"""
    cfg = config.load(environ)
    access, line = Access(cfg.dsn), UUID(line_id)
    with SingleInstance(line_lock(line)):   # 同じ回線の処理は、PC の中で1つだけ（取れなければ AlreadyRunning で終わる）
        _loop(cfg, access, line, holder, stop)


def _loop(cfg: config.Config, access: Access, line: UUID, holder: str, stop: Any) -> None:
    driver = plugins.load(cfg.line_driver, line)
    runner = plugins.load(cfg.call_runner, line)
    pause = int(access.in_system(ACTOR, lambda tx: settings.system_value(tx, "gate.denied_retry_wait")))
    while not stop.is_set():
        try:
            if emergency.engaged() is not None:   # 本番の PC の緊急停止の印（出口も見るが、候補を出す前に休む）
                stop.wait(IDLE_SECONDS)
                continue
            got = one_round(access, ACTOR, cfg.tenant_id, line, holder, driver, runner)
            if got.called is None:
                stop.wait(pause if got.pause else IDLE_SECONDS)
        except Exception as e:  # noqa: BLE001 記録して、少し待ってから続ける（止めない）
            report(access, cfg.tenant_id, "回線ごとの処理", e, line_id=line)
            stop.wait(IDLE_SECONDS)
