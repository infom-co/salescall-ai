"""見守り役の本体（DESIGN.md 5-4）。持ち主 CL-2。

1. **1台の PC に1つだけ**（PC の中の印のファイルを、ほかに開けないように押さえる。同じ PC で2つ動くと、同じ回線で二重にかけうる）。
   回線ごとの処理も、回線ごとの印を押さえる（本体が落ちて立ち上げ直したとき、前の回線ごとの処理がまだ通話の途中なら、その回線は
   後始末も立ち上げ直しもしない＝話している通話を結果不明にしない・同じスマホで二重にかけない。終われば印が外れる）。
2. **回線ごとの処理を立ち上げる前に、その回線の後始末**をする：この見守り役（PC と契約者で決まる名前）の、その回線の進行中の試行を
   終わらせる（予約のまま＝取り消し、発信してから＝結果不明。お客様が出ていれば、締めがお詫びしてかけ直す）。立ち上げるのは、
   この PC にその回線の処理が無いと確かめたあとなので、進行中の試行があれば、前に落ちた処理が残したもの。
3. **回線を借りて、決まった間隔で延ばす**（延ばすのは本体。長い仕事の裏でも止まらないよう、仕事とは別に回す）。借りられない回線
   （ほかの見守り役が生きて借りている）は動かさず、延ばすたびに借り直しを試す。延ばせなかった回線は、その回線の処理を止める
   （予約は門が通さない＝借用の期限）。
4. **回線ごとの処理を、別の処理（プロセス。`python -m salescall.supervisor line`）で動かす**（supervisor.line）。落ちたら、その回線の進行中の試行を後始末し、
   「見守り役の誤り」として記録して（CL-2 へ修正依頼）、間をあけて立ち上げ直す（続けて落ちるほど間を延ばす。上限5分）。
5. **決まった間隔の仕事**（supervisor.jobs の表）を、それぞれ別の流れ（スレッド）で、同じ仕事が重ならないように回す。
6. 止めるとき（Ctrl+C・止める印）：回線ごとの処理に止めるよう伝え、いまの通話が終わるのを待ってから、回線を返す。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from salescall.core import leases
from salescall.core.access import Access
from salescall.supervisor import ACTOR, config, jobs, report
from salescall.supervisor.locks import AlreadyRunning, SingleInstance, line_in_use_here

RESTART_FIRST, RESTART_MAX = 5.0, 300.0   # 回線ごとの処理を立ち上げ直すまでの間（秒。続けて落ちるほど倍に）


def holder_name(cfg: config.Config) -> str:
    """この見守り役の名前（PC と契約者で決まる。起動し直しても同じ＝前に落ちた自分の回線を後始末できる）。"""
    return f"{socket.gethostname()}#{cfg.tenant_id}"


@dataclass
class _Worker:
    line_id: UUID
    stop: Any
    process: Any
    fails: int = 0
    restart_at: float = 0.0


class _Child:
    """回線ごとの処理（別の命令として立ち上げた処理）。止めるときは入力を閉じる（本体が落ちても入力が閉じるので、回線ごとの処理は
    いまの通話のあと止まる＝借用を延ばす本体の無いまま、かけ続けない）。"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def exitcode(self) -> int | None:
        return self.proc.returncode

    def set(self) -> None:   # 止める（入力を閉じる）
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            self.proc.stdin.close()

    def join(self, timeout: float | None = None) -> None:
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            pass


def _start_process(cfg: config.Config, line_id: UUID, holder: str) -> tuple[Any, Any]:
    proc = subprocess.Popen([sys.executable, "-m", "salescall.supervisor", "line", str(line_id), holder],
                            stdin=subprocess.PIPE, env={**os.environ, **cfg.environ})
    child = _Child(proc)
    return child, child


