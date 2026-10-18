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
6. 止めるとき（Ctrl+C・止める印）：回線ごとの処理に止めるよう伝え、いまの通話が終わるのを待ってから、その回線の後始末をして、
   回線を返す。回線ごとの処理は、本体とは別の処理のまとまりで立ち上げる（Ctrl+C が回線ごとの処理に届いて、通話の途中で落ちない）。
7. **どの段の誤りでも、本体は止まらない**（データベースに届かない・回線ごとの処理を立ち上げられない など）：記録して（CL-2 へ
   修正依頼）、次の回にまた試す（延ばす間隔は、前に読めた値を使う。同じ段の失敗が続くほど、試す間を延ばす）。
   2026-10-18 の見直し：前は、ログインのときにネットがまだつながっていない・通信が一瞬切れる、だけで本体が終わり、その日は
   電話がかからなかった。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from salescall.core import leases
from salescall.core.access import Access
from salescall.supervisor import ACTOR, config, jobs, report, updates
from salescall.supervisor.locks import AlreadyRunning, SingleInstance, line_in_use_here, lock_dir, machine_id

RESTART_FIRST, RESTART_MAX = 5.0, 300.0   # 回線ごとの処理を立ち上げ直すまでの間（秒。続けて落ちるほど倍に）
RENEW_FALLBACK = 10.0                      # 延ばす間隔をまだ一度も読めていないとき（データベースに届かない）の、次に試すまでの秒数


def holder_name(cfg: config.Config) -> str:
    """この見守り役の名前（PC と契約者で決まる。起動し直しても同じ＝前に落ちた自分の回線を後始末できる）。PC は、PC の名前と、
    OS が PC ごとに持つ番号で見分ける（PC の名前だけでは、同じ名前の PC が2台あると同じ見守り役とみなされ、二重発信の守りが外れる
    ＝2026-10-18 の見直し）。"""
    return f"{socket.gethostname()}#{machine_id()}#{cfg.tenant_id}"


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
    # 別の処理のまとまりで立ち上げる（本体の画面の Ctrl+C が届かない。止めるのは、本体が入力を閉じたときだけ＝いまの通話のあと）
    apart: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                             else {"start_new_session": True})
    proc = subprocess.Popen([sys.executable, "-m", "salescall.supervisor", "line", str(line_id), holder],
                            stdin=subprocess.PIPE, env={**os.environ, **cfg.environ}, **apart)
    child = _Child(proc)
    return child, child


class _NoStop:
    """立ち上げられなかった回線ごとの処理の、止める印の代わり（止めるものが無い）。"""

    def set(self) -> None:
        pass


def _backoff(fails: int) -> float:
    """続けて失敗するほど、次に試すまでの間を延ばす（上限 RESTART_MAX）。"""
    return min(RESTART_FIRST * 2 ** fails, RESTART_MAX)


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
    _renew_every: float | None = None
    version_gap: str = "same"         # プログラムとデータベースの版のずれ（supervisor.updates）

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
        """回線を借りる・延ばす。借りられた回線は処理を動かし、借りられなかった回線は処理を止める。プログラムとデータベースの版が
        ずれていれば、回線ごとの処理を止め（いまの通話は最後まで）、立ち上げない（supervisor.updates。合えば、またかける）。"""
        if not self.cfg.calls:
            return
        before = self.version_gap
        self.version_gap = self.access.in_tenant(ACTOR, self.cfg.tenant_id, lambda tx: updates.check(tx, self.cfg.tenant_id))
        if self.version_gap == updates.PROGRAM_NEW and before != updates.PROGRAM_NEW:   # 流し忘れ（不具合）：CL-2 へ（変わったときだけ）
            report(self.access, self.cfg.tenant_id, "版のずれ（データベースへの表の作りの流し忘れ）",
                   RuntimeError(updates.describe(self.version_gap)))
        for line_id in self.cfg.lines:
            try:
                until = self.access.in_tenant(ACTOR, self.cfg.tenant_id,
                                              lambda tx, l=line_id: leases.take_line(tx, l, self.holder))
            except Exception as e:  # noqa: BLE001 データベースに届かない：延ばせない回線は期限で門が通さなくなる
                report(self.access, self.cfg.tenant_id, "回線の借用", e, line_id=line_id)
                until = None
            if until is None or self.version_gap != updates.SAME:   # 借りられない・版がずれている：その回線の処理を止める
                if until is None:
                    self.leased.discard(line_id)
                else:
                    self.leased.add(line_id)
                self._stop_worker(line_id)
            else:
                self.leased.add(line_id)
                if line_id not in self.workers and not line_in_use_here(line_id):
                    self._launch(line_id, fails=0, now=now)
        try:
            self._renew_every = float(self.access.in_tenant(ACTOR, self.cfg.tenant_id, leases.renew_every))
        except Exception as e:  # noqa: BLE001 読めなければ、前に読めた値（無ければ短い間）で次に試す
            report(self.access, self.cfg.tenant_id, "延ばす間隔の読み出し", e)
        self._next_renew = now + (self._renew_every or RENEW_FALLBACK)

    def _launch(self, line_id: UUID, *, fails: int, now: float | None = None) -> None:
        """回線ごとの処理を立ち上げる。その前に、その回線の後始末をする（呼ぶのは、この PC にその回線の処理が無いと確かめたあと
        ＝いま進行中の試行があれば、前の処理が残したもの）。後始末も立ち上げもできなければ、間をあけてまた試す（続けるほど延ばす。
        毎秒やり直して毎秒記録しない）。"""
        now = time.monotonic() if now is None else now
        try:
            self.access.in_tenant(ACTOR, self.cfg.tenant_id, lambda tx: leases.recover(tx, self.holder, line_id))
            process, stop = self.start_worker(self.cfg, line_id, self.holder)
        except Exception as e:  # noqa: BLE001 立ち上げない（後始末が済まないうちにかけない）
            report(self.access, self.cfg.tenant_id, "回線ごとの処理の立ち上げ", e, line_id=line_id)
            self.workers[line_id] = _Worker(line_id, _NoStop(), None, fails + 1, now + _backoff(fails))
            return
        self.workers[line_id] = _Worker(line_id, stop, process, fails)

    def _stop_worker(self, line_id: UUID) -> None:
        w = self.workers.pop(line_id, None)
        if w is not None:
            w.stop.set()

    def watch_workers(self, now: float) -> None:
        """落ちた回線ごとの処理を後始末して、間をあけて立ち上げ直す。"""
        for line_id, w in list(self.workers.items()):
            if w.process is None:   # 立ち上げ直す時刻を待っている
                if (now >= w.restart_at and line_id in self.leased and self.version_gap == updates.SAME
                        and not line_in_use_here(line_id)):
                    self._launch(line_id, fails=w.fails, now=now)
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
            self.workers[line_id] = _Worker(line_id, w.stop, None, w.fails + 1, now + _backoff(w.fails))

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
        steps: list[tuple[str, Callable[[], None]]] = [("回線の借用", lambda: self.renew(now))] if now >= self._next_renew else []
        steps += [("回線ごとの処理の見張り", lambda: self.watch_workers(now)), ("決まった間隔の仕事", lambda: self.run_jobs(now))]
        for what, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 どの段の誤りでも本体は止めない（次の回にまた試す）
                report(self.access, self.cfg.tenant_id, what, e)
                if what == "回線の借用":
                    self._next_renew = now + (self._renew_every or RENEW_FALLBACK)

    def shutdown(self, wait_seconds: float = 600.0) -> None:
        """回線ごとの処理を止め（いまの通話は最後まで）、終わったら、その回線の後始末をして返す（回線ごとの処理が通話の途中で落ちて
        いれば、その試行を終わらせてから返す。返してから後始末すると、借りている名前が消えて、起動し直すまで残った）。"""
        for w in self.workers.values():
            w.stop.set()
        deadline = time.monotonic() + wait_seconds
        for w in self.workers.values():
            if w.process is not None:
                w.process.join(max(0.0, deadline - time.monotonic()))
        for line_id in list(self.leased):
            if line_in_use_here(line_id):   # まだ通話の途中：返さない（期限で切れたあと、ほかが後始末する）
                continue
            def back(tx: Any, l: UUID = line_id) -> None:
                leases.recover(tx, self.holder, l)
                leases.return_line(tx, l, self.holder)
            try:
                self.access.in_tenant(ACTOR, self.cfg.tenant_id, back)
            except Exception:  # noqa: BLE001 返せなくても、期限で次が後始末する
                pass
        self.pool.shutdown(wait=True)


def serve(cfg: config.Config, stop: threading.Event | None = None) -> None:
    """見守り役を動かす（止める印が立つか Ctrl+C まで）。"""
    stop = stop or threading.Event()
    lock = lock_dir() / f"salescall-supervisor-{cfg.tenant_id}.lock"
    with SingleInstance(lock):
        sup = Supervisor(cfg)
        try:
            sup.start()
        except Exception as e:  # noqa: BLE001 起動のときに届かなくても止めない（tick がまた試す）
            report(sup.access, cfg.tenant_id, "起動", e)
        try:
            while not stop.is_set():
                sup.tick()
                stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            sup.shutdown()