@dataclass
class Supervisor:
    cfg: config.Config
    start_worker: Callable[[config.Config, UUID, str], tuple[Any, Any]] = _start_process
    holder: str = ""
    access: Access = field(init=False)
    workers: dict[UUID, _Worker] = field(default_factory=dict)
    leased: set[UUID] = field(default_factory=set)
    _next_job: dict[str, float] = field(default_factory=dict)
    _running: dict[str, Future] = field(default_factory=dict)
    _next_renew: float = 0.0

    def __post_init__(self) -> None:
        self.holder = self.holder or holder_name(self.cfg)
        self.access = Access(self.cfg.dsn)
        self.pool = ThreadPoolExecutor(max_workers=len(jobs.JOBS), thread_name_prefix="salescall-job")
        self.context = jobs.Context(self.access, self.cfg, self.holder)

    # ── 起動 ──
    def start(self) -> None:
        self.renew(time.monotonic())   # 前に落ちた自分の分の後始末は、回線ごとの処理を立ち上げる前に `_launch` がする

    # ── 回線の借用と、回線ごとの処理 ──
    def renew(self, now: float) -> None:
        """回線を借りる・延ばす。借りられた回線は処理を動かし、借りられなかった回線は処理を止める。"""
        if not self.cfg.calls:
            return
        for line_id in self.cfg.lines:
            try:
                until = self.access.in_tenant(ACTOR, self.cfg.tenant_id,
                                              lambda tx, l=line_id: leases.take_line(tx, l, self.holder))
            except Exception as e:  # noqa: BLE001 データベースに届かない：延ばせない回線は期限で門が通さなくなる
                report(self.access, self.cfg.tenant_id, "回線の借用", e, line_id=line_id)
                until = None
            if until is None:
                self.leased.discard(line_id)
                self._stop_worker(line_id)
            else:
                self.leased.add(line_id)
                if line_id not in self.workers and not line_in_use_here(line_id):
                    self._launch(line_id, fails=0)
        every = self.access.in_system(ACTOR, leases.renew_every)
        self._next_renew = now + every

    def _launch(self, line_id: UUID, *, fails: int) -> None:
        """回線ごとの処理を立ち上げる。その前に、その回線の後始末をする（呼ぶのは、この PC にその回線の処理が無いと確かめたあと
        ＝いま進行中の試行があれば、前の処理が残したもの）。"""
        try:
            self.access.in_tenant(ACTOR, self.cfg.tenant_id, lambda tx: leases.recover(tx, self.holder, line_id))
        except Exception as e:  # noqa: BLE001 後始末できなければ立ち上げない（次の延長の回にまた試す）
            report(self.access, self.cfg.tenant_id, "回線の後始末", e, line_id=line_id)
            return
        process, stop = self.start_worker(self.cfg, line_id, self.holder)
        self.workers[line_id] = _Worker(line_id, stop, process, fails)

    def _stop_worker(self, line_id: UUID) -> None:
        w = self.workers.pop(line_id, None)
        if w is not None:
            w.stop.set()

    def watch_workers(self, now: float) -> None:
        """落ちた回線ごとの処理を後始末して、間をあけて立ち上げ直す。"""
        for line_id, w in list(self.workers.items()):
            if w.process is None:   # 立ち上げ直す時刻を待っている
                if now >= w.restart_at and line_id in self.leased and not line_in_use_here(line_id):
                    self._launch(line_id, fails=w.fails)
                continue
            if w.process.is_alive():
                continue
            if line_in_use_here(line_id):   # 前の回線ごとの処理が、まだ通話の途中（立ち上げた処理は印を取れずに終わった）
                self.workers[line_id] = _Worker(line_id, w.stop, None, w.fails, now + RESTART_FIRST)
                continue
            try:   # 落ちた処理の通話を、すぐ後始末する（お客様が出ていれば、締めがお詫びしてすぐかけ直す）
                self.access.in_tenant(ACTOR, self.cfg.tenant_id,
                                      lambda tx: leases.recover(tx, self.holder, line_id))
            except Exception as e:  # noqa: BLE001
                report(self.access, self.cfg.tenant_id, "回線の後始末", e, line_id=line_id)
            report(self.access, self.cfg.tenant_id, "回線ごとの処理が止まった",
                   RuntimeError(f"終わりの番号 {getattr(w.process, 'exitcode', None)}"), line_id=line_id)
            wait = min(RESTART_FIRST * 2 ** w.fails, RESTART_MAX)
            self.workers[line_id] = _Worker(line_id, w.stop, None, w.fails + 1, now + wait)

    # ── 決まった間隔の仕事 ──
    def run_jobs(self, now: float) -> None:
        for job in jobs.JOBS:
            if not job.runs_here(self.cfg) or now < self._next_job.get(job.name, 0.0):
                continue
            running = self._running.get(job.name)
            if running is not None and not running.done():   # 前の回がまだ終わっていない：重ねない
                continue
            self._running[job.name] = self.pool.submit(jobs.run_job, self.context, job)
            self._next_job[job.name] = now + job.every

    # ── 1回分（本体の繰り返し） ──
    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if now >= self._next_renew:
            self.renew(now)
        self.watch_workers(now)
        self.run_jobs(now)

    def shutdown(self, wait_seconds: float = 600.0) -> None:
        """回線ごとの処理を止め（いまの通話は最後まで）、終わったら回線を返す。"""
        for w in self.workers.values():
            w.stop.set()
        deadline = time.monotonic() + wait_seconds
        for w in self.workers.values():
            if w.process is not None:
                w.process.join(max(0.0, deadline - time.monotonic()))
        for line_id in list(self.leased):
            if line_in_use_here(line_id):   # まだ通話の途中：返さない（期限で切れたあと、ほかが後始末する）
                continue
            try:
                self.access.in_tenant(ACTOR, self.cfg.tenant_id, lambda tx, l=line_id: leases.return_line(tx, l, self.holder))
            except Exception:  # noqa: BLE001 返せなくても、期限で次が後始末する
                pass
        self.pool.shutdown(wait=True)


def serve(cfg: config.Config, stop: threading.Event | None = None) -> None:
    """見守り役を動かす（止める印が立つか Ctrl+C まで）。"""
    stop = stop or threading.Event()
    lock = Path(tempfile.gettempdir()) / f"salescall-supervisor-{cfg.tenant_id}.lock"
    with SingleInstance(lock):
        sup = Supervisor(cfg)
        sup.start()
        try:
            while not stop.is_set():
                sup.tick()
                stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            sup.shutdown()
